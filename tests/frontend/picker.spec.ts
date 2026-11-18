import { describe, expect, it } from 'vitest'
import {
  filterEntries,
  formatEntryTime,
  isDriveRoot,
  recentHours,
  sortEntries,
  uniqueRemoteNames,
} from '../../src/frontend/picker'
import { defaultRemoteFolderName } from '../../src/frontend/store/transfer'
import type { FsEntry } from '../../src/frontend/services/api'

const NOW = 1_800_000_000

function entry(
  name: string,
  options: Partial<FsEntry> & { ageHours?: number } = {},
): FsEntry {
  const age = options.ageHours ?? 0
  return {
    name,
    path: `C:/data/${name}`,
    is_dir: options.is_dir ?? false,
    is_symlink: false,
    hidden: options.hidden ?? false,
    size: options.size ?? 10,
    modified: options.modified ?? NOW - age * 3600,
    created: options.created ?? NOW - age * 3600,
  }
}

describe('picker filters and sorting', () => {
  it('filters files by the selected time field', () => {
    const entries = [
      entry('fresh.txt', { ageHours: 1 }),
      entry('day-old.txt', { ageHours: 20 }),
      entry('old.txt', { ageHours: 30 }),
      entry('folder', { is_dir: true, ageHours: 100 }),
    ]

    expect(filterEntries(entries, 'file', 8, 'modified', true, NOW).map(item => item.name))
      .toEqual(['fresh.txt', 'folder'])
    expect(filterEntries(entries, 'file', 24, 'modified', true, NOW).map(item => item.name))
      .toEqual(['fresh.txt', 'day-old.txt', 'folder'])
  })

  it('switches filter and sort to the creation timestamp', () => {
    const entries = [
      entry('old-created.txt', {
        modified: NOW - 3600,
        created: NOW - 48 * 3600,
      }),
      entry('new-created.txt', {
        modified: NOW - 48 * 3600,
        created: NOW - 3600,
      }),
    ]

    const visible = filterEntries(entries, 'file', 8, 'created', true, NOW)
    expect(visible.map(item => item.name)).toEqual(['new-created.txt'])
    expect(
      sortEntries(visible, 'created', 'desc').map(item => item.name),
    ).toEqual(['new-created.txt'])
  })

  it('shows only folders in folder mode and applies the recent filter', () => {
    const entries = [
      entry('recent-folder', { is_dir: true, ageHours: 2 }),
      entry('old-folder', { is_dir: true, ageHours: 30 }),
      entry('file.txt', { ageHours: 1 }),
    ]

    expect(filterEntries(entries, 'folder', 8, 'modified', true, NOW).map(item => item.name))
      .toEqual(['recent-folder'])
    expect(filterEntries(entries, 'folder', 0, 'modified', true, NOW).map(item => item.name))
      .toEqual(['recent-folder', 'old-folder'])
  })

  it('hides hidden entries unless requested', () => {
    const entries = [entry('visible.txt'), entry('secret.txt', { hidden: true })]

    expect(filterEntries(entries, 'file', 0, 'modified', false, NOW).map(item => item.name))
      .toEqual(['visible.txt'])
    expect(filterEntries(entries, 'file', 0, 'modified', true, NOW).map(item => item.name))
      .toEqual(['visible.txt', 'secret.txt'])
  })

  it('sorts by time in both directions and falls back to names', () => {
    const entries = [
      entry('b.txt', { ageHours: 2 }),
      entry('a.txt', { ageHours: 2 }),
      entry('new.txt', { ageHours: 1 }),
    ]

    expect(sortEntries(entries, 'modified', 'desc').map(item => item.name))
      .toEqual(['new.txt', 'a.txt', 'b.txt'])
    expect(sortEntries(entries, 'modified', 'asc').map(item => item.name))
      .toEqual(['a.txt', 'b.txt', 'new.txt'])
  })

  it('exposes helper values', () => {
    expect(recentHours('all')).toBe(0)
    expect(recentHours('8h')).toBe(8)
    expect(recentHours('24h')).toBe(24)
    expect(isDriveRoot('C:\\')).toBe(true)
    expect(isDriveRoot('C:/')).toBe(true)
    expect(isDriveRoot('C:/Users')).toBe(false)
    expect(isDriveRoot('/')).toBe(true)
    expect(formatEntryTime(0)).toBe('—')
    expect(formatEntryTime(NOW)).toContain('2027')
  })
})

describe('uniqueRemoteNames', () => {
  it('suffixes colliding folder names', () => {
    const paths = ['C:/a/project', 'D:/b/project', 'E:/c/project']
    const first = defaultRemoteFolderName(paths[0])
    const names = uniqueRemoteNames(paths)

    expect(names.get(paths[0])).toBe(first)
    expect(names.get(paths[1])).toBe(`${first}_2`)
    expect(names.get(paths[2])).toBe(`${first}_3`)
    expect(new Set(names.values()).size).toBe(3)
  })

  it('avoids names already used in the queue', () => {
    const path = 'C:/a/project'
    const first = defaultRemoteFolderName(path)
    const names = uniqueRemoteNames([path], [first])

    expect(names.get(path)).toBe(`${first}_2`)
  })

  it('keeps the date_folder prefix', () => {
    const names = uniqueRemoteNames(['C:/work/项目'], [])
    const name = names.get('C:/work/项目')!

    expect(name).toMatch(/^\d{4}-\d{2}-\d{2}_项目$/)
  })
})
