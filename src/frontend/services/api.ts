import { invoke } from '@tauri-apps/api/core'

export type ServiceInfo = {
  baseUrl: string
  token: string
}

export type TransferStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type TransferPhase =
  | 'queued'
  | 'connecting'
  | 'checking'
  | 'transferring'
  | 'verifying'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type TransferResult = {
  remote_path: string
  size: number
  sha256: string
  resumed_from: number
}

export type TransferRequest = {
  host: string
  port: number
  username: string
  remote_dir: string
  local_path: string
  auth_method: 'key' | 'password'
  password?: string
  private_key_path?: string | null
  overwrite: boolean
  remember_password?: boolean
}

export type TransferSnapshot = {
  id: string
  status: TransferStatus
  phase: TransferPhase
  sent: number
  total: number
  speed: number
  message: string
  error: string | null
  result: TransferResult | null
  request: Omit<TransferRequest, 'password' | 'remember_password'>
}

export type HostKeyInfo = {
  host: string
  port: number
  key_type: string
  fingerprint: string
  trusted: boolean
}

export type SavedConfig = {
  host: string
  port: number
  username: string
  remote_dir: string
  auth_method: 'key' | 'password'
  private_key_path: string
  hotkey_modifiers: string[]
  hotkey_key: string
}

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

let cached: ServiceInfo | null = null

/** Ask the Tauri shell for the sidecar endpoint, starting it on first use. */
export async function ensureService(): Promise<ServiceInfo> {
  if (cached) return cached
  const info = await invoke<ServiceInfo>('service_status')
  cached = info
  return info
}

export function resetServiceCache() {
  cached = null
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const service = await ensureService()
  let response: Response
  try {
    response = await fetch(`${service.baseUrl}${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        'X-LANDrop-Token': service.token,
        ...(init.headers ?? {}),
      },
    })
  } catch {
    // ``fetch`` rejects with a browser-generated message that is useless to users.
    throw new ApiError(0, '无法连接本机传输服务，请重新打开应用')
  }
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string') detail = body.detail
      else if (Array.isArray(body?.detail)) detail = body.detail[0]?.msg ?? detail
    } catch {
      // keep the status-based message
    }
    throw new ApiError(response.status, detail)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export function health() {
  return request<{ status: string }>('/health')
}

export function getConfig() {
  return request<SavedConfig>('/api/config')
}

export function putConfig(config: SavedConfig) {
  return request<SavedConfig>('/api/config', {
    method: 'PUT',
    body: JSON.stringify(config),
  })
}

export function probeHostKey(host: string, port: number) {
  return request<HostKeyInfo>('/api/hostkeys/probe', {
    method: 'POST',
    body: JSON.stringify({ host, port }),
  })
}

export function trustHostKey(host: string, port: number, fingerprint: string) {
  return request<HostKeyInfo>('/api/hostkeys/trust', {
    method: 'POST',
    body: JSON.stringify({ host, port, fingerprint }),
  })
}

export function credentialStatus(username: string) {
  return request<{ saved: boolean }>(`/api/credentials/${encodeURIComponent(username)}`)
}

export function saveCredential(username: string, password: string) {
  return request<{ saved: boolean }>('/api/credentials', {
    method: 'PUT',
    body: JSON.stringify({ username, password }),
  })
}

export function deleteCredential(username: string) {
  return request<{ deleted: boolean }>(`/api/credentials/${encodeURIComponent(username)}`, {
    method: 'DELETE',
  })
}

export function createTransfer(payload: TransferRequest) {
  return request<TransferSnapshot>('/api/transfers', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function getTransfer(id: string) {
  return request<TransferSnapshot>(`/api/transfers/${id}`)
}

export function cancelTransfer(id: string) {
  return request<TransferSnapshot>(`/api/transfers/${id}/cancel`, { method: 'POST' })
}

export function retryTransfer(id: string, password?: string) {
  return request<TransferSnapshot>(`/api/transfers/${id}/retry`, {
    method: 'POST',
    body: JSON.stringify(password ? { password } : {}),
  })
}

export type EventStreamHandlers = {
  onSnapshot: (snapshot: TransferSnapshot) => void
  onError: (error: Error) => void
}

/**
 * Subscribe to task progress with automatic reconnect. ``Last-Event-ID`` lets the
 * service replay events missed while the connection was down.
 */
export function subscribeTransfer(
  id: string,
  handlers: EventStreamHandlers,
): { close: () => void } {
  let closed = false
  let controller: AbortController | null = null
  let lastEventId = 0
  let attempt = 0

  const connect = async () => {
    if (closed) return
    controller = new AbortController()
    try {
      const service = await ensureService()
      const response = await fetch(`${service.baseUrl}/api/transfers/${id}/events`, {
        headers: {
          'X-LANDrop-Token': service.token,
          ...(lastEventId ? { 'Last-Event-ID': String(lastEventId) } : {}),
        },
        signal: controller.signal,
      })
      if (!response.ok || !response.body) {
        throw new ApiError(response.status, `事件流连接失败 (${response.status})`)
      }
      attempt = 0
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      for (;;) {
        const { value, done } = await reader.read()
        if (done || closed) break
        buffer += decoder.decode(value, { stream: true })
        const frames = buffer.split('\n\n')
        buffer = frames.pop() ?? ''
        for (const frame of frames) {
          const dataLine = frame
            .split('\n')
            .find(line => line.startsWith('data: '))
          const idLine = frame.split('\n').find(line => line.startsWith('id: '))
          if (idLine) lastEventId = Number(idLine.slice(4)) || lastEventId
          if (!dataLine) continue
          try {
            handlers.onSnapshot(JSON.parse(dataLine.slice(6)) as TransferSnapshot)
          } catch {
            // ignore malformed frames
          }
        }
      }
      if (!closed) throw new Error('事件流已结束')
    } catch (error) {
      if (closed || (error instanceof DOMException && error.name === 'AbortError')) return
      handlers.onError(error instanceof Error ? error : new Error(String(error)))
      attempt += 1
      const delay = Math.min(1000 * 2 ** (attempt - 1), 8000)
      setTimeout(connect, delay)
    }
  }

  void connect()

  return {
    close() {
      closed = true
      controller?.abort()
    },
  }
}
