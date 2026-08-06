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
  private _intentionalDisconnect = false
  private _reconnecting = false
  public connected = false

  constructor(private url: string) {}

  get unauthorized() { return this._unauthorized }

  // 是否处于"已断开、正在后台重连"状态（供三态连接灯区分 绿/黄/红）
  get reconnecting() { return this._reconnecting }

  connect(token: string) {
    // 幂等：已连接且有效时，不重复断开重连（避免登录时 auth.login + AppLayout 重复调用导致竞态）
    if (this.ws && this.ws.readyState === WebSocket.OPEN && this.connected) {
      return
    }
    // 主动连接：清空所有旧状态（标记、重连计数、定时器）
    this._unauthorized = false
    this._intentionalDisconnect = false
    this._reconnecting = false
    this.reconnectAttempts = 0
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
    // 关闭可能残留的旧 socket：置空 handler 防旧 onclose 触发重连竞态
    if (this.ws) {
      const old = this.ws
      this.ws = null
      old.onclose = null
      old.onerror = null
      try { old.close() } catch { /* ignore */ }
    }
    this._open(token)
  }

  // 仅建立连接，不触碰重连状态/计数：供 connect() 与后台重连复用。
  // 后台重连（scheduleReconnect）必须走这里而非 connect()——
  // connect() 会重置 _intentionalDisconnect/reconnectAttempts，导致
  // 重连失败时 onclose 误判为主动断开而放弃重试，且指数退避失效。
  private _open(token: string) {
    const wsUrl = `${this.url}?token=${token}`
    this.ws = new WebSocket(wsUrl)

    this.ws.onopen = () => {
      this.connected = true
      this.reconnectAttempts = 0
      this._reconnecting = false
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
        // G5: 处理服务端心跳 ping，立即回 pong（在 emit 之前处理，避免给 listeners 传 ping 事件）
        if (data.type === 'ping') {
          this.send({ type: 'pong' })
          return
        }
        this.emit(data)
      } catch { /* ignore */ }
    }

    this.ws.onclose = (event) => {
      this.connected = false
      this.stopHeartbeat()
      // 先更新重连状态，再发事件：onWsDisconnected 需读到 reconnecting 才能亮黄灯
      if (event.code === 4001 || this._unauthorized || this._intentionalDisconnect) {
        // 主动断开 / token 失效：不重连，标记为"已断开"（红灯）
        this._reconnecting = false
        this.emit({ type: 'ws_disconnected' })
        return
      }
      this._reconnecting = true
      this.emit({ type: 'ws_disconnected' })
      this.scheduleReconnect()
    }

    this.ws.onerror = () => {
      // onerror 后会自动触发 onclose，重连逻辑统一在 onclose 中处理
      this.ws?.close()
    }
  }

  disconnect() {
    this.stopHeartbeat()
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
    this._intentionalDisconnect = true
    this._reconnecting = false
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

  private scheduleReconnect() {
    // 无限后台重连：不设重试次数上限，避免服务重启/长断线后连接"永久放弃"、
    // 状态灯永远卡在红色无法自愈。延迟指数退避、封顶 30s，重连即探测是否能 ping 通。
    const delay = Math.min(1000 * Math.pow(2, this.reconnectAttempts), this.maxReconnectDelay)
    this.reconnectAttempts++
    this.reconnectTimer = setTimeout(() => {
      // 从 localStorage 读取最新 token，避免使用过期闭包 token
      const freshToken = localStorage.getItem('token')
      if (freshToken) {
        // 直接 _open 而非 connect()：保持 _reconnecting=true（黄灯持续）、
        // 保留 reconnectAttempts 使指数退避连续；重连失败时 onclose 会再次调度，
        // 形成真正的"无限重连"链路。
        this._open(freshToken)
      }
      // token 不存在时不重连，用户需重新登录
    }, delay)
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