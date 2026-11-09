export interface WsEvent {
  type: string
  [key: string]: unknown
}

export type LocalAiWsEvent = WsEvent & {
  type: 'local_ai_device_updated' | 'local_ai_download_updated' | 'local_ai_instance_updated'
}

/**
 * 心跳时序（审计 P1-7）。
 *
 * 服务端每 30s 下发 {"type":"ping"}，10s 内收不到 pong 就关连接（web/ws_hub.py）。
 * 客户端这边原来只"发"心跳、从不校验回应：连接处于半开状态（NAT 超时、链路
 * 静默丢包、服务端进程卡死但 socket 未 FIN）时，socket.readyState 仍是 OPEN，
 * 状态灯一直绿着，用户发的消息全部石沉大海，直到手动刷新页面。
 *
 * 现在的判定：每次下行链路都算"活着"（任何帧都能证明半开已结束），
 * 主动 ping 之后 60s 内没有任何下行帧 → 判定连接已死 → 主动重建。
 * 60s = 2.4 个 ping 周期：容忍单次丢包与浏览器后台节流，又能在 1 分钟内
 * 把假绿揪出来。重连复用既有指数退避（1s→2s→…→30s 封顶），不会造成重连风暴。
 */
export const HEARTBEAT_PING_INTERVAL_MS = 25_000
export const HEARTBEAT_ACK_TIMEOUT_MS = 60_000

export class WsClient {
  private ws: WebSocket | null = null
  private reconnectAttempts = 0
  private maxReconnectDelay = 30000
  private listeners: Map<string, Set<(data: WsEvent) => void>> = new Map()
  private heartbeatInterval: ReturnType<typeof setInterval> | null = null
  private heartbeatWatchdog: ReturnType<typeof setInterval> | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private _unauthorized = false
  private _intentionalDisconnect = false
  private _reconnecting = false
  /** 首个"发出后尚未收到任何下行帧"的 ping 时间；0 = 无未决 ping */
  private pingPendingSince = 0
  /** 已针对当前 socket 判定过心跳超时（防重复重建） */
  private heartbeatDead = false
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
    this.heartbeatDead = false
    this.pingPendingSince = 0
    this.reconnectAttempts = 0
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
    // 关闭可能残留的旧 socket：置空 handler 防旧 onclose 触发重连竞态
    if (this.ws) {
      const old = this.ws
      this.ws = null
      old.onclose = null
      old.onerror = null
      try { old.close() } catch { /* 静默兜底：关闭旧连接失败不影响新连接 */ }
    }
    this._open(token)
  }

  reconnect(token: string) {
    this.disconnect()
    this.connect(token)
  }

  retry(): boolean {
    const token = localStorage.getItem('token')
    if (!token) return false
    this.connect(token)
    return true
  }

  // 仅建立连接，不触碰重连状态/计数：供 connect() 与后台重连复用。
  // 后台重连（scheduleReconnect）必须走这里而非 connect()——
  // connect() 会重置 _intentionalDisconnect/reconnectAttempts，导致
  // 重连失败时 onclose 误判为主动断开而放弃重试，且指数退避失效。
  private _open(token: string) {
    const socket = token ? new WebSocket(this.url, [token]) : new WebSocket(this.url)
    this.ws = socket
    // 关键竞态防护（audit-fix-20260829）：所有处理器按 socket 实例身份守卫。
    // 旧 socket 被替换/关闭后，其迟到的 close/error/open/message 一律不得
    // 影响新连接——否则 reconnect() 后旧 onclose 会把新连接标为断开并再开
    // 第三条、旧 onerror 甚至会直接关掉新 socket。
    const stale = () => this.ws !== socket

    socket.onopen = () => {
      if (stale()) return
      this.connected = true
      this.reconnectAttempts = 0
      this._reconnecting = false
      this.startHeartbeat()
      this.emit({ type: 'ws_connected' })
    }

    socket.onmessage = (event) => {
      if (stale()) return
      // 任何下行帧都证明链路活着：清掉未决 ping（P1-7）
      this.pingPendingSince = 0
      try {
        const data = JSON.parse(event.data) as WsEvent
        // Token 失效：服务端发送 UNAUTHORIZED 错误后关闭连接
        // 立即停止重连，避免循环弹错
        if (data.type === 'error' && data.code === 'UNAUTHORIZED') {
          this._unauthorized = true
          this.disconnect()
          this._handleUnauthorized()
          return
        }
        // G5: 处理服务端心跳 ping，立即回 pong（在 emit 之前处理，避免给 listeners 传 ping 事件）
        if (data.type === 'ping') {
          this.send({ type: 'pong' })
          return
        }
        this.emit(data)
      } catch { /* 静默兜底：收到非法帧就丢弃，等待后续合法帧 */ }
    }

    socket.onclose = (event) => {
      if (stale()) return
      this.connected = false
      this.stopHeartbeat()
      // 先更新重连状态，再发事件：onWsDisconnected 需读到 reconnecting 才能亮黄灯
      if (event.code === 4001) {
        // token 失效（服务端直接以 4001 关闭）：停止重连、清 token 跳登录
        this._reconnecting = false
        this._handleUnauthorized()
        this.emit({ type: 'ws_disconnected' })
        return
      }
      if (this._unauthorized || this._intentionalDisconnect) {
        // 主动断开 / token 失效：不重连，标记为"已断开"（红灯）
        this._reconnecting = false
        this.emit({ type: 'ws_disconnected' })
        return
      }
      this._reconnecting = true
      this.emit({ type: 'ws_disconnected' })
      this.scheduleReconnect()
    }

    socket.onerror = () => {
      if (stale()) return
      // onerror 后会自动触发 onclose，重连逻辑统一在 onclose 中处理
      socket.close()
    }
  }

  disconnect() {
    this.stopHeartbeat()
    this.heartbeatDead = false
    if (this.reconnectTimer) { clearTimeout(this.reconnectTimer); this.reconnectTimer = null }
    this._intentionalDisconnect = true
    this._reconnecting = false
    const hadSocket = !!this.ws
    this.ws?.close()
    this.ws = null
    this.connected = false
    // 曾连接过：补发 ws_disconnected。原先是旧的 onclose 异步补发，
    // 身份守卫后旧 onclose 不再执行（其守卫使迟到事件失效），这里同步补发
    // 保持既有语义：状态灯熄灭 + 前端清理 isProcessing。
    if (hadSocket) this.emit({ type: 'ws_disconnected' })
  }

  private _handleUnauthorized() {
    // 清除本地 token 并跳转登录页
    localStorage.removeItem('token')
    localStorage.removeItem('expires_at')
    if (!location.hash.includes('/login')) {
      location.hash = '#/login'
    }
  }

  send(data: Record<string, unknown>): boolean {
    if (this.ws?.readyState === WebSocket.OPEN) {
      try {
        this.ws.send(JSON.stringify(data))
        return true
      } catch {
        return false
      }
    }
    return false
  }

  on<T extends WsEvent = WsEvent>(type: string, handler: (data: T) => void) {
    if (!this.listeners.has(type)) {
      this.listeners.set(type, new Set())
    }
    this.listeners.get(type)!.add(handler as (data: WsEvent) => void)
  }

  off<T extends WsEvent = WsEvent>(type: string, handler: (data: T) => void) {
    this.listeners.get(type)?.delete(handler as (data: WsEvent) => void)
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
    this.heartbeatDead = false
    this.pingPendingSince = 0
    this.heartbeatInterval = setInterval(() => this.sendHeartbeatPing(), HEARTBEAT_PING_INTERVAL_MS)
    // 看门狗比 ping 密集：判定延迟上限取决于它，而不是 ping 周期
    this.heartbeatWatchdog = setInterval(() => this.checkHeartbeat(), 10_000)
    document.addEventListener('visibilitychange', this.onHeartbeatVisibility)
  }

  private stopHeartbeat() {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval)
      this.heartbeatInterval = null
    }
    if (this.heartbeatWatchdog) {
      clearInterval(this.heartbeatWatchdog)
      this.heartbeatWatchdog = null
    }
    document.removeEventListener('visibilitychange', this.onHeartbeatVisibility)
    this.pingPendingSince = 0
  }

  /** 页面回到前台：后台节流期间的时间不算超时，并立刻补一次探活 */
  private onHeartbeatVisibility = () => {
    if (document.hidden) return
    this.pingPendingSince = 0
    this.sendHeartbeatPing()
  }

  private sendHeartbeatPing() {
    // 后台标签页的定时器会被浏览器节流，此时的"沉默"不代表链路已死，
    // 记下时间点即可，等回到前台由 onHeartbeatVisibility 重置
    if (document.hidden) return
    if (this.pingPendingSince === 0) this.pingPendingSince = Date.now()
    this.send({ type: 'ping' })
  }

  private checkHeartbeat() {
    if (!this.connected || document.hidden) return
    if (this.pingPendingSince === 0) return
    if (Date.now() - this.pingPendingSince < HEARTBEAT_ACK_TIMEOUT_MS) return
    const socket = this.ws
    if (!socket) return
    this.handleHeartbeatTimeout(socket)
  }

  /**
   * 半开连接：socket 仍报 OPEN，但下行已死。
   * 不能只 close() 等 onclose —— 半开场景下 onclose 可能永远不来，
   * 所以这里主动走一遍断开语义并复用既有退避重连。
   */
  private handleHeartbeatTimeout(socket: WebSocket) {
    if (this.ws !== socket || this.heartbeatDead) return
    this.heartbeatDead = true
    this.stopHeartbeat()
    this.connected = false
    this._reconnecting = true
    this.ws = null
    // 摘掉处理器：旧 socket 迟到的任何事件都不得影响新连接
    socket.onopen = null
    socket.onmessage = null
    socket.onclose = null
    socket.onerror = null
    try { socket.close() } catch { /* 静默兜底：连接可能已断开，关闭异常可忽略 */ }
    // 先更新重连态再发事件（与 onclose 路径同序，状态灯才会转黄）
    this.emit({ type: 'ws_disconnected' })
    this.scheduleReconnect()
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
      } else {
        // 无 token（已登出/未登录）：无法再发起重连，复位重连态并广播断线，
        // 避免 _reconnecting 永久为 true 导致状态灯卡黄
        this._reconnecting = false
        this.emit({ type: 'ws_disconnected' })
      }
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
