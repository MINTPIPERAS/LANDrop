<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { open } from '@tauri-apps/plugin-dialog'
import { isRegistered, register, unregister } from '@tauri-apps/plugin-global-shortcut'
import { invoke } from '@tauri-apps/api/core'
import {
  AlertCircle,
  ArrowDownToLine,
  CalendarClock,
  CheckCircle2,
  ChevronDown,
  CircleHelp,
  File,
  FileText,
  Folder,
  FolderOpen,
  KeyRound,
  Laptop,
  LockKeyhole,
  Plus,
  RefreshCw,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  Upload,
  Wifi,
  X,
} from '@lucide/vue'
import {
  activeDeviceId,
  activeTask,
  addDevice,
  addFiles,
  addFolder,
  addSchedule,
  applyLogSettings,
  autostartEnabled,
  batch,
  batchMessage,
  buildHotkey,
  busy,
  canSend,
  cancelActive,
  cancelQueue,
  checkHostKey,
  clearQueue,
  confirmHostKey,
  connectService,
  DEFAULT_HOTKEY,
  deleteDevice,
  deleteSchedule,
  devices,
  errorHint,
  fileName,
  form,
  formatBytes,
  formatSpeed,
  hasTargetConflicts,
  HOTKEY_MODIFIERS,
  hotkey,
  hotkeyAccelerator,
  hotkeyLabel,
  hostKey,
  isLocked,
  lastError,
  logsInfo,
  openLogDirectory,
  passwordSaved,
  percent,
  phaseLabel,
  queue,
  queueSummary,
  refreshAutostart,
  refreshLogs,
  refreshScheduleStatus,
  removeSource,
  resetScheduleDraft,
  retryActive,
  retryFailedWithOverwrite,
  runScheduleNow,
  saveConnection,
  saveHotkey,
  scheduleDraft,
  scheduleStatus,
  schedules,
  selectTask,
  sendBlockReason,
  sendQueue,
  serviceMessage,
  serviceState,
  setAutostart,
  settings,
  switchDevice,
  syncLiveState,
  systemTasksSupported,
  tasks,
  toggleSchedule,
  updateSystemSettings,
} from './store/transfer'

const hotkeyStatus = ref('快捷键未注册')
const showConnectionForm = ref(false)
const showHotkeyEditor = ref(false)
const showLogSettings = ref(false)
const logHint = ref<string | null>(null)
const showSchedulePanel = ref(false)
const scheduleHint = ref<string | null>(null)
const showSystemSettings = ref(false)
const systemHint = ref<string | null>(null)
const showHelpPanel = ref(false)
const connectionHint = ref<string | null>(null)
const connectionHintError = ref(false)
const connectionSection = ref<HTMLElement | null>(null)
const WEEKDAY_LABELS = ['一', '二', '三', '四', '五', '六', '日']
const draftModifiers = ref<string[]>([...DEFAULT_HOTKEY.modifiers])
const draftKey = ref(DEFAULT_HOTKEY.key)
let registeredAccelerator: string | null = null
let liveTimer: number | null = null

const isDesktop = '__TAURI_INTERNALS__' in window
const selectedTask = computed(() => activeTask.value ?? tasks.value[0] ?? null)
const recentTasks = computed(() => tasks.value.slice(0, 8))
const totalSent = computed(() =>
  tasks.value
    .filter(task => task.status === 'succeeded')
    .reduce((sum, task) => sum + (task.result?.size ?? 0), 0),
)

const serviceLabel = computed(() => {
  if (serviceState.value === 'ready') return '应用已就绪'
  if (serviceState.value === 'connecting') return '正在启动…'
  return '启动失败'
})

const setupComplete = computed(() =>
  Boolean(form.host.trim() && form.username.trim() && form.remoteDir.trim()),
)

const currentStep = computed(() => {
  if (isLocked.value) return 3
  if (!queue.value.length) return 1
  if (!setupComplete.value) return 2
  return 3
})

// Clear the "saved" note as soon as the user edits any connection field.
watch(form, () => {
  connectionHint.value = null
  connectionHintError.value = false
}, { deep: true })

function tone(task: { status: string }): 'green' | 'blue' | 'amber' | 'red' {
  if (task.status === 'succeeded') return 'green'
  if (task.status === 'partial') return 'amber'
  if (task.status === 'failed') return 'red'
  if (task.status === 'cancelled') return 'amber'
  return 'blue'
}

async function chooseFile() {
  if (isLocked.value) return
  if (!isDesktop) {
    addFiles(['示例文件_仅供预览.zip'])
    return
  }
  const selected = await open({ multiple: true, directory: false })
  const paths = typeof selected === 'string' ? [selected] : Array.isArray(selected) ? selected : []
  addFiles(paths)
}

async function chooseFolder() {
  if (isLocked.value) return
  if (!isDesktop) {
    queue.value.push({
      key: 'preview-folder',
      path: '示例文件夹_仅供预览',
      kind: 'folder',
      size: 0,
      fileCount: 0,
    })
    return
  }
  const selected = await open({ directory: true })
  if (typeof selected !== 'string') return
  const error = await addFolder(selected)
  if (error) lastError.value = error
}

async function choosePrivateKey() {
  if (!isDesktop) return
  const path = await open({ multiple: false, directory: false })
  if (typeof path === 'string') form.privateKeyPath = path
}

/** Open the receiver form and bring it into view; it sits above the send panel. */
function openConnectionPanel() {
  showConnectionForm.value = true
  void nextTick(() => {
    connectionSection.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  })
}

async function addReceiver() {
  await addDevice()
  openConnectionPanel()
}

async function saveActiveConnection() {
  busy.value = true
  const error = await saveConnection()
  busy.value = false
  connectionHintError.value = Boolean(error)
  connectionHint.value = error ?? '已保存，下次打开可以直接使用'
}

async function openHelpLogs() {
  const opened = await openLogDirectory()
  if (!opened) lastError.value = '无法打开日志目录'
}

function removeActiveDevice() {
  if (!activeDeviceId.value) return
  const confirmed = window.confirm('删除这台电脑？删除后它会从列表中移除，已保存的密码不会被删除。')
  if (!confirmed) return
  void deleteDevice(activeDeviceId.value)
}

function toggleLogSettings() {
  showLogSettings.value = !showLogSettings.value
  if (showLogSettings.value) void refreshLogs()
}

async function chooseLogDir() {
  if (!isDesktop) return
  const selected = await open({ directory: true })
  if (typeof selected === 'string') settings.logDir = selected
}

function resetLogDir() {
  settings.logDir = ''
}

async function saveLogSettings() {
  busy.value = true
  const error = await applyLogSettings()
  busy.value = false
  if (error) {
    logHint.value = error
    return
  }
  logHint.value = logsInfo.value ? `已保存，当前目录：${logsInfo.value.dir}` : '已保存'
}

async function openLogFolder() {
  const opened = await openLogDirectory()
  if (!opened) logHint.value = '无法打开日志目录'
}

function toggleSchedulePanel() {
  showSchedulePanel.value = !showSchedulePanel.value
  if (!showSchedulePanel.value) return
  if (!scheduleDraft.deviceId) resetScheduleDraft()
  void refreshScheduleStatus()
}

async function chooseScheduleSource() {
  if (!isDesktop) return
  const selected = await open({ multiple: false, directory: false })
  if (typeof selected === 'string') {
    scheduleDraft.sourcePath = selected
    scheduleDraft.recursive = false
  }
}

async function chooseScheduleFolder() {
  if (!isDesktop) return
  const selected = await open({ directory: true })
  if (typeof selected === 'string') {
    scheduleDraft.sourcePath = selected
    scheduleDraft.recursive = true
  }
}

function toggleDraftWeekday(day: number) {
  const index = scheduleDraft.weekdays.indexOf(day)
  if (index >= 0) scheduleDraft.weekdays.splice(index, 1)
  else scheduleDraft.weekdays.push(day)
}

function deviceLabel(id: string): string {
  const device = devices.value.find(item => item.id === id)
  return device ? device.name || device.host : '设备已删除'
}

function scheduleModeLabel(item: {
  mode: string
  time: string
  weekdays: number[]
  date: string
}): string {
  if (item.mode === 'once') return `${item.date} ${item.time}`
  if (item.mode === 'daily') return `每天 ${item.time}`
  const days = item.weekdays.map(day => `周${WEEKDAY_LABELS[day] ?? day}`).join('、')
  return `${days || '每周'} ${item.time}`
}

function scheduleNextLabel(id: string): string {
  const status = scheduleStatus.value[id]
  if (!status) return '—'
  if (status.missed) return '上次未执行'
  return status.next_run_at ? status.next_run_at.replace('T', ' ').slice(0, 16) : '—'
}

function scheduleStatusLabel(status: string): string {
  switch (status) {
    case 'succeeded':
      return '成功'
    case 'partial':
      return '部分完成'
    case 'failed':
      return '失败'
    case 'cancelled':
      return '已取消'
    case 'running':
      return '执行中'
    default:
      return '未执行'
  }
}

/** Prefer the polled status over the snapshot loaded at startup. */
function scheduleLastRun(item: {
  id: string
  last_run_at: string
  last_run_status: string
}): { at: string; status: string } {
  const status = scheduleStatus.value[item.id]
  return {
    at: status?.last_run_at ?? item.last_run_at,
    status: status?.last_run_status ?? item.last_run_status,
  }
}

async function submitSchedule() {
  scheduleHint.value = null
  const error = await addSchedule()
  scheduleHint.value = error ?? '已添加'
}

async function removeScheduleItem(id: string) {
  const confirmed = window.confirm('删除这个定时任务？系统里的计划任务也会一并移除。')
  if (!confirmed) return
  const error = await deleteSchedule(id)
  if (error) scheduleHint.value = error
}

async function toggleScheduleEnabled(id: string) {
  const error = await toggleSchedule(id)
  if (error) scheduleHint.value = error
}

async function runScheduleItem(id: string) {
  scheduleHint.value = null
  const error = await runScheduleNow(id)
  if (error) scheduleHint.value = error
}

function toggleSystemSettings() {
  showSystemSettings.value = !showSystemSettings.value
}

async function saveSystemSettings() {
  const error = await updateSystemSettings()
  systemHint.value = error ?? '已保存'
}

async function toggleAutostart(event: Event) {
  const enabled = (event.target as HTMLInputElement).checked
  const error = await setAutostart(enabled)
  if (error) {
    autostartEnabled.value = !enabled
    systemHint.value = error
    return
  }
  systemHint.value = enabled ? '已开启开机自启，下次登录生效' : '已关闭开机自启'
}

async function toggleHotkey() {
  if (!isDesktop) {
    hotkeyStatus.value = '仅桌面版支持快捷键'
    return
  }
  try {
    if (registeredAccelerator && (await isRegistered(registeredAccelerator))) {
      await unregister(registeredAccelerator)
      registeredAccelerator = null
      hotkeyStatus.value = '快捷键已关闭'
      return
    }
    await registerHotkey()
  } catch {
    hotkeyStatus.value = '快捷键被占用'
  }
}

async function registerHotkey(): Promise<boolean> {
  const accelerator = hotkeyAccelerator()
  try {
    await register(accelerator, () => void invoke('show_main_window'))
  } catch {
    registeredAccelerator = null
    hotkeyStatus.value = '快捷键被占用'
    return false
  }
  registeredAccelerator = accelerator
  hotkeyStatus.value = hotkeyLabel()
  return true
}

async function applyHotkey() {
  const next = buildHotkey(draftModifiers.value, draftKey.value)
  if (!next) {
    lastError.value = '快捷键需要至少一个修饰键，且按键不能包含空格或加号'
    return
  }
  const previous = { modifiers: [...hotkey.value.modifiers], key: hotkey.value.key }
  const previousStatus = hotkeyStatus.value
  const previousAccelerator = registeredAccelerator

  if (isDesktop && previousAccelerator) {
    try {
      await unregister(previousAccelerator)
    } catch {
      // The old accelerator may already be gone; keep going.
    }
    registeredAccelerator = null
  }

  hotkey.value = next
  if (isDesktop) {
    const ok = await registerHotkey()
    if (!ok) {
      hotkey.value = previous
      lastError.value = `${hotkeyLabel(next)} 已被其他程序占用，已保留原快捷键`
      if (previousAccelerator) {
        try {
          await register(previousAccelerator, () => void invoke('show_main_window'))
          registeredAccelerator = previousAccelerator
          hotkeyStatus.value = previousStatus
        } catch {
          registeredAccelerator = null
          hotkeyStatus.value = '快捷键未注册'
        }
      }
      return
    }
  }

  const saveError = await saveHotkey(next)
  if (saveError) {
    lastError.value = saveError
    return
  }
  lastError.value = null
  showHotkeyEditor.value = false
}

function toggleModifier(modifier: string) {
  const index = draftModifiers.value.indexOf(modifier)
  if (index >= 0) draftModifiers.value.splice(index, 1)
  else draftModifiers.value.push(modifier)
}

function openHotkeyEditor() {
  if (!isDesktop) {
    hotkeyStatus.value = '仅桌面版支持快捷键'
    return
  }
  draftModifiers.value = [...hotkey.value.modifiers]
  draftKey.value = hotkey.value.key
  showHotkeyEditor.value = !showHotkeyEditor.value
}

onMounted(async () => {
  await connectService()
  resetScheduleDraft()
  void refreshAutostart()
  // Scheduled runs are created inside the service; polling is how the UI learns
  // about them and keeps "上次运行" fresh.
  void syncLiveState()
  liveTimer = window.setInterval(() => {
    if (document.visibilityState === 'visible') void syncLiveState()
  }, 3000)
  if (!isDesktop) return
  draftModifiers.value = [...hotkey.value.modifiers]
  draftKey.value = hotkey.value.key
  try {
    if (!(await isRegistered(hotkeyAccelerator()))) await registerHotkey()
  } catch {
    hotkeyStatus.value = '快捷键被占用，可在左下角重新设置'
  }
})

onUnmounted(() => {
  if (liveTimer !== null) {
    window.clearInterval(liveTimer)
    liveTimer = null
  }
  if (registeredAccelerator) void unregister(registeredAccelerator)
})
</script>

<template>
  <div class="app-frame">
    <aside class="sidebar">
      <div class="brand-row">
        <div class="brand-mark"><ArrowDownToLine :size="18" stroke-width="2.4" /></div>
        <span class="brand-name">LANDrop</span>
      </div>

      <div class="side-section-label">工作区</div>
      <button class="nav-item selected">
        <ArrowDownToLine :size="17" /><span>传输任务</span>
        <span class="nav-count">{{ tasks.length }}</span>
      </button>

      <div class="side-section-heading">
        <span class="side-section-label">接收电脑</span>
        <span class="side-heading-actions">
          <button class="icon-button small" title="添加接收电脑" aria-label="添加接收电脑" @click="addReceiver">
            <Plus :size="15" />
          </button>
          <button class="icon-button small" title="接收电脑设置" aria-label="接收电脑设置" @click="openConnectionPanel">
            <Settings2 :size="16" />
          </button>
        </span>
      </div>
      <div class="connection-list">
        <button
          v-for="device in devices"
          :key="device.id"
          class="connection-item"
          :class="{ active: device.id === activeDeviceId }"
          @click="switchDevice(device.id)"
        >
          <span class="connection-copy">
            <span class="connection-name">{{ device.name || device.host || '未命名接收电脑' }}</span>
            <span class="connection-host">{{ device.host || '未填写 IP 地址' }}</span>
          </span>
          <span v-if="device.id === activeDeviceId" class="active-tag">当前</span>
        </button>
        <div v-if="!devices.length" class="connection-empty">还没有接收电脑</div>
      </div>

      <div class="sidebar-bottom">
        <div v-if="showHotkeyEditor" class="hotkey-editor">
          <div class="hotkey-editor-title">
            <span>全局快捷键</span>
            <button class="inline-action" @click="showHotkeyEditor = false">关闭</button>
          </div>
          <div class="hotkey-mods">
            <button
              v-for="modifier in HOTKEY_MODIFIERS"
              :key="modifier"
              class="hotkey-mod"
              :class="{ active: draftModifiers.includes(modifier) }"
              @click="toggleModifier(modifier)"
            >{{ modifier === 'super' ? 'Win' : modifier === 'ctrl' ? 'Ctrl' : modifier === 'alt' ? 'Alt' : 'Shift' }}</button>
          </div>
          <input
            v-model="draftKey"
            class="hotkey-key-input"
            maxlength="24"
            placeholder="space / f9 / k"
            @keydown.stop
          />
          <div class="hotkey-editor-foot">
            <code>{{ hotkeyLabel({ modifiers: draftModifiers, key: draftKey }) }}</code>
            <button class="inline-action trust" @click="applyHotkey">应用</button>
          </div>
        </div>
        <div class="privacy-note">
          <ShieldCheck :size="15" /><span>加密传输 · 只在你和对方电脑之间</span>
        </div>
        <button class="profile-row" title="点击开启/关闭全局快捷键" @click="toggleHotkey">
          <div class="avatar">L</div>
          <span class="profile-copy"><strong>全局快捷键</strong><small>{{ hotkeyStatus }}</small></span>
          <Settings2 :size="16" class="muted-icon" />
        </button>
        <button class="hotkey-config-row" @click="openHotkeyEditor">
          <Settings2 :size="14" /><span>设置全局快捷键</span>
        </button>
        <button class="hotkey-config-row" @click="toggleLogSettings">
          <FileText :size="14" /><span>日志设置</span>
        </button>
        <button class="hotkey-config-row" @click="toggleSchedulePanel">
          <CalendarClock :size="14" /><span>定时传输</span>
        </button>
        <button class="hotkey-config-row" @click="toggleSystemSettings">
          <Settings2 :size="14" /><span>系统设置</span>
        </button>
      </div>
    </aside>

    <main class="main-area">
      <header class="topbar">
        <div class="breadcrumbs"><span>LANDrop</span><span class="crumb-divider">/</span><strong>传输任务</strong></div>
        <div class="top-actions">
          <div class="local-status" :title="serviceMessage">
            <span class="pulse-dot" :class="{ online: serviceState === 'ready' }"></span>{{ serviceLabel }}
          </div>
          <button class="icon-button top-hotkey" title="全局快捷键" aria-label="全局快捷键" @click="toggleHotkey">
            <Settings2 :size="16" />
          </button>
          <button
            class="icon-button"
            :class="{ active: showHelpPanel }"
            title="使用帮助"
            aria-label="使用帮助"
            :aria-expanded="showHelpPanel"
            @click="showHelpPanel = !showHelpPanel"
          ><CircleHelp :size="17" /></button>
        </div>
      </header>

      <div class="content-wrap">
        <section class="page-heading">
          <div>
            <div class="eyebrow">局域网文件传输</div>
            <h1>传输任务</h1>
            <p>把文件发送到同一网络里的另一台电脑。第一次使用请先添加接收电脑。</p>
          </div>
        </section>

        <section v-if="showHelpPanel" class="send-panel help-panel">
          <div class="panel-heading">
            <div class="panel-title-group">
              <span class="panel-icon"><CircleHelp :size="17" /></span>
              <div><h2>使用帮助</h2><p>三步完成文件发送</p></div>
            </div>
            <div class="panel-heading-actions">
              <button class="inline-action" @click="openHelpLogs">打开日志目录</button>
              <button class="text-button" title="收起" @click="showHelpPanel = false"><ChevronDown :size="18" /></button>
            </div>
          </div>
          <ol class="help-steps">
            <li><span class="step-index">1</span><span><strong>选择文件</strong>：点「选择文件」，也可以点「选文件夹」发送整个文件夹。</span></li>
            <li><span class="step-index">2</span><span><strong>选择接收电脑</strong>：首次使用先填写对方电脑的 IP 地址、用户名和保存位置，点「保存这台电脑」。</span></li>
            <li><span class="step-index">3</span><span><strong>开始发送</strong>：点绿色「开始发送」按钮，等待进度到 100%。</span></li>
          </ol>
          <div class="help-section">
            <h3>连不上对方电脑？</h3>
            <ul>
              <li>确认两台电脑连接在同一个网络。</li>
              <li>确认接收电脑已开启远程连接（Windows 的 OpenSSH 服务）。</li>
              <li>确认 IP 地址、用户名和密码填写正确。</li>
              <li>确认接收电脑的防火墙允许 22 端口。</li>
            </ul>
          </div>
          <div class="help-section">
            <h3>第一次连接为什么要验证对方电脑？</h3>
            <p>这是为了防止别人冒充接收电脑。点「验证对方电脑身份」后，把显示的指纹和接收电脑上的指纹核对一致，再点「确认验证」。以后会自动验证。</p>
          </div>
        </section>

        <section v-if="showConnectionForm || !form.host" ref="connectionSection" class="send-panel">
          <div class="panel-heading">
            <div class="panel-title-group">
              <span class="panel-icon"><KeyRound :size="17" /></span>
              <div>
                <h2>{{ form.host ? '接收电脑设置' : '添加接收电脑' }}</h2>
                <p>{{ form.host ? '修改后点「保存这台电脑」生效' : '填写一次，之后就能直接发送' }}</p>
              </div>
            </div>
            <div class="panel-heading-actions">
              <button
                v-if="activeDeviceId"
                class="inline-action danger"
                :disabled="isLocked"
                @click="removeActiveDevice"
              >
                <Trash2 :size="14" />删除这台电脑
              </button>
              <button v-if="form.host" class="text-button" title="收起" @click="showConnectionForm = false"><ChevronDown :size="18" /></button>
            </div>
          </div>
          <p class="form-hint">这些信息在接收电脑上查看；端口一般不用改。</p>
          <div class="form-grid">
            <label class="field wide"><span>电脑名称</span>
              <input v-model="form.deviceName" :disabled="isLocked" placeholder="例如：家里的台式机"
                title="给接收电脑起个容易记住的名字，只显示在这台电脑上。" />
            </label>
            <label class="field"><span>对方电脑的 IP 地址</span>
              <input v-model="form.host" :disabled="isLocked" placeholder="192.168.1.23"
                title="在接收电脑上打开命令提示符，输入 ipconfig 查看 IPv4 地址。" />
            </label>
            <label class="field narrow"><span>端口 <em class="field-hint">一般不用改</em></span>
              <input v-model.number="form.port" :disabled="isLocked" type="number" min="1" max="65535"
                title="SSH 端口，默认 22；没有特别要求就保持不动。" />
            </label>
            <label class="field"><span>对方电脑的用户名</span>
              <input v-model="form.username" :disabled="isLocked" placeholder="例如：lin"
                title="登录接收电脑时使用的账户名。" />
            </label>
            <label class="field wide"><span>文件保存到哪个文件夹</span>
              <input v-model="form.remoteDir" :disabled="isLocked" placeholder="例如：D:/LANDrop"
                title="接收电脑上的文件夹，不存在会自动创建；Windows 路径写成 D:/LANDrop 这种形式。" />
            </label>
          </div>
          <div class="auth-row">
            <div class="auth-toggle">
              <button :class="{ active: form.authMethod === 'password' }" :disabled="isLocked" @click="form.authMethod = 'password'">密码（推荐）</button>
              <button :class="{ active: form.authMethod === 'key' }" :disabled="isLocked" @click="form.authMethod = 'key'">密钥文件</button>
            </div>
            <span class="field-hint auth-hint">
              {{ form.authMethod === 'password' ? '用对方电脑的登录密码，最简单。' : '需要先在对方电脑上配置公钥，适合熟悉 SSH 的用户。' }}
            </span>
          </div>
          <div v-if="form.authMethod === 'key'" class="auth-detail">
            <button class="file-picker compact" :disabled="isLocked" @click="choosePrivateKey">
              <span class="file-picker-copy">
                <strong>{{ form.privateKeyPath ? fileName(form.privateKeyPath) : '选择密钥文件' }}</strong>
                <small>高级方式：选择没有设置口令的私钥文件</small>
              </span>
              <span class="picker-key">浏览</span>
            </button>
          </div>
          <div v-else class="auth-detail">
            <input v-model="form.password" class="password-input" :disabled="isLocked" type="password"
              :placeholder="passwordSaved ? '已保存密码，留空即可' : '对方电脑的登录密码'" autocomplete="off" />
            <label class="remember" title="保存在 Windows 凭据管理器，不会明文保存。">
              <input v-model="form.rememberPassword" :disabled="isLocked" type="checkbox" />
              <span>{{ passwordSaved ? '已保存到 Windows 凭据管理器' : '记住密码（下次不用再输入）' }}</span>
            </label>
          </div>
          <div class="hostkey-row">
            <button class="inline-action" :disabled="busy || isLocked" @click="checkHostKey">
              <ShieldAlert :size="14" />验证对方电脑身份
            </button>
            <span class="field-hint">首次连接需要核对，之后会自动验证</span>
            <template v-if="hostKey">
              <code>{{ hostKey.key_type }} {{ hostKey.fingerprint }}</code>
              <span v-if="hostKey.trusted" class="trusted-tag"><CheckCircle2 :size="13" />已验证</span>
              <button v-else class="inline-action trust" :disabled="busy" @click="confirmHostKey">
                确认验证
              </button>
            </template>
          </div>
          <div class="panel-foot">
            <span v-if="connectionHint" class="log-hint" :class="{ error: connectionHintError }">{{ connectionHint }}</span>
            <button class="primary-button" :disabled="busy || isLocked" @click="saveActiveConnection">
              保存这台电脑
            </button>
          </div>
        </section>

        <section v-if="showLogSettings" class="send-panel log-panel">
          <div class="panel-heading">
            <div class="panel-title-group">
              <span class="panel-icon"><FileText :size="17" /></span>
              <div><h2>日志</h2><p>传输记录按天滚动，超期自动清理</p></div>
            </div>
            <div class="panel-heading-actions">
              <button class="inline-action" @click="openLogFolder">打开目录</button>
              <button class="text-button" title="收起" @click="showLogSettings = false"><ChevronDown :size="18" /></button>
            </div>
          </div>
          <div class="form-grid">
            <label class="field wide"><span>日志目录</span>
              <div class="log-dir-row">
                <input v-model="settings.logDir" :placeholder="logsInfo?.default_dir || '默认目录'" />
                <button class="inline-action" @click="chooseLogDir">浏览</button>
                <button class="inline-action" @click="resetLogDir">用默认</button>
              </div>
            </label>
            <label class="field narrow"><span>保留天数</span>
              <select v-model.number="settings.logRetentionDays" class="log-select">
                <option v-for="days in [7, 14, 30, 90, 0]" :key="days" :value="days">{{ days ? `${days} 天` : '永久' }}</option>
              </select>
            </label>
          </div>
          <div class="log-meta">
            <span v-if="logsInfo">{{ formatBytes(logsInfo.total_size) }} · {{ logsInfo.files.length }} 个文件</span>
            <span v-else>尚未读取日志目录</span>
            <button class="inline-action trust" :disabled="busy" @click="saveLogSettings">保存</button>
          </div>
          <p v-if="logHint" class="log-hint">{{ logHint }}</p>
        </section>

        <section v-if="showSchedulePanel" class="send-panel">
          <div class="panel-heading">
            <div class="panel-title-group">
              <span class="panel-icon"><CalendarClock :size="17" /></span>
              <div><h2>定时传输</h2><p>应用运行时本机调度；关闭后由 Windows 计划任务执行</p></div>
            </div>
            <div class="panel-heading-actions">
              <span v-if="!systemTasksSupported" class="schedule-warning">开发模式：不注册系统任务</span>
              <button class="text-button" title="收起" @click="showSchedulePanel = false"><ChevronDown :size="18" /></button>
            </div>
          </div>

          <div class="form-grid">
            <label class="field"><span>任务名称</span>
              <input v-model="scheduleDraft.name" placeholder="例如 每晚备份" />
            </label>
            <label class="field wide"><span>来源</span>
              <div class="log-dir-row">
                <input v-model="scheduleDraft.sourcePath" placeholder="选择文件或文件夹" />
                <button class="inline-action" @click="chooseScheduleSource">文件</button>
                <button class="inline-action" @click="chooseScheduleFolder">文件夹</button>
              </div>
            </label>
            <label class="field"><span>目标设备</span>
              <select v-model="scheduleDraft.deviceId" class="log-select">
                <option v-for="device in devices" :key="device.id" :value="device.id">{{ device.name || device.host }}</option>
              </select>
            </label>
            <label class="field"><span>模式</span>
              <select v-model="scheduleDraft.mode" class="log-select">
                <option value="once">一次性</option>
                <option value="daily">每天</option>
                <option value="weekly">每周</option>
              </select>
            </label>
            <label class="field narrow"><span>时间</span>
              <input v-model="scheduleDraft.time" type="time" />
            </label>
            <label v-if="scheduleDraft.mode === 'once'" class="field narrow"><span>日期</span>
              <input v-model="scheduleDraft.date" type="date" />
            </label>
          </div>

          <div v-if="scheduleDraft.mode === 'weekly'" class="weekday-row">
            <span>星期</span>
            <button
              v-for="(label, index) in WEEKDAY_LABELS"
              :key="index"
              class="hotkey-mod"
              :class="{ active: scheduleDraft.weekdays.includes(index) }"
              @click="toggleDraftWeekday(index)"
            >{{ label }}</button>
          </div>

          <div class="log-meta">
            <label class="remember">
              <input v-model="scheduleDraft.overwrite" type="checkbox" />
              <span>覆盖远端同名文件</span>
            </label>
            <button class="inline-action trust" :disabled="busy" @click="submitSchedule">添加任务</button>
          </div>
          <p v-if="scheduleHint" class="log-hint">{{ scheduleHint }}</p>

          <div class="schedule-list">
            <div v-if="!schedules.length" class="empty-row">还没有定时任务。</div>
            <div v-for="item in schedules" :key="item.id" class="schedule-row">
              <label class="schedule-toggle">
                <input type="checkbox" :checked="item.enabled" @change="toggleScheduleEnabled(item.id)" />
              </label>
              <span class="schedule-copy">
                <strong>{{ item.name }}</strong>
                <small>{{ fileName(item.source_path) }} · {{ deviceLabel(item.device_id) }} · {{ scheduleModeLabel(item) }}</small>
                <small class="schedule-meta">
                  下次：{{ scheduleNextLabel(item.id) }}
                  <template v-if="scheduleLastRun(item).at"> · 上次：{{ scheduleStatusLabel(scheduleLastRun(item).status) }}</template>
                </small>
              </span>
              <span class="schedule-actions">
                <button class="inline-action" :disabled="busy" @click="runScheduleItem(item.id)">立即执行</button>
                <button class="icon-button small" title="删除" aria-label="删除" @click="removeScheduleItem(item.id)">
                  <X :size="13" />
                </button>
              </span>
            </div>
          </div>
        </section>

        <section v-if="showSystemSettings" class="send-panel">
          <div class="panel-heading">
            <div class="panel-title-group">
              <span class="panel-icon"><Settings2 :size="17" /></span>
              <div><h2>系统</h2><p>窗口行为与启动方式</p></div>
            </div>
            <button class="text-button" title="收起" @click="showSystemSettings = false"><ChevronDown :size="18" /></button>
          </div>
          <div class="system-options">
            <label class="remember">
              <input v-model="settings.closeToTray" type="checkbox" @change="saveSystemSettings" />
              <span>关闭窗口时最小化到托盘（服务与定时任务继续运行）</span>
            </label>
            <label class="remember">
              <input :checked="autostartEnabled" type="checkbox" @change="toggleAutostart" />
              <span>开机自动启动（静默启动到托盘，由注册表 Run 项实现）</span>
            </label>
          </div>
          <p v-if="systemHint" class="log-hint">{{ systemHint }}</p>
        </section>

        <section class="send-panel">
          <div class="panel-heading">
            <div class="panel-title-group">
              <span class="panel-icon"><Upload :size="17" /></span>
              <div><h2>发送文件</h2><p>按下面 3 步操作即可</p></div>
            </div>
            <button class="inline-action" title="接收电脑设置" @click="openConnectionPanel">
              <Settings2 :size="14" />接收电脑设置
            </button>
          </div>
          <div class="step-strip">
            <div class="step-item" :class="{ active: currentStep === 1, done: queue.length > 0 }">
              <span class="step-index">
                <CheckCircle2 v-if="queue.length > 0" :size="14" />
                <template v-else>1</template>
              </span><span>选择文件</span>
            </div>
            <span class="step-connector"></span>
            <div class="step-item" :class="{ active: currentStep === 2, done: setupComplete }">
              <span class="step-index">
                <CheckCircle2 v-if="setupComplete" :size="14" />
                <template v-else>2</template>
              </span><span>选择接收电脑</span>
            </div>
            <span class="step-connector"></span>
            <div class="step-item" :class="{ active: currentStep === 3 }">
              <span class="step-index">3</span><span>开始发送</span>
            </div>
          </div>
          <div class="send-controls">
            <div class="source-picker-group">
              <button class="file-picker" :disabled="isLocked" @click="chooseFile">
                <span class="file-picker-icon"><FolderOpen :size="18" /></span>
                <span class="file-picker-copy">
                  <strong>第 1 步 · 选择文件</strong>
                  <small>{{ queue.length ? `已选 ${queue.length} 项，可以继续添加` : '可以多选，也可以选整个文件夹' }}</small>
                </span>
                <span class="picker-key">选择</span>
              </button>
              <button class="folder-action" :disabled="isLocked" title="选择整个文件夹" aria-label="选择整个文件夹" @click="chooseFolder">
                <Folder :size="16" /><span>选文件夹</span>
              </button>
            </div>
            <div class="control-divider"><span></span></div>
            <button class="device-select" @click="openConnectionPanel">
              <span class="device-select-icon"><Laptop :size="18" /></span>
              <span class="device-select-copy">
                <small>第 2 步 · 发送到</small>
                <strong>{{ form.deviceName || form.host || '先添加接收电脑' }}</strong>
              </span>
              <ChevronDown :size="16" class="muted-icon" />
            </button>
            <button class="primary-button send-action" :disabled="!canSend" @click="sendQueue">
              <Upload :size="16" />{{ queue.length ? `开始发送 ${queue.length} 项` : '开始发送' }}
            </button>
          </div>
          <p class="send-hint" :class="{ ready: canSend }">
            <CheckCircle2 v-if="canSend" :size="13" />
            <AlertCircle v-else :size="13" />
            <span v-if="sendBlockReason">{{ sendBlockReason }}</span>
            <span v-else>将发送到 {{ form.deviceName || form.host }}</span>
          </p>
          <div v-if="queue.length" class="queue-panel">
            <div class="queue-head">
              <span class="queue-title">
                {{ queue.length }} 项<template v-if="queueSummary.size"> · {{ formatBytes(queueSummary.size) }}</template>
              </span>
              <span class="queue-head-actions">
                <button v-if="batch.running" class="inline-action danger" @click="cancelQueue">取消发送</button>
                <button v-else class="inline-action" @click="clearQueue">清空列表</button>
              </span>
            </div>
            <div class="queue-list">
              <div v-for="item in queue" :key="item.key" class="queue-row">
                <span class="queue-icon">
                  <Folder v-if="item.kind === 'folder'" :size="14" />
                  <File v-else :size="14" />
                </span>
                <span class="queue-copy">
                  <strong>{{ fileName(item.path) }}</strong>
                  <small>
                    <template v-if="item.kind === 'folder'">{{ item.fileCount }} 个文件 · {{ formatBytes(item.size) }}</template>
                    <template v-else>{{ item.path }}</template>
                    <template v-if="item.error"> · {{ item.error }}</template>
                  </small>
                </span>
                <button v-if="!batch.running" class="icon-button small" title="从列表移除" aria-label="从列表移除" @click="removeSource(item.key)">
                  <X :size="13" />
                </button>
              </div>
            </div>
            <div v-if="batch.running || batchMessage" class="queue-foot">
              <span v-if="batch.running">正在发送 {{ Math.min(batch.done + 1, batch.total) }}/{{ batch.total }}</span>
              <span v-else>{{ batchMessage }}</span>
              <button
                v-if="!batch.running && hasTargetConflicts"
                class="inline-action trust"
                @click="retryFailedWithOverwrite"
              >覆盖并重发</button>
            </div>
          </div>
          <div class="destination-row">
            <span>保存到</span><code>{{ form.remoteDir || '未设置' }}</code>
            <button class="inline-action" title="更改保存位置" @click="openConnectionPanel"><Settings2 :size="13" />更改</button>
            <span class="destination-spacer"></span>
            <LockKeyhole :size="13" /><span>加密传输</span>
          </div>
        </section>

        <section v-if="selectedTask" class="active-panel" :class="tone(selectedTask)">
          <div class="active-head">
            <span class="file-icon" :class="tone(selectedTask)"><File :size="17" /></span>
            <div class="file-meta">
              <strong>
                {{ fileName(selectedTask.request.local_path) }}
                <span v-if="selectedTask.source === 'schedule'" class="source-tag">定时</span>
              </strong>
              <small :title="selectedTask.error || undefined">
                {{ selectedTask.message }}
                <template v-if="selectedTask.files_total"> · 第 {{ selectedTask.files_done }}/{{ selectedTask.files_total }} 个</template>
                <template v-if="selectedTask.current_file"> · {{ fileName(selectedTask.current_file) }}</template>
                <template v-if="selectedTask.error"> · {{ errorHint(selectedTask.error_code) || selectedTask.error }}</template>
              </small>
            </div>
            <div class="active-actions">
              <button v-if="selectedTask.status === 'running' || selectedTask.status === 'queued'" class="inline-action danger" @click="cancelActive">停止发送</button>
              <button v-if="selectedTask.status === 'failed' || selectedTask.status === 'cancelled'" class="inline-action" :disabled="busy" @click="retryActive">
                <RefreshCw :size="13" />重新发送
              </button>
            </div>
          </div>
          <div class="progress-label">
            <span :class="`status-text ${tone(selectedTask)}`">{{ phaseLabel(selectedTask) }}</span>
            <span v-if="selectedTask.phase === 'verifying'">
              正在核对文件 {{ formatBytes(selectedTask.verified) }} / {{ formatBytes(selectedTask.verify_total || selectedTask.total) }}
            </span>
            <span v-else>{{ formatBytes(selectedTask.sent) }} / {{ formatBytes(selectedTask.total) }} · {{ formatSpeed(selectedTask.speed) }} · {{ percent(selectedTask) }}%</span>
          </div>
          <div class="progress-track tall">
            <span :class="`progress-fill ${tone(selectedTask)}`" :style="{ width: `${percent(selectedTask)}%` }"></span>
          </div>
          <div v-if="selectedTask.result" class="result-row">
            <template v-if="selectedTask.result.files">
              <span>已保存到：<code>{{ selectedTask.result.remote_path }}</code></span>
              <span>成功 {{ selectedTask.result.ok_count }} · 失败 {{ selectedTask.result.failed_count }}</span>
              <span
                v-for="failure in selectedTask.result.files.filter(item => !item.ok)"
                :key="failure.rel_path"
                class="failure-line"
                :title="failure.error || undefined"
              >{{ failure.rel_path }}：{{ errorHint(failure.error_code) || failure.error }}</span>
            </template>
            <template v-else>
              <span>已保存到：<code>{{ selectedTask.result.remote_path }}</code></span>
              <span v-if="selectedTask.result.sha256">文件校验值：<code>{{ selectedTask.result.sha256 }}</code></span>
              <span v-if="selectedTask.result.resumed_from">从上次中断处继续（已传 {{ formatBytes(selectedTask.result.resumed_from) }}）</span>
            </template>
          </div>
        </section>

        <div v-if="lastError" class="error-banner">
          <AlertCircle :size="15" /><span>{{ lastError }}</span>
        </div>

        <section class="overview-row">
          <div class="overview-label"><span class="section-kicker">概览</span><h2>最近传输</h2></div>
          <div class="overview-metrics">
            <div><strong>{{ tasks.length }}</strong><span>总任务</span></div><i></i>
            <div><strong>{{ tasks.filter(t => t.status === 'succeeded').length }}</strong><span>已完成</span></div><i></i>
            <div><strong>{{ formatBytes(totalSent) }}</strong><span>累计发送</span></div>
          </div>
        </section>

        <section class="transfer-table">
          <div class="table-header"><span>文件</span><span>状态</span><span>大小</span></div>
          <div v-if="!recentTasks.length" class="empty-row">还没有发送记录。选择文件后点「开始发送」即可。</div>
          <button v-for="task in recentTasks" :key="task.id" class="transfer-row" @click="selectTask(task.id)">
            <div class="file-cell">
              <span class="file-icon" :class="tone(task)"><File :size="17" /></span>
              <span class="file-meta">
                <strong>
                  {{ fileName(task.request.local_path) }}
                  <span v-if="task.source === 'schedule'" class="source-tag">定时</span>
                </strong>
                <small :title="task.error || undefined">{{ task.message }}<template v-if="task.error"> · {{ errorHint(task.error_code) || task.error }}</template></small>
              </span>
            </div>
            <div class="progress-cell">
              <div class="progress-label">
                <span :class="`status-text ${tone(task)}`">{{ phaseLabel(task) }}</span>
                <span>{{ percent(task) }}%</span>
              </div>
              <div class="progress-track"><span :class="`progress-fill ${tone(task)}`" :style="{ width: `${percent(task)}%` }"></span></div>
            </div>
            <div class="size-cell">{{ formatBytes(task.total) }}</div>
          </button>
          <div class="table-footer">
            <span>最近 {{ recentTasks.length }} 条记录</span>
            <span>{{ form.remoteDir || '未设置保存位置' }}</span>
          </div>
        </section>

        <footer class="bottom-info">
          <div class="network-info">
            <Wifi :size="15" /><span>文件只在你和对方电脑之间传输</span>
            <span class="info-separator">·</span><span>{{ serviceState === 'ready' ? '应用运行正常' : '应用未就绪' }}</span>
          </div>
          <span class="version-button">v0.2.0</span>
        </footer>
      </div>
    </main>
  </div>
</template>
