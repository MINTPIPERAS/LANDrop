<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ArrowUp, ChevronRight, File, Folder, FolderOpen, RefreshCw, X } from '@lucide/vue'
import { listDirectory, listRoots, type FsEntry, type FsListing, type FsRoot } from '../services/api'
import {
  filterEntries,
  formatEntryTime,
  isDriveRoot,
  recentHours,
  sortEntries,
  type PickerConfirmResult,
  type PickerMode,
  type RecentFilter,
  type SortOrder,
  type TimeField,
} from '../picker'

const props = defineProps<{
  mode: PickerMode
  multiple: boolean
  title?: string
}>()

const emit = defineEmits<{
  (event: 'close'): void
  (event: 'confirm', result: PickerConfirmResult): void
}>()

const STORAGE_KEY = 'irecon-picker-dir'
const isDesktop = '__TAURI_INTERNALS__' in window

const activeMode = ref<PickerMode>(props.mode)
const currentPath = ref('')
const pathInput = ref('')
const parentPath = ref<string | null>(null)
const entries = ref<FsEntry[]>([])
const roots = ref<FsRoot[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const truncated = ref(false)
const total = ref(0)
const skippedCount = ref(0)
const recentFilter = ref<RecentFilter>('all')
const timeField = ref<TimeField>('modified')
const sortOrder = ref<SortOrder>('desc')
const showHidden = ref(false)
const selected = ref(new Map<string, { kind: 'file' | 'folder'; size: number | null }>())
const pathInputRef = ref<HTMLInputElement | null>(null)

let requestId = 0
let previousOverflow = ''

const hours = computed(() => recentHours(recentFilter.value))
const visibleEntries = computed(() =>
  sortEntries(
    filterEntries(entries.value, activeMode.value, hours.value, timeField.value, showHidden.value),
    timeField.value,
    sortOrder.value,
  ),
)
const selectableEntries = computed(() =>
  visibleEntries.value.filter(entry =>
    activeMode.value === 'file' ? !entry.is_dir : entry.is_dir,
  ),
)
const allVisibleSelected = computed(
  () =>
    selectableEntries.value.length > 0 &&
    selectableEntries.value.every(entry => selected.value.has(entry.path)),
)
const selectedItems = computed(() => [...selected.value.values()])
const selectedFileCount = computed(
  () => selectedItems.value.filter(item => item.kind === 'file').length,
)
const selectedFolderCount = computed(
  () => selectedItems.value.filter(item => item.kind === 'folder').length,
)
const canConfirm = computed(() => !loading.value && selected.value.size > 0)
const currentFolderSelectable = computed(
  () =>
    activeMode.value === 'folder' &&
    !loading.value &&
    Boolean(currentPath.value) &&
    !isDriveRoot(currentPath.value),
)
const currentFolderTitle = computed(() =>
  isDriveRoot(currentPath.value)
    ? '请先进入具体文件夹，不能直接选择盘符或共享根目录'
    : '只添加当前文件夹，忽略其他已选条目',
)
const dialogTitle = computed(
  () => props.title || (activeMode.value === 'file' ? '选择文件' : '选择文件夹'),
)

function formatSize(size: number | null): string {
  if (size === null) return '文件夹'
  if (size < 1024) return `${size} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = size
  let unit = -1
  do {
    value /= 1024
    unit += 1
  } while (value >= 1024 && unit < units.length - 1)
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`
}

function isSelectable(entry: FsEntry): boolean {
  return activeMode.value === 'file' ? !entry.is_dir : entry.is_dir
}

function isSelected(path: string): boolean {
  return selected.value.has(path)
}

function close() {
  emit('close')
}

function confirm() {
  if (!canConfirm.value) return
  const files: string[] = []
  const folders: string[] = []
  for (const [path, item] of selected.value) {
    if (item.kind === 'file') files.push(path)
    else folders.push(path)
  }
  emit('confirm', { files, folders })
}

function toggleEntry(entry: FsEntry) {
  if (!isSelectable(entry)) return
  const next = new Map(selected.value)
  if (next.has(entry.path)) {
    next.delete(entry.path)
  } else {
    if (!props.multiple) next.clear()
    next.set(entry.path, {
      kind: entry.is_dir ? 'folder' : 'file',
      size: entry.size,
    })
  }
  selected.value = next
}

function toggleSelectAll() {
  if (!props.multiple) return
  const next = new Map(selected.value)
  if (allVisibleSelected.value) {
    for (const entry of selectableEntries.value) next.delete(entry.path)
  } else {
    for (const entry of selectableEntries.value) {
      next.set(entry.path, {
        kind: entry.is_dir ? 'folder' : 'file',
        size: entry.size,
      })
    }
  }
  selected.value = next
}

function clearSelection() {
  selected.value = new Map()
}

function selectCurrentFolder() {
  if (!currentFolderSelectable.value) return
  emit('confirm', { files: [], folders: [currentPath.value] })
}

function switchMode(mode: PickerMode) {
  if (activeMode.value === mode) return
  activeMode.value = mode
  selected.value = new Map()
}

function toggleSort() {
  sortOrder.value = sortOrder.value === 'desc' ? 'asc' : 'desc'
}

function handleRowClick(entry: FsEntry) {
  if (activeMode.value === 'file' && entry.is_dir) {
    void loadDirectory(entry.path)
    return
  }
  toggleEntry(entry)
}

function previewEntries(base: string): FsEntry[] {
  const now = Date.now() / 1000
  const at = (name: string) => `${base.replace(/[\\/]+$/, '')}/${name}`
  const make = (
    name: string,
    isDir: boolean,
    size: number | null,
    modifiedHours: number,
    createdHours: number,
    hidden = false,
  ): FsEntry => ({
    name,
    path: at(name),
    is_dir: isDir,
    is_symlink: false,
    hidden,
    size,
    modified: now - modifiedHours * 3600,
    created: now - createdHours * 3600,
  })
  return [
    make('今天的项目', true, null, 2, 3),
    make('刚刚下载.zip', false, 12 * 1024 * 1024, 1, 1),
    make('今天生成的报告.pdf', false, 2 * 1024 * 1024, 3, 4),
    make('昨天的文件.txt', false, 1024, 30, 30),
    make('旧项目', true, null, 48, 50),
    make('旧资料.zip', false, 80 * 1024 * 1024, 72, 80),
    make('.隐藏文件.txt', false, 2048, 5, 5, true),
  ]
}

function previewListing(path: string): FsListing {
  const normalized = path.trim().replace(/[\\/]+$/, '') || 'C:/示例目录'
  const home = 'C:/示例目录'
  const child = normalized.endsWith('今天的项目')
  const base = child ? normalized : home
  const now = Date.now() / 1000
  const entries = child
    ? [
        {
          name: '说明.txt',
          path: `${base}/说明.txt`,
          is_dir: false,
          is_symlink: false,
          hidden: false,
          size: 512,
          modified: now - 3600,
          created: now - 7200,
        },
      ]
    : previewEntries(base)
  return {
    path: base,
    parent: child ? home : 'C:/',
    entries,
    total: entries.length,
    truncated: false,
    skipped: [],
  }
}

function previewRoots(): FsRoot[] {
  return [
    { label: '示例目录', path: 'C:/示例目录', kind: 'home' },
    { label: '桌面', path: 'C:/示例目录/桌面', kind: 'desktop' },
    { label: '下载', path: 'C:/示例目录/下载', kind: 'downloads' },
    { label: 'C:', path: 'C:/', kind: 'drive' },
  ]
}

async function loadRoots() {
  if (!isDesktop) {
    roots.value = previewRoots()
    return
  }
  try {
    const result = await listRoots()
    roots.value = result.roots
  } catch {
    roots.value = []
  }
}

async function loadDirectory(path: string) {
  const requested = path.trim()
  if (!requested) {
    error.value = '请输入目录路径'
    return
  }
  const id = ++requestId
  loading.value = true
  error.value = null
  try {
    const listing = isDesktop ? await listDirectory(requested) : previewListing(requested)
    if (id !== requestId) return
    currentPath.value = listing.path
    pathInput.value = listing.path
    parentPath.value = listing.parent
    entries.value = listing.entries
    total.value = listing.total
    truncated.value = listing.truncated
    skippedCount.value = listing.skipped.length
    if (isDesktop) {
      try {
        localStorage.setItem(STORAGE_KEY, listing.path)
      } catch {
        // Storage may be unavailable; browsing still works.
      }
    }
  } catch (err) {
    if (id !== requestId) return
    error.value = err instanceof Error ? err.message : '无法读取目录'
  } finally {
    if (id === requestId) loading.value = false
  }
}

async function initialize() {
  await loadRoots()
  const fallback = roots.value.find(root => root.kind === 'home')?.path || roots.value[0]?.path || ''
  let initial = ''
  if (isDesktop) {
    try {
      initial = localStorage.getItem(STORAGE_KEY) ?? ''
    } catch {
      initial = ''
    }
  } else {
    initial = 'C:/示例目录'
  }
  if (!initial) initial = fallback
  if (!initial) {
    error.value = '无法获取可浏览的位置'
    loading.value = false
    return
  }
  await loadDirectory(initial)
  if (error.value && fallback && initial !== fallback) {
    error.value = null
    await loadDirectory(fallback)
  }
  await nextTick()
  pathInputRef.value?.focus()
  pathInputRef.value?.select()
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
  }
}

onMounted(() => {
  previousOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  document.addEventListener('keydown', handleKeydown)
  void initialize()
})

onUnmounted(() => {
  document.removeEventListener('keydown', handleKeydown)
  document.body.style.overflow = previousOverflow
})
</script>

<template>
  <Teleport to="body">
    <div class="modal-backdrop picker-backdrop" @click.self="close">
      <div
        class="modal-card picker-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="picker-title"
        tabindex="-1"
      >
        <div class="modal-head picker-head">
          <div class="picker-title-group">
            <FolderOpen :size="20" />
            <h3 id="picker-title">{{ dialogTitle }}</h3>
          </div>
          <button class="text-button" title="关闭" aria-label="关闭" @click="close">
            <X :size="16" />
          </button>
        </div>

        <div class="picker-path-row">
          <button
            class="icon-button small"
            :disabled="!parentPath || loading"
            title="上一级"
            aria-label="上一级"
            @click="parentPath && loadDirectory(parentPath)"
          >
            <ArrowUp :size="16" />
          </button>
          <input
            ref="pathInputRef"
            v-model="pathInput"
            class="picker-path-input"
            spellcheck="false"
            placeholder="输入绝对路径后回车"
            @keydown.enter.prevent="loadDirectory(pathInput)"
          />
          <button class="inline-action" :disabled="loading" @click="loadDirectory(pathInput)">转到</button>
          <button
            class="icon-button small"
            :disabled="loading || !currentPath"
            title="刷新"
            aria-label="刷新"
            @click="loadDirectory(currentPath)"
          >
            <RefreshCw :size="15" :class="{ spinning: loading }" />
          </button>
        </div>

        <div v-if="roots.length" class="picker-roots">
          <button
            v-for="root in roots"
            :key="root.path"
            class="hotkey-mod picker-root"
            :class="{ active: currentPath === root.path }"
            :title="root.path"
            :disabled="loading"
            @click="loadDirectory(root.path)"
          >{{ root.label }}</button>
        </div>

        <div class="picker-mode-row">
          <div class="auth-toggle picker-mode-toggle">
            <button :class="{ active: activeMode === 'file' }" @click="switchMode('file')">
              <File :size="14" />文件
            </button>
            <button :class="{ active: activeMode === 'folder' }" @click="switchMode('folder')">
              <Folder :size="14" />文件夹
            </button>
          </div>
          <span class="picker-mode-hint">
            {{ activeMode === 'folder'
              ? '文件夹按自身时间筛选；进入子目录请点右侧箭头。'
              : '文件夹始终显示用于导航；时间筛选只作用于文件。' }}
          </span>
        </div>

        <div class="picker-filter-row">
          <div class="picker-filter-chips">
            <button class="hotkey-mod" :class="{ active: recentFilter === 'all' }" @click="recentFilter = 'all'">全部</button>
            <button class="hotkey-mod" :class="{ active: recentFilter === '8h' }" @click="recentFilter = '8h'">近8小时</button>
            <button class="hotkey-mod" :class="{ active: recentFilter === '24h' }" @click="recentFilter = '24h'">近24小时</button>
          </div>
          <div class="auth-toggle picker-time-toggle">
            <button :class="{ active: timeField === 'modified' }" @click="timeField = 'modified'">修改时间</button>
            <button :class="{ active: timeField === 'created' }" @click="timeField = 'created'">创建时间</button>
          </div>
          <button class="inline-action picker-sort" @click="toggleSort">
            {{ sortOrder === 'desc' ? '最新优先 ↓' : '最早优先 ↑' }}
          </button>
          <label class="remember picker-hidden-toggle">
            <input v-model="showHidden" type="checkbox" />
            <span>显示隐藏项</span>
          </label>
        </div>

        <div class="picker-list-head">
          <span></span>
          <span></span>
          <span>名称</span>
          <span>{{ timeField === 'modified' ? '修改时间' : '创建时间' }}</span>
          <span>大小</span>
          <span></span>
        </div>

        <div class="picker-list">
          <div v-if="loading" class="picker-empty">
            <RefreshCw :size="16" class="spinning" /> 正在读取…
          </div>
          <div v-else-if="error" class="picker-empty picker-error">{{ error }}</div>
          <div v-else-if="!visibleEntries.length" class="picker-empty">
            此目录没有匹配的{{ activeMode === 'folder' ? '文件夹' : '文件' }}
          </div>
          <div
            v-for="entry in visibleEntries"
            :key="entry.path"
            class="picker-row"
            :class="{ selected: isSelected(entry.path) }"
            role="button"
            tabindex="0"
            @click="handleRowClick(entry)"
            @keydown.enter.prevent="handleRowClick(entry)"
          >
            <span class="picker-check">
              <input
                v-if="isSelectable(entry)"
                type="checkbox"
                :checked="isSelected(entry.path)"
                :aria-label="'选择 ' + entry.name"
                @click.stop
                @change="toggleEntry(entry)"
              />
            </span>
            <span class="picker-row-icon">
              <Folder v-if="entry.is_dir" :size="17" />
              <File v-else :size="17" />
            </span>
            <span class="picker-row-name" :title="entry.name">
              {{ entry.name }}<small v-if="entry.is_symlink">链接</small>
            </span>
            <span class="picker-row-time">{{ formatEntryTime(entry[timeField]) }}</span>
            <span class="picker-row-size">{{ entry.is_dir ? '文件夹' : formatSize(entry.size) }}</span>
            <button
              v-if="entry.is_dir"
              class="icon-button small picker-enter"
              :title="'进入 ' + entry.name"
              :aria-label="'进入 ' + entry.name"
              @click.stop="loadDirectory(entry.path)"
            >
              <ChevronRight :size="16" />
            </button>
          </div>
          <div v-if="truncated" class="picker-note">
            目录项较多，仅显示前 {{ entries.length }} / {{ total }} 项，请进入子目录缩小范围。
          </div>
          <div v-if="skippedCount" class="picker-note">{{ skippedCount }} 项无法读取，已跳过。</div>
        </div>

        <div class="picker-footer">
          <span class="picker-summary">
            已选
            <strong v-if="selectedFileCount">{{ selectedFileCount }} 个文件</strong>
            <template v-if="selectedFileCount && selectedFolderCount"> · </template>
            <strong v-if="selectedFolderCount">{{ selectedFolderCount }} 个文件夹</strong>
            <span v-if="!selectedFileCount && !selectedFolderCount">0 项</span>
          </span>
          <div class="picker-footer-actions">
            <button
              v-if="props.multiple"
              class="inline-action"
              :disabled="!selectableEntries.length"
              @click="toggleSelectAll"
            >{{ allVisibleSelected ? '取消全选当前目录' : '全选当前目录' }}</button>
            <button v-if="selected.size" class="inline-action" @click="clearSelection">清空选择</button>
            <button
              v-if="activeMode === 'folder'"
              class="inline-action"
              :disabled="!currentFolderSelectable"
              :title="currentFolderTitle"
              @click="selectCurrentFolder"
            >就用当前文件夹</button>
            <button class="inline-action" @click="close">取消</button>
            <button class="primary-button picker-confirm" :disabled="!canConfirm" @click="confirm">确定</button>
          </div>
        </div>
        <p v-if="props.multiple && activeMode === 'folder' && selectedFolderCount > 1" class="picker-note picker-footer-note">
          多选文件夹将按「日期_文件夹名」自动命名，稍后可在队列中修改。
        </p>
      </div>
    </div>
  </Teleport>
</template>
