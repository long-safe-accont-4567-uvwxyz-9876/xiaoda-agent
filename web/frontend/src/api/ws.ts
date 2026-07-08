export interface WsEvent {
  type: string
  [key: string]: unknown
}

export class WsClient {
  private ws: WebSocket | null = null
  private reconnectAttempts = 0
  private maxReconnectDelay = 30000
  private listeners: Map<string, Set<(data: WsEvent) => void>> = new Map()
  private heartbeatInterval: ReturnType<typeof setInterval> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private _unauthorized = false
  public connected = false

  constructor(private url: string) {}

  get unauthorized() { return this._unauthorized }

  connect(token: string) {
    // 幂等：已连接且有效时，不重复断开重连（避免登录时 auth.login + AppLayout 重复调用导致竞态）
    if (this.ws && this.ws.readyState === WebSocket.OPEN && this.connected) {
      return
    }
    this._unauthorized = false
    this.reconnectAttempts = 0  // 重置重连计数器，避免 disconnect() 设置的 999 残留
    this.disconnect()
    const wsUrl = `${this.url}?token=${token}`
    this.ws = new WebSocket(wsUrl)

    this.ws.onopen = () => {
      this.connected = true
      this.reconnectAttempts = 0
      this.startHeartbeat()
      this.emit({ type: 'ws_connected' })
    }

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as WsEvent
        // Token 失效：服务端发送 UNAUTHORIZED 错误后关闭连接
        // 立即停止重连，避免循环弹错
        if (data.type === 'error' && data.code === 'UNAUTHORIZED') {
          this._unauthorized = true
          this.disconnect()
          // 清除本地 token 并跳转登录页
          localStorage.removeItem('token')
          localStorage.removeItem('expires_at')
          if (!location.hash.includes('/login')) {
            location.hash = '#/login'
          }
          return
        }
        this.emit(data)
      } catch { /* ignore */ }
    }

    this.ws.onclose = (event) => {
      this.connected = false
      this.stopHeartbeat()
      this.emit({ type: 'ws_disconnected' })
      // 4001 = token 失效，不重连
      if (event.code === 4001 || this._unauthorized) return
      this.scheduleReconnect(token)
    }

    this.ws.onerror = () => {
      this.ws?.close()
    }
  }

  disconnect() {
    this.stopHeartbeat()
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
    this.reconnectAttempts = 999 // prevent reconnect
    this.ws?.close()
    this.ws = null
    this.connected = false
  }

  send(data: Record<string, unknown>) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(data))
    }
  }

  on(type: string, handler: (data: WsEvent) => void) {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, new Set())
    }
    this.listeners.get(type)!.add(handler)
  }

  off(type: string, handler: (data: WsEvent) => void) {
    this.listeners.get(type)?.delete(handler)
  }

  private emit(data: WsEvent) {
    const handlers = this.listeners.get(data.type)
    if (handlers) {
      handlers.forEach(h => h(data))
    }
    // Also notify wildcard listeners
    const wildcardHandlers = this.listeners.get('*')
    if (wildcardHandlers) {
      wildcardHandlers.forEach(h => h(data))
    }
  }

  private startHeartbeat() {
    this.stopHeartbeat()
    this.heartbeatInterval = setInterval(() => {
      this.send({ type: 'ping' })
    }, 25000)
  }

  private stopHeartbeat() {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval)
      this.heartbeatInterval = null
    }
  }

  private scheduleReconnect(token: string) {
    if (this.reconnectAttempts >= 20) return
    const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), this.maxReconnectDelay)
    this.reconnectAttempts++
    this.reconnectTimer = setTimeout(() => this.connect(token), delay)
  }
}

let instance: WsClient | null = null

export function getWsClient(): WsClient {
  if (!instance) {
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    instance = new WsClient(`${protocol}//${location.host}/ws`)
  }
  return instance
}