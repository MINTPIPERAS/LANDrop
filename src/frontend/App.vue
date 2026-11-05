<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { open } from '@tauri-apps/plugin-dialog'
import { isRegistered, register, unregister } from '@tauri-apps/plugin-global-shortcut'
import { invoke } from '@tauri-apps/api/core'
import {
  AlertCircle,
  ArrowDownToLine,
  CheckCircle2,
  ChevronDown,
  CircleHelp,
  File,
  FolderOpen,
  KeyRound,
  Laptop,
  LockKeyhole,
  MoreHorizontal,
  RefreshCw,
  Settings2,
  ShieldAlert,
  ShieldCheck,
  Upload,
  Wifi,
} from '@lucide/vue'
import {
  activeTask,
  buildHotkey,
  busy,
  canSend,
  cancelActive,
  checkHostKey,
  confirmHostKey,
  connectService,
  DEFAULT_HOTKEY,
  fileName,
  form,
  formatBytes,
  formatSpeed,
  HOTKEY_MODIFIERS,
  hotkey,
  hotkeyAccelerator,
  hotkeyLabel,
  hostKey,
  isLocked,
  lastError,
  localFile,
  passwordSaved,
  percent,
  phaseLabel,
  retryActive,
  saveHotkey,
  selectTask,
  send,
  serviceMessage,
  serviceState,
  tasks,
} from './store/transfer'

const hotkeyStatus = ref('快捷键未注册')
const showConnectionForm = ref(false)
const showHotkeyEditor = ref(false)
const draftModifiers = ref<string[]>([...DEFAULT_HOTKEY.modifiers])
const draftKey = ref(DEFAULT_HOTKEY.key)
let registeredAccelerator: string | null = null

const isDesktop = '__TAURI_INTERNALS__' in window
const selectedTask = computed(() => activeTask.value ?? tasks.value[0] ?? null)
const recentTasks = computed(() => tasks.value.slice(0, 8))
const totalSent = computed(() =>
  tasks.value
    .filter(task => task.status === 'succeeded')
    .reduce((sum, task) => sum + (task.result?.size ?? 0), 0),
)

function tone(task: { status: string }): 'green' | 'blue' | 'amber' | 'red' {
  if (task.status === 'succeeded') return 'green'
  if (task.status === 'failed') return 'red'
  if (task.status === 'cancelled') return 'amber'
  return 'blue'
}

async function chooseFile() {
  if (isLocked.value) return
  if (!isDesktop) {
    localFile.value = '示例文件_仅供预览.zip'
    return
  }
  const path = await open({ multiple: false, directory: false })
  if (typeof path === 'string') localFile.value = path
}

async function choosePrivateKey() {
  if (!isDesktop) return
  const path = await open({ multiple: false, directory: false })
  if (typeof path === 'string') form.privateKeyPath = path
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
  if (!isDesktop) return
  draftModifiers.value = [...hotkey.value.modifiers]
  draftKey.value = hotkey.value.key
  try {
    if (!(await isRegistered(hotkeyAccelerator()))) await registerHotkey()
  } catch {
    hotkeyStatus.value = '快捷键被占用，可点击头像重试'
  }
})

onUnmounted(() => {
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
        <span class="side-section-label">连接</span>
        <button class="icon-button small" title="连接设置" aria-label="连接设置" @click="showConnectionForm = !showConnectionForm">
          <Settings2 :size="16" />
        </button>
      </div>
      <div class="connection-list">
        <button class="connection-item active" @click="showConnectionForm = !showConnectionForm">
          <span class="device-status" :class="serviceState === 'ready' ? 'connected' : ''"></span>
          <span class="connection-copy">
            <span class="connection-name">{{ form.host || '尚未配置远端' }}</span>
            <span class="connection-host">{{ form.username ? `${form.username}@${form.port}` : '填写 SSH 连接信息' }}</span>
          </span>
          <MoreHorizontal :size="16" class="connection-more" />
        </button>
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
          <ShieldCheck :size="15" /><span>SSH 加密 · 验证主机身份</span>
        </div>
        <button class="profile-row" title="点击切换全局快捷键" @click="toggleHotkey">
          <div class="avatar">L</div>
          <span class="profile-copy"><strong>本机设备</strong><small>{{ hotkeyStatus }}</small></span>
          <Settings2 :size="16" class="muted-icon" />
        </button>
        <button class="hotkey-config-row" @click="openHotkeyEditor">
          <Settings2 :size="14" /><span>设置全局快捷键</span>
        </button>
      </div>
    </aside>

    <main class="main-area">
      <header class="topbar">
        <div class="breadcrumbs"><span>LANDrop</span><span class="crumb-divider">/</span><strong>传输任务</strong></div>
        <div class="top-actions">
          <div class="local-status">
            <span class="pulse-dot" :class="{ online: serviceState === 'ready' }"></span>{{ serviceMessage }}
          </div>
          <button class="icon-button" title="帮助" aria-label="帮助"><CircleHelp :size="17" /></button>
          <button class="avatar mini-avatar" :title="hotkeyStatus" @click="toggleHotkey">L</button>
        </div>
      </header>

      <div class="content-wrap">
        <section class="page-heading">
          <div>
            <div class="eyebrow">LOCAL FILE TRANSFER</div>
            <h1>传输任务</h1>
            <p>通过 SSH/SFTP 把文件安全地发送到已配置的远端电脑。</p>
          </div>
          <button class="primary-button" :disabled="!canSend" @click="send">
            <Upload :size="16" />发送文件
          </button>
        </section>

        <section v-if="showConnectionForm || !form.host" class="send-panel">
          <div class="panel-heading">
            <div class="panel-title-group">
              <span class="panel-icon"><KeyRound :size="17" /></span>
              <div><h2>连接配置</h2><p>保存后用于本机传输服务</p></div>
            </div>
            <button class="text-button" title="收起" @click="showConnectionForm = false"><ChevronDown :size="18" /></button>
          </div>
          <div class="form-grid">
            <label class="field"><span>远端主机</span>
              <input v-model="form.host" :disabled="isLocked" placeholder="192.168.1.23" />
            </label>
            <label class="field narrow"><span>端口</span>
              <input v-model.number="form.port" :disabled="isLocked" type="number" min="1" max="65535" />
            </label>
            <label class="field"><span>用户名</span>
              <input v-model="form.username" :disabled="isLocked" placeholder="lin" />
            </label>
            <label class="field wide"><span>目标目录</span>
              <input v-model="form.remoteDir" :disabled="isLocked" placeholder="/home/lin/Downloads" />
            </label>
          </div>
          <div class="auth-row">
            <div class="auth-toggle">
              <button :class="{ active: form.authMethod === 'key' }" :disabled="isLocked" @click="form.authMethod = 'key'">私钥</button>
              <button :class="{ active: form.authMethod === 'password' }" :disabled="isLocked" @click="form.authMethod = 'password'">密码</button>
            </div>
            <template v-if="form.authMethod === 'key'">
              <button class="file-picker compact" :disabled="isLocked" @click="choosePrivateKey">
                <span class="file-picker-copy">
                  <strong>{{ form.privateKeyPath ? fileName(form.privateKeyPath) : '使用默认 SSH 私钥' }}</strong>
                  <small>可选，留空则使用 ssh-agent 或 ~/.ssh</small>
                </span>
                <span class="picker-key">浏览</span>
              </button>
            </template>
            <template v-else>
              <input v-model="form.password" class="password-input" :disabled="isLocked" type="password"
                :placeholder="passwordSaved ? '已保存密码，留空则使用凭据管理器' : 'SSH 密码'" autocomplete="off" />
              <label class="remember">
                <input v-model="form.rememberPassword" :disabled="isLocked" type="checkbox" />
                <span>{{ passwordSaved ? '已保存到凭据管理器' : '记住密码（Windows 凭据管理器）' }}</span>
              </label>
            </template>
          </div>
          <div class="hostkey-row">
            <button class="inline-action" :disabled="busy || isLocked" @click="checkHostKey">
              <ShieldAlert :size="14" />检查主机指纹
            </button>
            <template v-if="hostKey">
              <code>{{ hostKey.key_type }} {{ hostKey.fingerprint }}</code>
              <span v-if="hostKey.trusted" class="trusted-tag"><CheckCircle2 :size="13" />已信任</span>
              <button v-else class="inline-action trust" :disabled="busy" @click="confirmHostKey">
                确认并保存
              </button>
            </template>
          </div>
        </section>

        <section class="send-panel">
          <div class="panel-heading">
            <div class="panel-title-group">
              <span class="panel-icon"><Upload :size="17" /></span>
              <div><h2>新建传输</h2><p>选择文件并发送到配置的远端目录</p></div>
            </div>
            <button class="text-button" title="连接设置" @click="showConnectionForm = !showConnectionForm"><Settings2 :size="18" /></button>
          </div>
          <div class="send-controls">
            <button class="file-picker" :disabled="isLocked" @click="chooseFile">
              <span class="file-picker-icon"><FolderOpen :size="18" /></span>
              <span class="file-picker-copy">
                <strong>{{ localFile ? fileName(localFile) : '选择要发送的文件' }}</strong>
                <small>{{ localFile ? localFile : '浏览此设备上的文件' }}</small>
              </span>
              <span class="picker-key">浏览</span>
            </button>
            <div class="control-divider"><span></span></div>
            <button class="device-select" @click="showConnectionForm = true">
              <span class="device-select-icon"><Laptop :size="18" /></span>
              <span class="device-select-copy">
                <small>发送到</small>
                <strong>{{ form.host || '尚未配置' }}</strong>
              </span>
              <ChevronDown :size="16" class="muted-icon" />
            </button>
            <button class="send-action" :disabled="!canSend" title="开始发送" aria-label="开始发送" @click="send">
              <ArrowDownToLine :size="18" />
            </button>
          </div>
          <div class="destination-row">
            <span>目标文件夹</span><code>{{ form.remoteDir || '未设置' }}</code>
            <button class="inline-action" title="更改目标目录" @click="showConnectionForm = true"><Settings2 :size="14" /></button>
            <span class="destination-spacer"></span>
            <LockKeyhole :size="13" /><span>SSH 加密连接</span>
          </div>
        </section>

        <section v-if="selectedTask" class="active-panel" :class="tone(selectedTask)">
          <div class="active-head">
            <span class="file-icon" :class="tone(selectedTask)"><File :size="17" /></span>
            <div class="file-meta">
              <strong>{{ fileName(selectedTask.request.local_path) }}</strong>
              <small>{{ selectedTask.message }}<template v-if="selectedTask.error"> · {{ selectedTask.error }}</template></small>
            </div>
            <div class="active-actions">
              <button v-if="selectedTask.status === 'running' || selectedTask.status === 'queued'" class="inline-action danger" @click="cancelActive">取消</button>
              <button v-if="selectedTask.status === 'failed' || selectedTask.status === 'cancelled'" class="inline-action" :disabled="busy" @click="retryActive">
                <RefreshCw :size="13" />重试
              </button>
            </div>
          </div>
          <div class="progress-label">
            <span :class="`status-text ${tone(selectedTask)}`">{{ phaseLabel(selectedTask) }}</span>
            <span>{{ formatBytes(selectedTask.sent) }} / {{ formatBytes(selectedTask.total) }} · {{ formatSpeed(selectedTask.speed) }} · {{ percent(selectedTask) }}%</span>
          </div>
          <div class="progress-track tall">
            <span :class="`progress-fill ${tone(selectedTask)}`" :style="{ width: `${percent(selectedTask)}%` }"></span>
          </div>
          <div v-if="selectedTask.result" class="result-row">
            <span>远端：<code>{{ selectedTask.result.remote_path }}</code></span>
            <span>SHA-256：<code>{{ selectedTask.result.sha256 }}</code></span>
            <span v-if="selectedTask.result.resumed_from">续传自 {{ formatBytes(selectedTask.result.resumed_from) }}</span>
          </div>
        </section>

        <div v-if="lastError" class="error-banner">
          <AlertCircle :size="15" /><span>{{ lastError }}</span>
        </div>

        <section class="overview-row">
          <div class="overview-label"><span class="section-kicker">OVERVIEW</span><h2>最近传输</h2></div>
          <div class="overview-metrics">
            <div><strong>{{ tasks.length }}</strong><span>总任务</span></div><i></i>
            <div><strong>{{ tasks.filter(t => t.status === 'succeeded').length }}</strong><span>已完成</span></div><i></i>
            <div><strong>{{ formatBytes(totalSent) }}</strong><span>累计发送</span></div>
          </div>
        </section>

        <section class="transfer-table">
          <div class="table-header"><span>文件</span><span>传输状态</span><span>大小</span><span></span></div>
          <div v-if="!recentTasks.length" class="empty-row">还没有传输任务。选择文件并点击发送即可开始。</div>
          <button v-for="task in recentTasks" :key="task.id" class="transfer-row" @click="selectTask(task.id)">
            <div class="file-cell">
              <span class="file-icon" :class="tone(task)"><File :size="17" /></span>
              <span class="file-meta">
                <strong>{{ fileName(task.request.local_path) }}</strong>
                <small>{{ task.message }}<template v-if="task.error"> · {{ task.error }}</template></small>
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
            <span class="row-more icon-button"><MoreHorizontal :size="17" /></span>
          </button>
          <div class="table-footer">
            <span>显示最近 {{ recentTasks.length }} 个任务</span>
            <span>{{ form.remoteDir || '未设置目标目录' }}</span>
          </div>
        </section>

        <footer class="bottom-info">
          <div class="network-info">
            <Wifi :size="15" /><span>仅通过你配置的 SSH 连接传输</span>
            <span class="info-separator">·</span><span>{{ serviceState === 'ready' ? '本机服务运行中' : '本机服务不可用' }}</span>
          </div>
          <button class="version-button">v0.1.0 <ChevronDown :size="13" /></button>
        </footer>
      </div>
    </main>
  </div>
</template>
