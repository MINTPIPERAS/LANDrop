import { beforeEach, describe, expect, it, vi } from 'vitest'
import { nextTick } from 'vue'

const serviceStatus = vi.fn()

vi.mock('@tauri-apps/api/core', () => ({
  invoke: (...args: unknown[]) => serviceStatus(...args),
}))

vi.mock('@tauri-apps/plugin-dialog', () => ({
  open: vi.fn(),
}))

vi.mock('@tauri-apps/plugin-global-shortcut', () => ({
  isRegistered: vi.fn().mockResolvedValue(false),
  register: vi.fn().mockResolvedValue(undefined),
  unregister: vi.fn().mockResolvedValue(undefined),
}))

const fetchMock = vi.fn()

describe('transfer store', () => {
  beforeEach(() => {
    vi.resetModules()
    fetchMock.mockReset()
    serviceStatus.mockReset()
    serviceStatus.mockResolvedValue({ baseUrl: 'http://127.0.0.1:1234', token: 't'.repeat(48) })
    globalThis.fetch = fetchMock as unknown as typeof fetch
    // The store only talks to the service when the Tauri bridge is present.
    ;(window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {}
  })

  it('reports preview mode when the Tauri bridge is absent', async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__
    const store = await import('../../src/frontend/store/transfer')
    await store.connectService()

    expect(store.serviceState.value).toBe('error')
    expect(store.serviceMessage.value).toContain('浏览器预览模式')
    expect(serviceStatus).not.toHaveBeenCalled()
  })

  it('connects to the local service and reports readiness', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          host: '192.168.1.23',
          port: 22,
          username: 'lin',
          remote_dir: '/incoming',
          auth_method: 'key',
          private_key_path: '',
          hotkey_modifiers: ['ctrl', 'alt'],
          hotkey_key: 'space',
        }),
        { status: 200 },
      ),
    )
    fetchMock.mockResolvedValueOnce(
      new Response(JSON.stringify({ saved: false }), { status: 200 }),
    )

    const store = await import('../../src/frontend/store/transfer')
    await store.connectService()

    expect(store.serviceState.value).toBe('ready')
    expect(store.form.host).toBe('192.168.1.23')
    expect(fetchMock.mock.calls[0][1].headers['X-LANDrop-Token']).toBe('t'.repeat(48))
  })

  it('surfaces service startup failures without crashing', async () => {
    serviceStatus.mockRejectedValueOnce(new Error('本机服务未能在限定时间内就绪'))
    const store = await import('../../src/frontend/store/transfer')
    await store.connectService()

    expect(store.serviceState.value).toBe('error')
    expect(store.serviceMessage.value).toContain('就绪')
  })

  it('formats byte counts and phases for the UI', async () => {
    const store = await import('../../src/frontend/store/transfer')

    expect(store.formatBytes(0)).toBe('0 B')
    expect(store.formatBytes(1024)).toBe('1.0 KB')
    expect(store.formatBytes(5 * 1024 ** 3)).toBe('5.0 GB')
    expect(store.formatSpeed(0)).toBe('—')

    const task = {
      id: 'x',
      status: 'running' as const,
      phase: 'transferring' as const,
      sent: 50,
      total: 200,
      speed: 1024,
      message: '',
      error: null,
      result: null,
      request: {},
    }
    expect(store.phaseLabel(task)).toBe('正在发送')
    expect(store.percent(task)).toBe(25)
    expect(store.phaseLabel({ ...task, status: 'succeeded', phase: 'completed' })).toBe('已完成')
    expect(store.phaseLabel({ ...task, status: 'partial', phase: 'partial' })).toBe('部分完成')
    expect(store.percent({ ...task, status: 'succeeded' })).toBe(100)
    expect(store.percent({ ...task, total: 0, sent: 0 })).toBe(0)
  })

  it('scans a folder before queueing it', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          root: 'D:/project',
          total_files: 12,
          total_size: 2048,
          empty_dirs: 1,
          skipped: [],
        }),
        { status: 200 },
      ),
    )
    const store = await import('../../src/frontend/store/transfer')

    const error = await store.addFolder('D:/project')

    expect(error).toBeNull()
    expect(store.queue.value).toHaveLength(1)
    expect(store.queue.value[0]).toMatchObject({
      path: 'D:/project',
      kind: 'folder',
      size: 2048,
      fileCount: 12,
    })
    expect(store.queueSummary.value).toEqual({ count: 1, size: 2048 })
  })

  it('deduplicates queued paths case-insensitively', async () => {
    const store = await import('../../src/frontend/store/transfer')

    expect(store.addFiles(['C:/a.bin', 'c:/A.bin', 'C:/b.bin'])).toBe(2)
    expect(store.queue.value.map(item => item.path)).toEqual(['C:/a.bin', 'C:/b.bin'])
    expect(store.addFiles(['C:/a.bin'])).toBe(0)

    store.removeSource(store.queue.value[0].key)
    expect(store.queue.value.map(item => item.path)).toEqual(['C:/b.bin'])

    store.clearQueue()
    expect(store.queue.value).toEqual([])
  })

  it('marks folders as recursive in the transfer payload', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.form.host = 'host'
    store.form.username = 'user'
    store.form.remoteDir = '/incoming'
    store.form.authMethod = 'password'
    store.form.password = 'secret'

    const filePayload = store.transferPayload({
      path: 'C:/tmp/a.bin',
      kind: 'file',
      size: 0,
      fileCount: 1,
    })
    expect(filePayload.recursive).toBe(false)
    expect(filePayload.local_path).toBe('C:/tmp/a.bin')
    expect(filePayload.password).toBe('secret')

    const folderPayload = store.transferPayload({
      path: 'C:/tmp/project',
      kind: 'folder',
      size: 100,
      fileCount: 3,
    })
    expect(folderPayload.recursive).toBe(true)
    expect(folderPayload.local_path).toBe('C:/tmp/project')

    store.form.authMethod = 'key'
    store.form.privateKeyPath = 'C:/keys/id'
    const keyPayload = store.transferPayload({
      path: 'C:/tmp/project',
      kind: 'folder',
      size: 100,
      fileCount: 3,
    })
    expect(keyPayload.private_key_path).toBe('C:/keys/id')
    expect(keyPayload.password).toBeUndefined()

    const overwritePayload = store.transferPayload({
      path: 'C:/tmp/project',
      kind: 'folder',
      size: 100,
      fileCount: 3,
      overwrite: true,
    })
    expect(overwritePayload.overwrite).toBe(true)
  })

  function snapshot(id: string, status: string, request: object = {}) {
    return {
      id,
      status,
      phase: status === 'running' ? 'transferring' : 'completed',
      sent: 1,
      total: 1,
      speed: 0,
      message: '',
      error: status === 'failed' ? '远端目标文件已存在' : null,
      error_code: status === 'failed' ? 'target_exists' : null,
      current_file: null,
      files_done: 0,
      files_total: 0,
      verified: 0,
      verify_total: 0,
      created_at: 1,
      source: 'manual',
      result: null,
      request,
    }
  }

  function sseResponse(payload: unknown) {
    const encoder = new TextEncoder()
    const frames = [`id: 1\ndata: ${JSON.stringify(payload)}\n\n`]
    let index = 0
    return new Response(
      new ReadableStream({
        pull(controller) {
          if (index < frames.length) controller.enqueue(encoder.encode(frames[index++]))
          else controller.close()
        },
      }),
      { status: 200 },
    )
  }

  it('sends queued sources one at a time and keeps the failures', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.serviceState.value = 'ready'
    store.form.host = 'host'
    store.form.username = 'user'
    store.form.remoteDir = '/incoming'
    store.form.authMethod = 'password'
    store.form.password = 'secret'
    store.addFiles(['C:/one.bin', 'C:/two.bin', 'C:/three.bin'])

    const created: string[] = []
    const outcomes: Record<string, string> = {}
    let sequence = 0
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/config') && method === 'PUT') {
        return new Response(JSON.stringify({}), { status: 200 })
      }
      if (url.endsWith('/api/transfers') && method === 'POST') {
        const body = JSON.parse(String(init?.body))
        const id = `task-${++sequence}`
        created.push(body.local_path)
        outcomes[id] = body.local_path.endsWith('two.bin') ? 'failed' : 'succeeded'
        return new Response(JSON.stringify(snapshot(id, 'running', body)), { status: 200 })
      }
      if (url.includes('/events')) {
        const id = url.split('/').at(-2) ?? ''
        return sseResponse(snapshot(id, outcomes[id]))
      }
      if (url.includes('/api/transfers/')) {
        const id = url.split('/').at(-1) ?? ''
        return new Response(JSON.stringify(snapshot(id, outcomes[id] ?? 'succeeded')), {
          status: 200,
        })
      }
      throw new Error(`unexpected request: ${method} ${url}`)
    })

    await store.sendQueue()

    // Serial dispatch preserves the queue order.
    expect(created).toEqual(['C:/one.bin', 'C:/two.bin', 'C:/three.bin'])
    // The failed item stays queued with its error code; successes are dropped.
    expect(store.queue.value.map(item => item.path)).toEqual(['C:/two.bin'])
    expect(store.queue.value[0].errorCode).toBe('target_exists')
    expect(store.batch.value.ok).toBe(2)
    expect(store.batch.value.failed).toBe(1)
    expect(store.batchMessage.value).toContain('失败 1')
    expect(store.hasTargetConflicts.value).toBe(true)
  })

  it('retries target conflicts with the overwrite flag', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.serviceState.value = 'ready'
    store.form.host = 'host'
    store.form.username = 'user'
    store.form.remoteDir = '/incoming'
    store.form.authMethod = 'password'
    store.form.password = 'secret'
    store.queue.value = [
      {
        key: 'c:/dup.bin',
        path: 'C:/dup.bin',
        kind: 'file',
        size: 0,
        fileCount: 1,
        error: '远端目标文件已存在',
        errorCode: 'target_exists',
      },
    ]

    const overwriteFlags: boolean[] = []
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/config') && method === 'PUT') {
        return new Response(JSON.stringify({}), { status: 200 })
      }
      if (url.endsWith('/api/transfers') && method === 'POST') {
        const body = JSON.parse(String(init?.body))
        overwriteFlags.push(body.overwrite)
        return new Response(JSON.stringify(snapshot('task-1', 'running', body)), { status: 200 })
      }
      if (url.includes('/events')) {
        return sseResponse(snapshot('task-1', 'succeeded'))
      }
      return new Response(JSON.stringify(snapshot('task-1', 'succeeded')), { status: 200 })
    })

    await store.retryFailedWithOverwrite()

    expect(overwriteFlags).toEqual([true])
    expect(store.queue.value).toEqual([])
    expect(store.batch.value.ok).toBe(1)
  })

  it('persists the close-to-tray preference and pushes it to the shell', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.settings.closeToTray = false

    let saved: Record<string, unknown> | null = null
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/config') && method === 'PUT') {
        saved = JSON.parse(String(init?.body))
        return new Response(JSON.stringify(saved), { status: 200 })
      }
      throw new Error(`unexpected request: ${method} ${url}`)
    })

    const error = await store.updateSystemSettings()

    expect(error).toBeNull()
    expect(saved!.close_to_tray).toBe(false)
    expect(serviceStatus).toHaveBeenCalledWith('set_close_to_tray', { enabled: false })
  })

  it('adds a schedule and persists it', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.schedules.value = []
    store.scheduleDraft.deviceId = 'd1'
    store.scheduleDraft.sourcePath = 'C:/src/a.bin'
    store.scheduleDraft.name = '备份'
    store.scheduleDraft.mode = 'daily'
    store.scheduleDraft.time = '09:00'

    let saved: Record<string, any> | null = null
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/config') && method === 'PUT') {
        saved = JSON.parse(String(init?.body))
        return new Response(JSON.stringify(saved), { status: 200 })
      }
      if (url.endsWith('/api/schedules')) {
        return new Response(
          JSON.stringify({ schedules: [], system_tasks: false }),
          { status: 200 },
        )
      }
      throw new Error(`unexpected request: ${method} ${url}`)
    })

    const error = await store.addSchedule()

    expect(error).toBeNull()
    expect(store.schedules.value).toHaveLength(1)
    expect(saved!.schedules[0]).toMatchObject({
      name: '备份',
      source_path: 'C:/src/a.bin',
      device_id: 'd1',
      mode: 'daily',
      enabled: true,
    })
    expect(saved!.schedules[0].weekdays).toEqual([])
  })

  it('rejects a schedule without a source or target device', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.scheduleDraft.sourcePath = ''
    store.scheduleDraft.deviceId = 'd1'
    expect(await store.addSchedule()).toContain('文件或文件夹')

    store.scheduleDraft.sourcePath = 'C:/src/a.bin'
    store.scheduleDraft.deviceId = ''
    expect(await store.addSchedule()).toContain('目标设备')

    store.scheduleDraft.deviceId = 'd1'
    store.scheduleDraft.mode = 'weekly'
    store.scheduleDraft.weekdays = []
    expect(await store.addSchedule()).toContain('至少选择一天')
  })

  it('toggles a schedule and persists the new state', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.schedules.value = [
      {
        id: 's1',
        name: '备份',
        enabled: true,
        source_path: 'C:/src/a.bin',
        recursive: false,
        device_id: 'd1',
        remote_dir: '',
        overwrite: false,
        mode: 'daily',
        time: '09:00',
        date: '',
        weekdays: [],
        last_run_at: '',
        last_run_status: '',
      },
    ]

    let saved: Record<string, any> | null = null
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/config') && method === 'PUT') {
        saved = JSON.parse(String(init?.body))
        return new Response(JSON.stringify(saved), { status: 200 })
      }
      if (url.endsWith('/api/schedules')) {
        return new Response(
          JSON.stringify({ schedules: [], system_tasks: true }),
          { status: 200 },
        )
      }
      throw new Error(`unexpected request: ${method} ${url}`)
    })

    await store.toggleSchedule('s1')

    expect(store.schedules.value[0].enabled).toBe(false)
    expect(saved!.schedules[0].enabled).toBe(false)
  })

  it('runs a schedule immediately and tracks the created task', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.schedules.value = [
      {
        id: 's1',
        name: '备份',
        enabled: true,
        source_path: 'C:/src/a.bin',
        recursive: false,
        device_id: 'd1',
        remote_dir: '',
        overwrite: false,
        mode: 'daily',
        time: '09:00',
        date: '',
        weekdays: [],
        last_run_at: '',
        last_run_status: '',
      },
    ]

    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith('/api/schedules/s1/run')) {
        return new Response(JSON.stringify(snapshot('task-9', 'running')), { status: 200 })
      }
      if (url.endsWith('/api/schedules')) {
        return new Response(
          JSON.stringify({ schedules: [{ id: 's1', next_run_at: null, missed: false }], system_tasks: true }),
          { status: 200 },
        )
      }
      if (url.includes('/events')) {
        return sseResponse(snapshot('task-9', 'succeeded'))
      }
      return new Response(JSON.stringify(snapshot('task-9', 'succeeded')), { status: 200 })
    })

    const error = await store.runScheduleNow('s1')

    expect(error).toBeNull()
    expect(store.tasks.value.some(task => task.id === 'task-9')).toBe(true)
    expect(store.activeTaskId.value).toBe('task-9')
  })

  it('merges backend tasks so scheduled runs appear in the list', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.tasks.value = [snapshot('local-1', 'succeeded') as any]

    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          tasks: [
            { ...snapshot('sched-1', 'running'), source: 'schedule', created_at: 200 },
            { ...snapshot('local-1', 'succeeded'), created_at: 100 },
          ],
        }),
        { status: 200 },
      ),
    )
    // The running task discovered by polling gets an SSE subscription.
    fetchMock.mockResolvedValueOnce(sseResponse(snapshot('sched-1', 'running')))

    await store.refreshTasks()

    expect(store.tasks.value.map(task => task.id)).toEqual(['sched-1', 'local-1'])
    expect(store.tasks.value[0].source).toBe('schedule')
  })

  it('folds scheduler run results into the local schedule rows', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.schedules.value = [
      {
        id: 's1',
        name: '备份',
        enabled: true,
        source_path: 'C:/src/a.bin',
        recursive: false,
        device_id: 'd1',
        remote_dir: '',
        overwrite: false,
        mode: 'daily',
        time: '09:00',
        date: '',
        weekdays: [],
        last_run_at: '',
        last_run_status: '',
      },
    ]
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          schedules: [
            {
              id: 's1',
              next_run_at: '2026-11-10T09:00:00',
              missed: false,
              last_run_at: '2026-11-09T09:00:05',
              last_run_status: 'succeeded',
              enabled: true,
            },
          ],
          system_tasks: true,
        }),
        { status: 200 },
      ),
    )

    await store.refreshScheduleStatus()

    expect(store.schedules.value[0].last_run_at).toBe('2026-11-09T09:00:05')
    expect(store.schedules.value[0].last_run_status).toBe('succeeded')
    expect(store.scheduleStatus.value.s1.missed).toBe(false)
  })

  it('locks sending while a task is running', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.serviceState.value = 'ready'
    store.form.host = 'host'
    store.form.username = 'user'
    store.form.remoteDir = '/tmp'
    store.queue.value = [
      { key: 'c:/tmp/file.bin', path: 'C:/tmp/file.bin', kind: 'file', size: 0, fileCount: 1 },
    ]

    expect(store.canSend.value).toBe(true)

    store.tasks.value = [
      {
        id: 'run-1',
        status: 'running',
        phase: 'transferring',
        sent: 1,
        total: 10,
        speed: 1,
        message: '',
        error: null,
        result: null,
        request: {},
      },
    ]
    store.activeTaskId.value = 'run-1'
    await nextTick()

    expect(store.isLocked.value).toBe(true)
    expect(store.canSend.value).toBe(false)
  })

  it('defaults new receivers to password auth', async () => {
    const store = await import('../../src/frontend/store/transfer')

    expect(store.form.authMethod).toBe('password')
  })

  it('explains why sending is blocked', async () => {
    const store = await import('../../src/frontend/store/transfer')

    store.serviceState.value = 'error'
    expect(store.sendBlockReason.value).toContain('重新打开')

    store.serviceState.value = 'connecting'
    expect(store.sendBlockReason.value).toContain('启动')

    store.serviceState.value = 'ready'
    expect(store.sendBlockReason.value).toContain('选择')

    store.queue.value = [
      { key: 'c:/tmp/file.bin', path: 'C:/tmp/file.bin', kind: 'file', size: 0, fileCount: 1 },
    ]
    expect(store.sendBlockReason.value).toContain('IP')

    store.form.host = 'host'
    expect(store.sendBlockReason.value).toContain('用户名')

    store.form.username = 'user'
    expect(store.sendBlockReason.value).toContain('保存位置')

    store.form.remoteDir = '/tmp'
    expect(store.sendBlockReason.value).toBe('')
  })

  it('maps transfer error codes to plain-language hints', async () => {
    const store = await import('../../src/frontend/store/transfer')

    expect(store.errorHint('auth_failed')).toContain('用户名')
    expect(store.errorHint('timeout')).toContain('网络')
    expect(store.errorHint(null)).toBeNull()
    expect(store.errorHint('unknown_code')).toBeNull()
  })

  it('saves the current receiver and refreshes the sidebar entry', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.form.deviceName = '书房电脑'
    store.form.host = '192.168.1.50'
    store.form.username = 'lin'
    store.form.remoteDir = 'D:/LANDrop'

    const saved: Record<string, unknown> = {}
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/config') && method === 'PUT') {
        Object.assign(saved, JSON.parse(String(init?.body)))
        return new Response(JSON.stringify({}), { status: 200 })
      }
      if (url.includes('/api/credentials')) {
        return new Response(JSON.stringify({ saved: false }), { status: 200 })
      }
      return new Response('{}', { status: 404 })
    })

    const error = await store.saveConnection()

    expect(error).toBeNull()
    expect(store.devices.value).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ name: '书房电脑', host: '192.168.1.50' }),
      ]),
    )
    expect(saved.devices).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ name: '书房电脑', host: '192.168.1.50' }),
      ]),
    )
  })

  it('saves a remembered password together with the receiver', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.form.host = '192.168.1.51'
    store.form.username = 'lin'
    store.form.remoteDir = 'D:/LANDrop'
    store.form.authMethod = 'password'
    store.form.password = 'secret'
    store.form.rememberPassword = true

    let credentialSaved = false
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/config') && method === 'PUT') {
        return new Response(JSON.stringify({}), { status: 200 })
      }
      if (url.includes('/api/credentials') && method === 'PUT') {
        credentialSaved = true
        return new Response(JSON.stringify({ saved: true }), { status: 200 })
      }
      if (url.includes('/api/credentials')) {
        return new Response(JSON.stringify({ saved: true }), { status: 200 })
      }
      return new Response('{}', { status: 404 })
    })

    expect(await store.saveConnection()).toBeNull()
    expect(credentialSaved).toBe(true)
  })

  it('refuses to save a receiver without required fields', async () => {
    const store = await import('../../src/frontend/store/transfer')

    expect(await store.saveConnection()).toContain('IP')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('parses SSE frames and keeps the last event id for reconnects', async () => {
    const encoder = new TextEncoder()
    const frames = [
      'id: 1\ndata: {"id":"t1","status":"running","phase":"transferring","sent":5,"total":10,"speed":1,"message":"正在传输","error":null,"result":null,"request":{}}\n\n',
      'id: 2\ndata: {"id":"t1","status":"succeeded","phase":"completed","sent":10,"total":10,"speed":0,"message":"传输完成","error":null,"result":null,"request":{}}\n\n',
    ]
    let index = 0
    const body = new ReadableStream({
      pull(controller) {
        if (index < frames.length) controller.enqueue(encoder.encode(frames[index++]))
        else controller.close()
      },
    })
    fetchMock.mockResolvedValueOnce(new Response(body, { status: 200 }))

    const api = await import('../../src/frontend/services/api')
    const snapshots: unknown[] = []
    const stream = api.subscribeTransfer('t1', {
      onSnapshot: snapshot => snapshots.push(snapshot),
      onError: () => undefined,
    })

    await new Promise(resolve => setTimeout(resolve, 80))
    stream.close()

    expect(snapshots).toHaveLength(2)
    expect((snapshots[1] as { status: string }).status).toBe('succeeded')
  })

  it('builds the accelerator string the shortcut plugin parses', async () => {
    const store = await import('../../src/frontend/store/transfer')

    expect(store.hotkeyAccelerator({ modifiers: ['ctrl', 'alt'], key: 'space' })).toBe(
      'Ctrl+Alt+space',
    )
    expect(store.hotkeyAccelerator({ modifiers: ['shift'], key: 'f9' })).toBe('Shift+f9')
    // Canonical order matters: the plugin rejects "Ctrl+C+Shift".
    expect(store.hotkeyAccelerator({ modifiers: ['shift', 'ctrl'], key: 'k' })).toBe(
      'Ctrl+Shift+k',
    )
    expect(store.hotkeyAccelerator({ modifiers: ['super'], key: 'space' })).toBe('Super+space')
  })

  it('formats a readable hotkey label', async () => {
    const store = await import('../../src/frontend/store/transfer')

    expect(store.hotkeyLabel({ modifiers: ['ctrl', 'alt'], key: 'space' })).toBe(
      'Ctrl + Alt + Space',
    )
    expect(store.hotkeyLabel({ modifiers: ['ctrl'], key: 'k' })).toBe('Ctrl + K')
    expect(store.hotkeyLabel({ modifiers: ['super'], key: 'f9' })).toBe('Win + F9')
  })

  it('normalizes a stored hotkey and falls back when it is unusable', async () => {
    const store = await import('../../src/frontend/store/transfer')

    expect(store.normalizeHotkey(['ALT', 'ctrl', 'alt'], 'F9')).toEqual({
      modifiers: ['ctrl', 'alt'],
      key: 'f9',
    })
    expect(store.normalizeHotkey(['ctrl', 'hyper'], 'k')).toEqual({
      modifiers: ['ctrl'],
      key: 'k',
    })
    expect(store.normalizeHotkey([], 'space')).toEqual(store.DEFAULT_HOTKEY)
    expect(store.normalizeHotkey(['ctrl'], '')).toEqual(store.DEFAULT_HOTKEY)
    expect(store.normalizeHotkey(undefined, undefined)).toEqual(store.DEFAULT_HOTKEY)
  })

  it('rejects an edited hotkey instead of silently restoring the default', async () => {
    const store = await import('../../src/frontend/store/transfer')

    expect(store.buildHotkey(['ctrl', 'alt'], 'Space')).toEqual({
      modifiers: ['ctrl', 'alt'],
      key: 'space',
    })
    expect(store.buildHotkey(['ctrl'], 'f9')).toEqual({ modifiers: ['ctrl'], key: 'f9' })

    expect(store.buildHotkey([], 'space')).toBeNull()
    expect(store.buildHotkey(['ctrl'], '')).toBeNull()
    expect(store.buildHotkey(['ctrl'], '   ')).toBeNull()
    expect(store.buildHotkey(['ctrl'], 'a b')).toBeNull()
    expect(store.buildHotkey(['ctrl'], 'ctrl+c')).toBeNull()
    expect(store.buildHotkey(['ctrl'], 'a,b')).toBeNull()
    expect(store.buildHotkey(['ctrl'], 'x'.repeat(33))).toBeNull()
  })

  it('loads the hotkey from config and persists edits', async () => {
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          host: '192.168.1.23',
          port: 22,
          username: 'lin',
          remote_dir: '/incoming',
          auth_method: 'key',
          private_key_path: '',
          hotkey_modifiers: ['shift', 'ctrl'],
          hotkey_key: 'F9',
        }),
        { status: 200 },
      ),
    )
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ saved: false }), { status: 200 }))

    const store = await import('../../src/frontend/store/transfer')
    await store.connectService()

    expect(store.hotkey.value).toEqual({ modifiers: ['ctrl', 'shift'], key: 'f9' })

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }))
    const saveError = await store.saveHotkey({ modifiers: ['ctrl', 'alt'], key: 'k' })

    expect(saveError).toBeNull()
    const body = JSON.parse(fetchMock.mock.calls.at(-1)[1].body)
    expect(body.hotkey_modifiers).toEqual(['ctrl', 'alt'])
    expect(body.hotkey_key).toBe('k')
    expect(body.host).toBe('192.168.1.23')
  })

  const multiDeviceConfig = {
    host: '192.168.1.20',
    port: 22,
    username: 'lin',
    remote_dir: '/data',
    auth_method: 'password',
    private_key_path: '',
    hotkey_modifiers: ['ctrl', 'alt'],
    hotkey_key: 'space',
    devices: [
      {
        id: 'a',
        name: '家里',
        host: '192.168.1.20',
        port: 22,
        username: 'lin',
        remote_dir: '/data',
        auth_method: 'password',
        private_key_path: '',
      },
      {
        id: 'b',
        name: '办公室',
        host: '10.0.0.5',
        port: 2222,
        username: 'ops',
        remote_dir: '/incoming',
        auth_method: 'password',
        private_key_path: '',
      },
    ],
    active_device_id: 'a',
  }

  async function connectedStore() {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(multiDeviceConfig), { status: 200 }))
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ saved: true }), { status: 200 }))
    const store = await import('../../src/frontend/store/transfer')
    await store.connectService()
    return store
  }

  it('loads every saved device and the active one into the form', async () => {
    const store = await connectedStore()

    expect(store.devices.value.map(device => device.id)).toEqual(['a', 'b'])
    expect(store.activeDeviceId.value).toBe('a')
    expect(store.form.deviceName).toBe('家里')
    expect(store.form.host).toBe('192.168.1.20')
    expect(store.form.authMethod).toBe('password')
    // Log settings fall back to the documented defaults.
    expect(store.settings.logDir).toBe('')
    expect(store.settings.logRetentionDays).toBe(14)
  })

  it('persists log settings and refreshes the directory listing', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.settings.logDir = 'D:/landrop-logs'
    store.settings.logRetentionDays = 7

    let saved: Record<string, unknown> | null = null
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (url.endsWith('/api/config') && method === 'PUT') {
        saved = JSON.parse(String(init?.body))
        return new Response(JSON.stringify(saved), { status: 200 })
      }
      if (url.endsWith('/api/logs')) {
        return new Response(
          JSON.stringify({
            dir: 'D:/landrop-logs',
            default_dir: 'C:/Users/ASUS/AppData/Roaming/LANDrop/logs',
            files: [{ name: 'landrop.log', size: 10, modified: 1 }],
            total_size: 10,
          }),
          { status: 200 },
        )
      }
      throw new Error(`unexpected request: ${method} ${url}`)
    })

    const error = await store.applyLogSettings()

    expect(error).toBeNull()
    expect(saved!.log_dir).toBe('D:/landrop-logs')
    expect(saved!.log_retention_days).toBe(7)
    expect(store.logsInfo.value?.total_size).toBe(10)
    expect(store.logsInfo.value?.files).toHaveLength(1)
  })

  it('switches devices, persists the choice and clears the typed password', async () => {
    const store = await connectedStore()
    store.form.password = 'typed-for-device-a'

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }))
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ saved: false }), { status: 200 }))
    await store.switchDevice('b')

    expect(store.activeDeviceId.value).toBe('b')
    expect(store.form.host).toBe('10.0.0.5')
    expect(store.form.port).toBe(2222)
    expect(store.form.deviceName).toBe('办公室')
    // A password typed for device a must not leak into device b.
    expect(store.form.password).toBe('')

    const body = JSON.parse(fetchMock.mock.calls.at(-2)[1].body)
    expect(body.active_device_id).toBe('b')
    expect(body.devices.map((device: { id: string }) => device.id)).toEqual(['a', 'b'])
    expect(body.host).toBe('10.0.0.5')
  })

  it('adds a new device with password auth and makes it active', async () => {
    const store = await connectedStore()

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }))
    await store.addDevice()

    expect(store.devices.value).toHaveLength(3)
    const added = store.devices.value.at(-1)!
    expect(added.auth_method).toBe('password')
    expect(added.host).toBe('')
    expect(store.activeDeviceId.value).toBe(added.id)
    expect(store.form.deviceName).toBe('新的接收电脑')

    const body = JSON.parse(fetchMock.mock.calls.at(-1)[1].body)
    expect(body.active_device_id).toBe(added.id)
    expect(body.devices).toHaveLength(3)
  })

  it('deletes a device and falls back to the remaining one', async () => {
    const store = await connectedStore()

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }))
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ saved: false }), { status: 200 }))
    await store.deleteDevice('a')

    expect(store.devices.value.map(device => device.id)).toEqual(['b'])
    expect(store.activeDeviceId.value).toBe('b')
    expect(store.form.host).toBe('10.0.0.5')
  })

  it('keeps an empty device list when the last device is deleted', async () => {
    const store = await connectedStore()

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }))
    await store.deleteDevice('a')
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200 }))
    await store.deleteDevice('b')

    expect(store.devices.value).toEqual([])
    expect(store.activeDeviceId.value).toBe('')

    // The empty form needs no credential lookup, so the last call is the save.
    const body = JSON.parse(fetchMock.mock.calls.at(-1)[1].body)
    expect(body.devices).toEqual([])
    expect(body.active_device_id).toBe('')
  })
})
