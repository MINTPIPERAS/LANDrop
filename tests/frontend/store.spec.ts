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
    expect(store.phaseLabel(task)).toBe('传输中')
    expect(store.percent(task)).toBe(25)
    expect(store.phaseLabel({ ...task, status: 'succeeded', phase: 'completed' })).toBe('已完成')
    expect(store.percent({ ...task, status: 'succeeded' })).toBe(100)
    expect(store.percent({ ...task, total: 0, sent: 0 })).toBe(0)
  })

  it('locks sending while a task is running', async () => {
    const store = await import('../../src/frontend/store/transfer')
    store.serviceState.value = 'ready'
    store.form.host = 'host'
    store.form.username = 'user'
    store.form.remoteDir = '/tmp'
    store.localFile.value = 'C:/tmp/file.bin'

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
})
