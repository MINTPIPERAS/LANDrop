import { computed, reactive, ref } from 'vue'
import { invoke } from '@tauri-apps/api/core'
import {
  ApiError,
  cancelTransfer,
  createTransfer,
  credentialStatus,
  ensureService,
  getConfig,
  getLogs,
  getScheduleStatus,
  getTransfer,
  listTransfers,
  openLogDir,
  probeHostKey,
  putConfig,
  resetServiceCache,
  retryTransfer,
  runSchedule,
  saveCredential,
  scanDirectory,
  subscribeTransfer,
  trustHostKey,
  type DeviceConfig,
  type HostKeyInfo,
  type LogSummary,
  type SavedConfig,
  type ScheduleConfig,
  type ScheduleMode,
  type ScheduleStatus,
  type TransferRequest,
  type TransferSnapshot,
} from '../services/api'

export type ServiceState = 'connecting' | 'ready' | 'error'

export type Hotkey = {
  modifiers: string[]
  key: string
}

/** Default hotkey; the config file can override it. */
export const DEFAULT_HOTKEY: Hotkey = { modifiers: ['ctrl', 'alt'], key: 'space' }

export const HOTKEY_MODIFIERS = ['ctrl', 'alt', 'shift', 'super'] as const

const MODIFIER_LABELS: Record<string, string> = {
  ctrl: 'Ctrl',
  alt: 'Alt',
  shift: 'Shift',
  super: 'Win',
}

export type ConnectionForm = {
  deviceName: string
  host: string
  port: number
  username: string
  remoteDir: string
  authMethod: 'key' | 'password'
  privateKeyPath: string
  password: string
  rememberPassword: boolean
}

const EMPTY_FORM: ConnectionForm = {
  deviceName: '',
  host: '',
  port: 22,
  username: '',
  remoteDir: '',
  // Password is the simplest option for first-time users; key auth stays available.
  authMethod: 'password',
  privateKeyPath: '',
  password: '',
  rememberPassword: false,
}

export type LocalSource = {
  path: string
  kind: 'file' | 'folder'
  size: number
  fileCount: number
}

/** A queued source plus the outcome of its last attempt. */
export type QueuedSource = LocalSource & {
  key: string
  overwrite?: boolean
  error?: string | null
  errorCode?: string | null
}

export type BatchState = {
  running: boolean
  total: number
  done: number
  ok: number
  partial: number
  failed: number
  cancelled: boolean
}

const EMPTY_BATCH: BatchState = {
  running: false,
  total: 0,
  done: 0,
  ok: 0,
  partial: 0,
  failed: 0,
  cancelled: false,
}

export const serviceState = ref<ServiceState>('connecting')
export const serviceMessage = ref('正在启动本机传输服务')
export const form = reactive<ConnectionForm>({ ...EMPTY_FORM })
export const hotkey = ref<Hotkey>({ ...DEFAULT_HOTKEY, modifiers: [...DEFAULT_HOTKEY.modifiers] })
export const tasks = ref<TransferSnapshot[]>([])
export const activeTaskId = ref<string | null>(null)
export const devices = ref<DeviceConfig[]>([])
export const activeDeviceId = ref('')
export const queue = ref<QueuedSource[]>([])
export const batch = ref<BatchState>({ ...EMPTY_BATCH })
export const batchMessage = ref<string | null>(null)
export const settings = reactive({
  logDir: '',
  logRetentionDays: 14,
  closeToTray: true,
})
export const logsInfo = ref<LogSummary | null>(null)
export const autostartEnabled = ref(false)
export const schedules = ref<ScheduleConfig[]>([])
export const scheduleStatus = ref<Record<string, ScheduleStatus>>({})
export const systemTasksSupported = ref(false)

export type ScheduleDraft = {
  name: string
  sourcePath: string
  recursive: boolean
  deviceId: string
  remoteDir: string
  overwrite: boolean
  mode: ScheduleMode
  time: string
  date: string
  weekdays: number[]
}

export const scheduleDraft = reactive<ScheduleDraft>({
  name: '',
  sourcePath: '',
  recursive: false,
  deviceId: '',
  remoteDir: '',
  overwrite: false,
  mode: 'daily',
  time: '09:00',
  date: '',
  weekdays: [],
})
export const hostKey = ref<HostKeyInfo | null>(null)
export const passwordSaved = ref(false)
export const busy = ref(false)
export const lastError = ref<string | null>(null)

const streams = new Map<string, { close: () => void }>()

export const activeTask = computed(
  () => tasks.value.find(task => task.id === activeTaskId.value) ?? null,
)

export const isLocked = computed(() => {
  if (batch.value.running) return true
  const task = activeTask.value
  return task !== null && (task.status === 'running' || task.status === 'queued')
})

export const canSend = computed(() => {
  return Boolean(
    queue.value.length &&
      form.host.trim() &&
      form.username.trim() &&
      form.remoteDir.trim() &&
      !isLocked.value &&
      !busy.value &&
      serviceState.value === 'ready',
  )
})

/** Why the send button is disabled, in plain language; empty when ready. */
export const sendBlockReason = computed(() => {
  if (serviceState.value === 'connecting') return '应用还在启动，请稍等几秒'
  if (serviceState.value === 'error') return '本机服务未启动，请重新打开应用'
  if (isLocked.value) return '正在发送，请等待这一批完成'
  if (busy.value) return '正在处理，请稍候'
  if (!queue.value.length) return '请先选择要发送的文件'
  if (!form.host.trim()) return '请先填写对方电脑的 IP 地址'
  if (!form.username.trim()) return '请先填写对方电脑的用户名'
  if (!form.remoteDir.trim()) return '请先设置文件保存位置'
  return ''
})

export const queueSummary = computed(() => ({
  count: queue.value.length,
  size: queue.value.reduce((sum, item) => sum + item.size, 0),
}))

/** True when a failed item can be retried with the overwrite flag. */
export const hasTargetConflicts = computed(() =>
  queue.value.some(item => item.errorCode === 'target_exists'),
)

export function fileName(path: string | null): string {
  return path?.split(/[\\/]/).pop() ?? ''
}

function sourceKey(path: string): string {
  return path.trim().toLowerCase()
}

/** Append files to the queue, skipping paths that are already queued. */
export function addFiles(paths: string[]): number {
  const seen = new Set(queue.value.map(item => item.key))
  let added = 0
  for (const path of paths) {
    const key = sourceKey(path)
    if (!key || seen.has(key)) continue
    seen.add(key)
    queue.value.push({ key, path, kind: 'file', size: 0, fileCount: 1 })
    added += 1
  }
  return added
}

/** Append a folder, reading its size first so the queue can show the volume. */
export async function addFolder(path: string): Promise<string | null> {
  const key = sourceKey(path)
  if (!key) return '无效的文件夹路径'
  if (queue.value.some(item => item.key === key)) return null
  try {
    const summary = await scanDirectory(path)
    queue.value.push({
      key,
      path,
      kind: 'folder',
      size: summary.total_size,
      fileCount: summary.total_files,
    })
    return null
  } catch (error) {
    queue.value.push({ key, path, kind: 'folder', size: 0, fileCount: 0 })
    return error instanceof Error ? error.message : '无法读取文件夹内容'
  }
}

export function removeSource(key: string) {
  if (batch.value.running) return
  queue.value = queue.value.filter(item => item.key !== key)
}

export function clearQueue() {
  if (batch.value.running) return
  queue.value = []
  batchMessage.value = null
}

const ACCELERATOR_NAMES: Record<string, string> = {
  ctrl: 'Ctrl',
  alt: 'Alt',
  shift: 'Shift',
  super: 'Super',
}

const KEY_LABELS: Record<string, string> = {
  space: 'Space',
  enter: 'Enter',
  tab: 'Tab',
  escape: 'Esc',
  backspace: 'Backspace',
  delete: 'Delete',
  insert: 'Insert',
  home: 'Home',
  end: 'End',
  pageup: 'PageUp',
  pagedown: 'PageDown',
  arrowup: '↑',
  arrowdown: '↓',
  arrowleft: '←',
  arrowright: '→',
}

export function hotkeyAccelerator(value: Hotkey = hotkey.value): string {
  const parts = HOTKEY_MODIFIERS.filter(modifier => value.modifiers.includes(modifier)).map(
    modifier => ACCELERATOR_NAMES[modifier],
  )
  parts.push(value.key)
  return parts.join('+')
}

export function hotkeyLabel(value: Hotkey = hotkey.value): string {
  const parts = HOTKEY_MODIFIERS.filter(modifier => value.modifiers.includes(modifier)).map(
    modifier => MODIFIER_LABELS[modifier],
  )
  parts.push(keyLabel(value.key))
  return parts.join(' + ')
}

function keyLabel(key: string): string {
  if (KEY_LABELS[key]) return KEY_LABELS[key]
  if (/^f\d{1,2}$/.test(key)) return key.toUpperCase()
  return key.length === 1 ? key.toUpperCase() : key
}

/** Validate an edited hotkey; ``null`` means it cannot be registered at all. */
export function buildHotkey(modifiers: readonly string[], key: string): Hotkey | null {
  const unique = HOTKEY_MODIFIERS.filter(modifier =>
    modifiers.some(item => item.trim().toLowerCase() === modifier),
  )
  const normalizedKey = typeof key === 'string' ? key.trim().toLowerCase() : ''
  if (!unique.length || !normalizedKey) return null
  if (normalizedKey.length > 32 || /[\s+,]/.test(normalizedKey)) return null
  return { modifiers: [...unique], key: normalizedKey }
}

/** Normalize a stored config into a hotkey that can be registered. */
export function normalizeHotkey(modifiers: unknown, key: unknown): Hotkey {
  const list = Array.isArray(modifiers)
    ? modifiers.filter((item): item is string => typeof item === 'string')
    : []
  return (
    buildHotkey(list, typeof key === 'string' ? key : '') ?? {
      modifiers: [...DEFAULT_HOTKEY.modifiers],
      key: DEFAULT_HOTKEY.key,
    }
  )
}

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const value = bytes / 1024 ** index
  return `${value >= 100 || index === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[index]}`
}

export function formatSpeed(bytesPerSecond: number): string {
  return bytesPerSecond > 0 ? `${formatBytes(bytesPerSecond)}/s` : '—'
}

export function phaseLabel(task: TransferSnapshot): string {
  if (task.status === 'succeeded') return '已完成'
  if (task.status === 'partial') return '部分完成'
  if (task.status === 'failed') return '失败'
  if (task.status === 'cancelled') return '已取消'
  if (task.status === 'queued') return '等待发送'
  switch (task.phase) {
    case 'connecting':
      return '正在连接对方电脑'
    case 'checking':
      return '检查断点'
    case 'transferring':
      return '正在发送'
    case 'verifying':
      return '正在核对文件'
    case 'partial':
      return '部分完成'
    default:
      return '处理中'
  }
}

const ERROR_HINTS: Record<string, string> = {
  auth_failed: '登录对方电脑失败，请检查用户名、密码或密钥文件是否正确。',
  host_key_changed: '对方电脑的身份和上次不一致，已停止发送。请重新验证对方电脑身份。',
  timeout: '连接对方电脑超时，请确认两台电脑在同一个网络，并且对方已允许远程连接。',
  network_error: '网络连接中断了，请检查网络后点「重新发送」。',
  target_exists: '对方电脑上已经有同名文件。可以点「覆盖并重发」。',
  verify_failed: '文件发送后核对不一致，请点「重新发送」再试一次。',
  state_conflict: '对方电脑上还有上次未完成的临时文件，请稍后再试。',
  local_changed: '文件在发送过程中被修改了，请重新选择文件。',
  publish_failed: '文件已传完，但保存到最终位置时失败，请重试。',
  internal_error: '应用内部出错了，请打开日志查看详细信息。',
}

/** Plain-language explanation for a transfer error code. */
export function errorHint(code: string | null | undefined): string | null {
  if (!code) return null
  return ERROR_HINTS[code] ?? null
}

export function percent(task: TransferSnapshot): number {
  if (task.status === 'succeeded') return 100
  if (!task.total) return 0
  return Math.min(100, Math.round((task.sent / task.total) * 100))
}

const TERMINAL_STATUSES: TransferSnapshot['status'][] = [
  'succeeded',
  'partial',
  'failed',
  'cancelled',
]

const waiters = new Map<string, (snapshot: TransferSnapshot) => void>()

function upsert(snapshot: TransferSnapshot) {
  const index = tasks.value.findIndex(item => item.id === snapshot.id)
  if (index === -1) tasks.value = [snapshot, ...tasks.value]
  else tasks.value[index] = snapshot
  if (TERMINAL_STATUSES.includes(snapshot.status)) {
    streams.get(snapshot.id)?.close()
    streams.delete(snapshot.id)
    const waiter = waiters.get(snapshot.id)
    if (waiter) {
      waiters.delete(snapshot.id)
      waiter(snapshot)
    }
  }
}

/** Resolve once the task is terminal; polling covers a broken SSE stream. */
function waitForTerminal(id: string): Promise<TransferSnapshot> {
  const current = tasks.value.find(task => task.id === id)
  if (current && TERMINAL_STATUSES.includes(current.status)) {
    return Promise.resolve(current)
  }
  return new Promise(resolve => {
    let timer: ReturnType<typeof setInterval> | null = null
    const finish = (snapshot: TransferSnapshot) => {
      waiters.delete(id)
      if (timer) clearInterval(timer)
      resolve(snapshot)
    }
    waiters.set(id, finish)
    timer = setInterval(async () => {
      try {
        upsert(await getTransfer(id))
      } catch {
        // The local service may be restarting; keep waiting.
      }
    }, 2000)
  })
}

function watchTask(id: string) {
  if (streams.has(id)) return
  streams.set(
    id,
    subscribeTransfer(id, {
      onSnapshot: upsert,
      onError: async () => {
        // Fall back to a snapshot query; the stream reconnects on its own.
        try {
          upsert(await getTransfer(id))
        } catch {
          // ignore, the retry loop keeps trying
        }
      },
    }),
  )
}

export async function connectService() {
  serviceState.value = 'connecting'
  serviceMessage.value = '正在启动本机传输服务'
  if (!('__TAURI_INTERNALS__' in window)) {
    serviceState.value = 'error'
    serviceMessage.value = '浏览器预览模式：请用桌面应用连接传输服务'
    return
  }
  try {
    await ensureService()
    serviceState.value = 'ready'
    serviceMessage.value = '本机服务已连接'
    await loadConfig()
    void refreshLogs()
  } catch (error) {
    serviceState.value = 'error'
    serviceMessage.value = error instanceof Error ? error.message : '本机服务启动失败'
  }
}

export async function loadConfig() {
  try {
    const config = await getConfig()
    applyConfig(config)
  } catch {
    // First run or unreadable config: keep defaults.
  }
}

function applyConfig(config: SavedConfig) {
  devices.value = Array.isArray(config.devices) ? config.devices : []
  if (!devices.value.length && config.host) {
    // The backend migrates v0.1 configs, but stay usable if it did not.
    devices.value = [
      {
        id: 'default',
        name: config.username ? `${config.username}@${config.host}` : config.host,
        host: config.host,
        port: config.port,
        username: config.username,
        remote_dir: config.remote_dir,
        auth_method: config.auth_method,
        private_key_path: config.private_key_path,
      },
    ]
  }
  activeDeviceId.value = config.active_device_id || devices.value[0]?.id || ''
  const active = devices.value.find(device => device.id === activeDeviceId.value) ?? devices.value[0]
  if (active) {
    activeDeviceId.value = active.id
    applyDevice(active)
  } else {
    Object.assign(form, EMPTY_FORM)
  }
  hotkey.value = normalizeHotkey(config.hotkey_modifiers, config.hotkey_key)
  settings.logDir = config.log_dir ?? ''
  settings.logRetentionDays = config.log_retention_days ?? 14
  settings.closeToTray = config.close_to_tray ?? true
  schedules.value = Array.isArray(config.schedules) ? config.schedules : []
  void applyCloseToTray(settings.closeToTray)
  if (form.username) void refreshCredentialStatus()
}

/** Load one saved device into the editable form. */
function applyDevice(device: DeviceConfig) {
  form.deviceName = device.name || (device.username ? `${device.username}@${device.host}` : '')
  form.host = device.host
  form.port = device.port
  form.username = device.username
  form.remoteDir = device.remote_dir
  form.authMethod = device.auth_method
  form.privateKeyPath = device.private_key_path
  // A password typed for one device must never leak into another.
  form.password = ''
  form.rememberPassword = false
}

/** Project the current form onto the device shape that gets persisted. */
function deviceFromForm(): DeviceConfig {
  const host = form.host.trim()
  const username = form.username.trim()
  return {
    id: activeDeviceId.value || 'default',
    name: form.deviceName.trim() || (username ? `${username}@${host}` : host),
    host,
    port: Number(form.port) || 22,
    username,
    remote_dir: form.remoteDir.trim(),
    auth_method: form.authMethod,
    private_key_path: form.privateKeyPath.trim(),
  }
}

function savedConfig(value: Hotkey = hotkey.value): SavedConfig {
  const current = deviceFromForm()
  const merged = devices.value.map(device => (device.id === current.id ? current : device))
  const hasContent = Boolean(current.host || current.username || current.remote_dir)
  if (!merged.some(device => device.id === current.id) && (hasContent || merged.length > 0)) {
    merged.unshift(current)
  }
  return {
    host: current.host,
    port: current.port,
    username: current.username,
    remote_dir: current.remote_dir,
    auth_method: current.auth_method,
    private_key_path: current.private_key_path,
    hotkey_modifiers: [...value.modifiers],
    hotkey_key: value.key,
    devices: merged,
    active_device_id: merged.some(device => device.id === current.id) ? current.id : '',
    log_dir: settings.logDir.trim(),
    log_retention_days: Number(settings.logRetentionDays) || 0,
    schedules: schedules.value,
    close_to_tray: settings.closeToTray,
  }
}

function newDeviceId(): string {
  const existing = new Set(devices.value.map(device => device.id))
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const uuid = globalThis.crypto?.randomUUID?.()
    const candidate = uuid
      ? uuid.replace(/-/g, '').slice(0, 8)
      : Math.random().toString(16).slice(2, 10)
    if (candidate && !existing.has(candidate)) return candidate
  }
  return `device-${existing.size + 1}`
}

/** Switch the active device, saving the fields currently in the form first. */
export async function switchDevice(id: string) {
  if (id === activeDeviceId.value) return
  const target = devices.value.find(device => device.id === id)
  if (!target) return
  const previous = deviceFromForm()
  devices.value = devices.value.map(device =>
    device.id === previous.id ? previous : device,
  )
  activeDeviceId.value = id
  applyDevice(target)
  hostKey.value = null
  lastError.value = null
  await persistConfig()
  void refreshCredentialStatus()
}

/** Append an empty device and make it active so the user can fill it in. */
export async function addDevice() {
  const previous = deviceFromForm()
  devices.value = devices.value.map(device =>
    device.id === previous.id ? previous : device,
  )
  const id = newDeviceId()
  const device: DeviceConfig = {
    id,
    name: '',
    host: '',
    port: 22,
    username: '',
    remote_dir: '',
    auth_method: 'password',
    private_key_path: '',
  }
  devices.value = [...devices.value, device]
  activeDeviceId.value = id
  applyDevice(device)
  form.deviceName = '新的接收电脑'
  hostKey.value = null
  lastError.value = null
  passwordSaved.value = false
  await persistConfig()
}

/** Remove a device; passwords in the credential manager are left alone. */
export async function deleteDevice(id: string) {
  const remaining = devices.value.filter(device => device.id !== id)
  devices.value = remaining
  if (activeDeviceId.value === id) {
    const next = remaining[0] ?? null
    activeDeviceId.value = next?.id ?? ''
    if (next) {
      applyDevice(next)
    } else {
      Object.assign(form, EMPTY_FORM)
      passwordSaved.value = false
    }
  }
  hostKey.value = null
  await persistConfig()
  void refreshCredentialStatus()
}

export async function persistConfig(): Promise<string | null> {
  try {
    await putConfig(savedConfig())
    return null
  } catch (error) {
    return error instanceof Error ? error.message : '无法保存接收电脑设置'
  }
}

/** Persist the current form as a saved receiver and refresh the sidebar entry. */
export async function saveConnection(): Promise<string | null> {
  const host = form.host.trim()
  const username = form.username.trim()
  if (!host) return '请先填写对方电脑的 IP 地址'
  if (!username) return '请先填写对方电脑的用户名'
  if (!form.remoteDir.trim()) return '请先设置文件保存位置'

  const previous = devices.value.find(device => device.id === activeDeviceId.value)
  const current = deviceFromForm()
  const identityChanged = previous
    ? previous.host.trim() !== current.host || previous.port !== current.port
    : false

  devices.value = devices.value.map(device =>
    device.id === current.id ? current : device,
  )
  if (!devices.value.some(device => device.id === current.id)) {
    devices.value = [current, ...devices.value]
  }
  activeDeviceId.value = current.id
  // A trusted key belongs to host:port, so it must not survive an address change.
  if (identityChanged) hostKey.value = null

  const error = await persistConfig()
  if (error) return error

  if (form.authMethod === 'password' && form.rememberPassword && form.password) {
    try {
      await saveCredential(host, current.port, username, form.password)
      passwordSaved.value = true
    } catch (credentialError) {
      const reason = credentialError instanceof Error ? credentialError.message : '未知错误'
      return `连接已保存，但密码保存失败：${reason}`
    }
  }
  void refreshCredentialStatus(host, current.port, username)
  return null
}

/** Refresh the log directory listing shown in the settings panel. */
export async function refreshLogs() {
  try {
    logsInfo.value = await getLogs()
  } catch {
    logsInfo.value = null
  }
}

/** Persist log settings; the backend switches handlers and purges there. */
export async function applyLogSettings(): Promise<string | null> {
  try {
    await putConfig(savedConfig())
    await refreshLogs()
    return null
  } catch (error) {
    return error instanceof Error ? error.message : '无法保存日志设置'
  }
}

export async function openLogDirectory(): Promise<boolean> {
  try {
    return (await openLogDir()).opened
  } catch {
    return false
  }
}

function isDesktopRuntime(): boolean {
  return '__TAURI_INTERNALS__' in window
}

/** Mirror the saved close-to-tray preference into the Rust shell. */
async function applyCloseToTray(enabled: boolean) {
  if (!isDesktopRuntime()) return
  try {
    await invoke('set_close_to_tray', { enabled })
  } catch {
    // Older shells may not expose the command; the UI default still applies.
  }
}

export async function refreshAutostart() {
  if (!isDesktopRuntime()) return
  try {
    autostartEnabled.value = await invoke<boolean>('autostart_status')
  } catch {
    autostartEnabled.value = false
  }
}

export async function setAutostart(enabled: boolean): Promise<string | null> {
  if (!isDesktopRuntime()) return '仅桌面版支持开机自启'
  try {
    autostartEnabled.value = await invoke<boolean>('set_autostart', { enabled })
    return null
  } catch (error) {
    return typeof error === 'string' ? error : '无法修改开机自启'
  }
}

/** Persist window behaviour and push it to the shell immediately. */
export async function updateSystemSettings(): Promise<string | null> {
  const error = await persistConfig()
  if (!error) await applyCloseToTray(settings.closeToTray)
  return error
}

function todayIso(): string {
  const now = new Date()
  const month = String(now.getMonth() + 1).padStart(2, '0')
  const day = String(now.getDate()).padStart(2, '0')
  return `${now.getFullYear()}-${month}-${day}`
}

function newScheduleId(): string {
  const existing = new Set(schedules.value.map(item => item.id))
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const uuid = globalThis.crypto?.randomUUID?.()
    const candidate = uuid
      ? uuid.replace(/-/g, '').slice(0, 8)
      : Math.random().toString(16).slice(2, 10)
    if (candidate && !existing.has(candidate)) return candidate
  }
  return `schedule-${existing.size + 1}`
}

export function resetScheduleDraft() {
  scheduleDraft.name = ''
  scheduleDraft.sourcePath = ''
  scheduleDraft.recursive = false
  scheduleDraft.deviceId = activeDeviceId.value || devices.value[0]?.id || ''
  scheduleDraft.remoteDir = ''
  scheduleDraft.overwrite = false
  scheduleDraft.mode = 'daily'
  scheduleDraft.time = '09:00'
  scheduleDraft.date = todayIso()
  scheduleDraft.weekdays = []
}

/** Add the draft as a schedule; the backend mirrors it into Task Scheduler. */
export async function addSchedule(): Promise<string | null> {
  const source = scheduleDraft.sourcePath.trim()
  if (!source) return '请先选择要发送的文件或文件夹'
  if (!scheduleDraft.deviceId) return '请选择目标设备'
  if (scheduleDraft.mode === 'once' && !scheduleDraft.date) return '一次性任务需要选择日期'
  if (scheduleDraft.mode === 'weekly' && !scheduleDraft.weekdays.length) {
    return '每周任务至少选择一天'
  }

  const item: ScheduleConfig = {
    id: newScheduleId(),
    name: scheduleDraft.name.trim() || fileName(source) || '定时任务',
    enabled: true,
    source_path: source,
    recursive: scheduleDraft.recursive,
    device_id: scheduleDraft.deviceId,
    remote_dir: scheduleDraft.remoteDir.trim(),
    overwrite: scheduleDraft.overwrite,
    mode: scheduleDraft.mode,
    time: scheduleDraft.time || '09:00',
    date: scheduleDraft.mode === 'once' ? scheduleDraft.date : '',
    weekdays:
      scheduleDraft.mode === 'weekly'
        ? [...scheduleDraft.weekdays].sort((left, right) => left - right)
        : [],
    last_run_at: '',
    last_run_status: '',
  }

  schedules.value = [...schedules.value, item]
  const error = await persistConfig()
  if (error) {
    schedules.value = schedules.value.filter(entry => entry.id !== item.id)
    return error
  }
  resetScheduleDraft()
  await refreshScheduleStatus()
  return null
}

export async function deleteSchedule(id: string): Promise<string | null> {
  schedules.value = schedules.value.filter(item => item.id !== id)
  const error = await persistConfig()
  await refreshScheduleStatus()
  return error
}

export async function toggleSchedule(id: string): Promise<string | null> {
  schedules.value = schedules.value.map(item =>
    item.id === id ? { ...item, enabled: !item.enabled } : item,
  )
  const error = await persistConfig()
  await refreshScheduleStatus()
  return error
}

/** Next run / missed flag for every rule, computed by the backend. */
export async function refreshScheduleStatus() {
  try {
    const result = await getScheduleStatus()
    const mapped: Record<string, ScheduleStatus> = {}
    for (const item of result.schedules) mapped[item.id] = item
    scheduleStatus.value = mapped
    systemTasksSupported.value = result.system_tasks
    // The scheduler writes run state straight to config.json; fold it into the
    // local list so the rows (and the next save) stop showing stale values.
    schedules.value = schedules.value.map(item => {
      const status = mapped[item.id]
      if (!status) return item
      const retired =
        item.mode === 'once' && Boolean(status.last_run_at) && !status.enabled
      return {
        ...item,
        last_run_at: status.last_run_at ?? item.last_run_at,
        last_run_status: status.last_run_status ?? item.last_run_status,
        enabled: retired ? false : item.enabled,
      }
    })
  } catch {
    scheduleStatus.value = {}
  }
}

const MAX_TASKS = 60

/**
 * Merge backend-side tasks (including scheduler-dispatched ones) into the list.
 * Without this, a timed run transfers the file but the UI never shows it.
 */
export async function refreshTasks() {
  try {
    const result = await listTransfers()
    for (const snapshot of result.tasks) {
      upsert(snapshot)
      if (!TERMINAL_STATUSES.includes(snapshot.status)) watchTask(snapshot.id)
    }
    tasks.value = [...tasks.value]
      .sort((left, right) => (right.created_at ?? 0) - (left.created_at ?? 0))
      .slice(0, MAX_TASKS)
  } catch {
    // The service may be restarting; the next tick retries.
  }
}

/** One polling tick: task list plus schedule run state. */
export async function syncLiveState() {
  await Promise.all([refreshTasks(), refreshScheduleStatus()])
}

/** Fire a schedule immediately through the normal task list. */
export async function runScheduleNow(id: string): Promise<string | null> {
  try {
    const task = await runSchedule(id)
    activeTaskId.value = task.id
    upsert(task)
    watchTask(task.id)
    await refreshScheduleStatus()
    return null
  } catch (error) {
    return error instanceof Error ? error.message : '无法执行定时任务'
  }
}

/** Persist only the hotkey, leaving the connection fields untouched. */
export async function saveHotkey(next: Hotkey): Promise<string | null> {
  hotkey.value = { modifiers: [...next.modifiers], key: next.key }
  try {
    await putConfig(savedConfig(next))
    return null
  } catch (error) {
    return error instanceof Error ? error.message : '无法保存快捷键'
  }
}

/** Check whether this exact host:port/user pair has a stored password. */
export async function refreshCredentialStatus(
  host = form.host.trim(),
  port = Number(form.port) || 22,
  username = form.username.trim(),
) {
  if (!host || !username) {
    passwordSaved.value = false
    return
  }
  try {
    passwordSaved.value = (await credentialStatus(host, port, username)).saved
  } catch {
    passwordSaved.value = false
  }
}

export async function checkHostKey() {
  if (!form.host.trim()) {
    lastError.value = '请先填写对方电脑的 IP 地址'
    return
  }
  busy.value = true
  lastError.value = null
  try {
    hostKey.value = await probeHostKey(form.host.trim(), Number(form.port) || 22)
  } catch (error) {
    hostKey.value = null
    lastError.value = error instanceof Error ? error.message : '无法验证对方电脑身份'
  } finally {
    busy.value = false
  }
}

export async function confirmHostKey() {
  if (!hostKey.value) return
  busy.value = true
  try {
    hostKey.value = await trustHostKey(
      hostKey.value.host,
      hostKey.value.port,
      hostKey.value.fingerprint,
    )
  } catch (error) {
    lastError.value = error instanceof Error ? error.message : '无法保存身份验证结果'
  } finally {
    busy.value = false
  }
}

/** Build the request for one source; folders switch the backend to tree mode. */
export function transferPayload(source: LocalSource & { overwrite?: boolean }): TransferRequest {
  return {
    host: form.host.trim(),
    port: Number(form.port) || 22,
    username: form.username.trim(),
    remote_dir: form.remoteDir.trim(),
    local_path: source.path,
    auth_method: form.authMethod,
    password: form.authMethod === 'password' ? form.password || undefined : undefined,
    private_key_path: form.authMethod === 'key' ? form.privateKeyPath.trim() || null : null,
    overwrite: source.overwrite ?? false,
    recursive: source.kind === 'folder',
    remember_password: form.authMethod === 'password' ? form.rememberPassword : false,
  }
}

/** Send the queue one source at a time; one failure never stops the rest. */
export async function sendQueue() {
  if (!canSend.value) return
  const pending = [...queue.value]
  if (!pending.length) return

  busy.value = true
  lastError.value = null
  batchMessage.value = null
  batch.value = {
    running: true,
    total: pending.length,
    done: 0,
    ok: 0,
    partial: 0,
    failed: 0,
    cancelled: false,
  }

  try {
    const savedError = await persistConfig()
    if (savedError) lastError.value = savedError

    if (form.authMethod === 'password' && form.rememberPassword && form.password) {
      await saveCredential(
        form.host.trim(),
        Number(form.port) || 22,
        form.username.trim(),
        form.password,
      )
      passwordSaved.value = true
    }

    for (const item of pending) {
      if (batch.value.cancelled) break
      try {
        const task = await createTransfer(transferPayload(item))
        activeTaskId.value = task.id
        upsert(task)
        watchTask(task.id)
        const finalSnapshot = await waitForTerminal(task.id)

        item.error = finalSnapshot.error
        item.errorCode = finalSnapshot.error_code
        batch.value.done += 1

        if (finalSnapshot.status === 'succeeded') {
          batch.value.ok += 1
          queue.value = queue.value.filter(entry => entry.key !== item.key)
        } else if (finalSnapshot.status === 'partial') {
          batch.value.partial += 1
          queue.value = queue.value.filter(entry => entry.key !== item.key)
        } else if (finalSnapshot.status === 'cancelled') {
          batch.value.cancelled = true
        } else {
          batch.value.failed += 1
        }
      } catch (error) {
        item.error = error instanceof Error ? error.message : '无法创建传输任务'
        item.errorCode = 'request_failed'
        batch.value.failed += 1
        batch.value.done += 1
      }
    }
  } finally {
    batch.value.running = false
    busy.value = false
    const parts = [`成功 ${batch.value.ok}`]
    if (batch.value.partial) parts.push(`部分完成 ${batch.value.partial}`)
    if (batch.value.failed) parts.push(`失败 ${batch.value.failed}`)
    batchMessage.value = (batch.value.cancelled ? '已取消 · ' : '') + parts.join(' · ')
  }
}

/** Stop dispatching and cancel whatever is in flight. */
export async function cancelQueue() {
  if (!batch.value.running) return
  batch.value.cancelled = true
  await cancelActive()
}

/** Retry every failed item that only failed because the target exists. */
export async function retryFailedWithOverwrite() {
  if (batch.value.running) return
  let marked = false
  for (const item of queue.value) {
    if (item.errorCode === 'target_exists') {
      item.overwrite = true
      item.error = null
      item.errorCode = null
      marked = true
    }
  }
  if (!marked) return
  await sendQueue()
}

export async function cancelActive() {
  if (!activeTaskId.value) return
  try {
    upsert(await cancelTransfer(activeTaskId.value))
  } catch (error) {
    lastError.value = error instanceof Error ? error.message : '取消失败'
  }
}

export async function retryActive() {
  if (!activeTaskId.value) return
  busy.value = true
  try {
    const task = await retryTransfer(
      activeTaskId.value,
      form.authMethod === 'password' ? form.password || undefined : undefined,
    )
    activeTaskId.value = task.id
    upsert(task)
    watchTask(task.id)
  } catch (error) {
    lastError.value = error instanceof Error ? error.message : '重试失败'
  } finally {
    busy.value = false
  }
}

export function selectTask(id: string) {
  activeTaskId.value = id
  watchTask(id)
}

export function resetService() {
  resetServiceCache()
  serviceState.value = 'connecting'
}

export function isServiceError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401
}
