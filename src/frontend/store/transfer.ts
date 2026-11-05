import { computed, reactive, ref } from 'vue'
import {
  ApiError,
  cancelTransfer,
  createTransfer,
  credentialStatus,
  ensureService,
  getConfig,
  getTransfer,
  probeHostKey,
  putConfig,
  resetServiceCache,
  retryTransfer,
  saveCredential,
  subscribeTransfer,
  trustHostKey,
  type HostKeyInfo,
  type SavedConfig,
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
  host: '',
  port: 22,
  username: '',
  remoteDir: '',
  authMethod: 'key',
  privateKeyPath: '',
  password: '',
  rememberPassword: false,
}

export const serviceState = ref<ServiceState>('connecting')
export const serviceMessage = ref('正在启动本机传输服务')
export const form = reactive<ConnectionForm>({ ...EMPTY_FORM })
export const hotkey = ref<Hotkey>({ ...DEFAULT_HOTKEY, modifiers: [...DEFAULT_HOTKEY.modifiers] })
export const tasks = ref<TransferSnapshot[]>([])
export const activeTaskId = ref<string | null>(null)
export const localFile = ref<string | null>(null)
export const hostKey = ref<HostKeyInfo | null>(null)
export const passwordSaved = ref(false)
export const busy = ref(false)
export const lastError = ref<string | null>(null)

const streams = new Map<string, { close: () => void }>()

export const activeTask = computed(
  () => tasks.value.find(task => task.id === activeTaskId.value) ?? null,
)

export const isLocked = computed(() => {
  const task = activeTask.value
  return task !== null && (task.status === 'running' || task.status === 'queued')
})

export const canSend = computed(() => {
  const task = activeTask.value
  const finished = !task || task.status === 'succeeded' || task.status === 'failed' || task.status === 'cancelled'
  return Boolean(
    localFile.value &&
      form.host.trim() &&
      form.username.trim() &&
      form.remoteDir.trim() &&
      finished &&
      !busy.value &&
      serviceState.value === 'ready',
  )
})

export function fileName(path: string | null): string {
  return path?.split(/[\\/]/).pop() ?? ''
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
  if (task.status === 'failed') return '失败'
  if (task.status === 'cancelled') return '已取消'
  if (task.status === 'queued') return '排队中'
  switch (task.phase) {
    case 'connecting':
      return '连接中'
    case 'checking':
      return '检查续传'
    case 'transferring':
      return '传输中'
    case 'verifying':
      return '校验中'
    default:
      return '处理中'
  }
}

export function percent(task: TransferSnapshot): number {
  if (task.status === 'succeeded') return 100
  if (!task.total) return 0
  return Math.min(100, Math.round((task.sent / task.total) * 100))
}

function upsert(snapshot: TransferSnapshot) {
  const index = tasks.value.findIndex(item => item.id === snapshot.id)
  if (index === -1) tasks.value = [snapshot, ...tasks.value]
  else tasks.value[index] = snapshot
  if (snapshot.status === 'succeeded' || snapshot.status === 'failed' || snapshot.status === 'cancelled') {
    streams.get(snapshot.id)?.close()
    streams.delete(snapshot.id)
  }
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
  form.host = config.host
  form.port = config.port
  form.username = config.username
  form.remoteDir = config.remote_dir
  form.authMethod = config.auth_method
  form.privateKeyPath = config.private_key_path
  hotkey.value = normalizeHotkey(config.hotkey_modifiers, config.hotkey_key)
  if (config.username) void refreshCredentialStatus(config.username)
}

function savedConfig(value: Hotkey = hotkey.value): SavedConfig {
  return {
    host: form.host.trim(),
    port: Number(form.port) || 22,
    username: form.username.trim(),
    remote_dir: form.remoteDir.trim(),
    auth_method: form.authMethod,
    private_key_path: form.privateKeyPath.trim(),
    hotkey_modifiers: [...value.modifiers],
    hotkey_key: value.key,
  }
}

export async function persistConfig(): Promise<string | null> {
  try {
    await putConfig(savedConfig())
    return null
  } catch (error) {
    return error instanceof Error ? error.message : '无法保存连接配置'
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

export async function refreshCredentialStatus(username = form.username.trim()) {
  if (!username) {
    passwordSaved.value = false
    return
  }
  try {
    passwordSaved.value = (await credentialStatus(username)).saved
  } catch {
    passwordSaved.value = false
  }
}

export async function checkHostKey() {
  if (!form.host.trim()) {
    lastError.value = '请先填写远端主机地址'
    return
  }
  busy.value = true
  lastError.value = null
  try {
    hostKey.value = await probeHostKey(form.host.trim(), Number(form.port) || 22)
  } catch (error) {
    hostKey.value = null
    lastError.value = error instanceof Error ? error.message : '无法读取主机指纹'
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
    lastError.value = error instanceof Error ? error.message : '无法保存主机指纹'
  } finally {
    busy.value = false
  }
}

export async function send() {
  if (!localFile.value || !canSend.value) return
  busy.value = true
  lastError.value = null
  try {
    const savedError = await persistConfig()
    if (savedError) lastError.value = savedError

    if (form.authMethod === 'password' && form.rememberPassword && form.password) {
      await saveCredential(form.username.trim(), form.password)
      passwordSaved.value = true
    }

    const task = await createTransfer({
      host: form.host.trim(),
      port: Number(form.port) || 22,
      username: form.username.trim(),
      remote_dir: form.remoteDir.trim(),
      local_path: localFile.value,
      auth_method: form.authMethod,
      password: form.authMethod === 'password' ? form.password || undefined : undefined,
      private_key_path: form.authMethod === 'key' ? form.privateKeyPath.trim() || null : null,
      overwrite: false,
      remember_password: form.authMethod === 'password' ? form.rememberPassword : false,
    })
    activeTaskId.value = task.id
    upsert(task)
    watchTask(task.id)
  } catch (error) {
    lastError.value = error instanceof Error ? error.message : '无法创建传输任务'
  } finally {
    busy.value = false
  }
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
