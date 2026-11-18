import type { FsEntry } from './services/api'
import { defaultRemoteFolderName } from './store/transfer'

export type PickerMode = 'file' | 'folder'
export type RecentFilter = 'all' | '8h' | '24h'
export type TimeField = 'modified' | 'created'
export type SortOrder = 'asc' | 'desc'

export type PickerConfirmResult = {
  files: string[]
  folders: string[]
}

export function recentHours(filter: RecentFilter): number {
  if (filter === '8h') return 8
  if (filter === '24h') return 24
  return 0
}

export function filterEntries(
  entries: FsEntry[],
  mode: PickerMode,
  hours: number,
  timeField: TimeField,
  showHidden: boolean,
  nowSeconds = Date.now() / 1000,
): FsEntry[] {
  return entries.filter(entry => {
    if (entry.hidden && !showHidden) return false
    if (mode === 'folder' && !entry.is_dir) return false
    // Folders stay visible in file mode so the user can still navigate.
    if (mode === 'file' && entry.is_dir) return true
    if (hours <= 0) return true
    const stamp = entry[timeField]
    return typeof stamp === 'number' && stamp > 0 && nowSeconds - stamp <= hours * 3600
  })
}

export function sortEntries(
  entries: FsEntry[],
  timeField: TimeField,
  order: SortOrder,
): FsEntry[] {
  return [...entries].sort((left, right) => {
    const diff = (left[timeField] ?? 0) - (right[timeField] ?? 0)
    if (diff !== 0) return order === 'asc' ? diff : -diff
    return left.name.localeCompare(right.name, 'zh-CN', {
      numeric: true,
      sensitivity: 'base',
    })
  })
}

/** Assign unique date_folder names to a batch of selected folders. */
export function uniqueRemoteNames(
  paths: string[],
  existingNames: Iterable<string> = [],
): Map<string, string> {
  const used = new Set(
    [...existingNames]
      .map(name => name.trim().toLowerCase())
      .filter(Boolean),
  )
  const names = new Map<string, string>()
  for (const path of paths) {
    let base = defaultRemoteFolderName(path)
    if (base.length > 240) base = base.slice(0, 240)
    let candidate = base
    let suffix = 2
    while (used.has(candidate.toLowerCase())) {
      candidate = `${base}_${suffix}`
      suffix += 1
    }
    used.add(candidate.toLowerCase())
    names.set(path, candidate)
  }
  return names
}

export function formatEntryTime(epochSeconds: number): string {
  if (!Number.isFinite(epochSeconds) || epochSeconds <= 0) return '—'
  const date = new Date(epochSeconds * 1000)
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

export function isDriveRoot(path: string): boolean {
  const trimmed = path.trim().replace(/[\\/]+$/, '')
  if (!trimmed || trimmed === '/') return true
  if (/^[A-Za-z]:$/.test(trimmed)) return true
  return /^\\\\[^\\]+\\[^\\]+$/.test(trimmed)
}
