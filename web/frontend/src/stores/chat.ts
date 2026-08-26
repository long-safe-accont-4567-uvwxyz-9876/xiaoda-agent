import { defineStore } from 'pinia'
import { ref, type Ref } from 'vue'
import { getWsClient } from '../api/ws'
import type { WsEvent } from '../api/ws'
import { api } from '../api'
import { useAgentsStore } from './agents'
import { t, tf } from '../i18n'
import { clearMarkdownCache } from '../utils/markdown'

export interface ToolCall {
  id: string
  tool: string
  argsPreview: string
  ok: boolean | null
  elapsedMs: number | null
  running: boolean
  turn: number
  index: number
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  emotion?: string
  stickerUrl?: string
  audioUrl?: string
  audioPending?: boolean
  imageUrls?: string[]
  videoUrl?: string
  toolCalls?: ToolCall[]
  streaming?: boolean
  agent?: string
  timestamp: number
  imageUrl?: string  // 用户上传的图片 URL（用于气泡内显示预览）
  request?: ChatRequestSnapshot
}

export interface ChatAttachmentSnapshot {
  kind: 'image' | 'document'
  url: string
  name: string
  path?: string
  ext?: string
}

export interface ChatRequestSnapshot {
  text: string
  search: boolean
  think: boolean
  attachments: ChatAttachmentSnapshot[]
}

export type ChatSendResult =
  | { ok: true; msgId: string }
  | { ok: false; reason: 'EMPTY_REQUEST' | 'PROCESSING' | 'DISCONNECTED' }

const MAX_MESSAGES = 1000

function pushMessage(messages: Ref<Message[]>, msg: Message) {
  messages.value.push(msg)
  if (messages.value.length > MAX_MESSAGES) {
    messages.value = messages.value.slice(-MAX_MESSAGES)
  }
}

export const useChatStore = defineStore('chat', () => {
  const messages = ref<Message[]>([])
  let loadSessionGeneration = 0
  const currentAgent = ref('xiaoda')
  const sessionId = ref('')
  const isProcessing = ref(false)
  const currentStage = ref('')
  const statusText = ref('')
  const ws = getWsClient()
  const wsConnected = ref(false)
  const wsReconnecting = ref(ws.reconnecting)
  const lastEmotion = ref('平静')
  const pendingMsgId = ref('')
  const greetingPing = ref(0)  // 问候到达脉冲（GrassParticles 蒲公英雨）
  const streamStates = new Map<string, { lastSeq: number; terminal: boolean }>()
  // 终态流记录上限：只置 terminal flag 从不删除会让长会话缓慢增长，
  // 超限淘汰最旧记录（与后端 _MAX_STREAM_SESSIONS=256 同策略）
  const STREAM_STATES_MAX = 256()

  const pendingTimers: ReturnType<typeof setTimeout>[] = []

  // 初始化时主动同步 WS 状态（避免竞态：WS 在 chat store 初始化前已连接，ws_connected 事件被错过）
  if (ws.connected) {
    wsConnected.value = true
  }

  const onConnected = (e: WsEvent) => {
    wsConnected.value = true
    wsReconnecting.value = false
    // 重连后恢复会话与 agent（不丢状态）
    if (sessionId.value) {
      ws.send({ type: 'set_session', session_id: sessionId.value })
    } else {
      sessionId.value = e.session_id as string
    }
    if (currentAgent.value !== 'xiaoda') {
      ws.send({ type: 'set_agent', agent: currentAgent.value })
    }
  }
  const onWsConnected = () => { wsConnected.value = true; wsReconnecting.value = false }
  const onWsDisconnected = () => {
    wsConnected.value = false
    wsReconnecting.value = ws.reconnecting
    if (isProcessing.value) {
      isProcessing.value = false
      currentStage.value = ''
      statusText.value = ''
      pendingMsgId.value = ''
    }
  }

  const onStatus = (e: WsEvent) => {
    currentStage.value = e.stage as string
    statusText.value = (e.text as string) || ''
  }

  function onStreamEvent(e: WsEvent) {
    if (e.version !== 1) return
    const msgId = e.msg_id as string
    const seq = e.seq as number
    if (!msgId || !Number.isInteger(seq)) return
    const state = streamStates.get(msgId) || { lastSeq: 0, terminal: false }
    if (state.terminal || seq <= state.lastSeq) return
    state.lastSeq = seq
    state.terminal = e.terminal === true
    streamStates.delete(msgId)
    streamStates.set(msgId, state)
    if (streamStates.size > STREAM_STATES_MAX) {
      const oldest = streamStates.keys().next().value
      if (oldest !== undefined) streamStates.delete(oldest)
    }
    const event = e.event as string
    if (event === 'text_delta') {
      onStreamText({ ...e, type: 'stream_text' })
    } else if (event === 'tool_status') {
      onToolEvent({ ...e, type: 'tool_event' })
    } else if (event === 'final') {
      onFinal({ ...e, type: 'final' })
    } else if (event === 'error' || event === 'abort') {
      onError({ ...e, type: 'error', code: event === 'abort' ? 'ABORTED' : e.code })
    }
  }

  // P0: 流式文本推送 —— 逐 token 拼接，实时渲染（在消息列表中显示"正在输入"的临时消息）
  const onStreamText = (e: WsEvent) => {
    const msgId = e.msg_id as string
    if (!msgId) return
    let msg = messages.value.find(m => m.id === `a-${msgId}`)
    if (!msg) {
      msg = {
        id: `a-${msgId}`, role: 'assistant', content: '',
        streaming: true, timestamp: Date.now(),
      }
      pushMessage(messages, msg)
    }
    const delta = (e.delta as string) || ''
    const accumulated = (e.accumulated as string) || ''
    msg.content = delta ? msg.content + delta : accumulated
    msg.streaming = true
  }

  // P0: 工具调用中间状态 —— 显示"正在调用 web_search..."
  const onToolStatus = (e: WsEvent) => {
    currentStage.value = 'tool'
    statusText.value = (e.label as string) || ''
  }

  const onToolEvent = (e: WsEvent) => {
    const msgId = (e.msg_id as string) || pendingMsgId.value
    if (!msgId) return
    let msg = messages.value.find(m => m.id === `a-${msgId}`)
    if (!msg) {
      msg = {
        id: `a-${msgId}`, role: 'assistant', content: '',
        streaming: true, toolCalls: [], timestamp: Date.now(),
      }
      pushMessage(messages, msg)
    }
    if (!msg.toolCalls) msg.toolCalls = []
    const id = (e.tool_call_id as string) || `${e.turn || 0}:${e.index || 0}:${e.tool || ''}`
    const existing = msg.toolCalls.find(call => call.id === id)
    if (e.stage === 'started' || e.phase === 'start') {
      if (existing) {
        existing.running = true
        existing.argsPreview = (e.args_preview as string) || existing.argsPreview
      } else {
        msg.toolCalls.push({
          id,
          tool: e.tool as string,
          argsPreview: (e.args_preview as string) || '',
          ok: null, elapsedMs: null, running: true,
          turn: (e.turn as number) || 0,
          index: (e.index as number) || 0,
        })
      }
    } else if (existing) {
      existing.running = false
      existing.ok = e.ok == null ? e.stage === 'completed' : e.ok as boolean
      existing.elapsedMs = (e.elapsed_ms as number) ?? null
    }
  }

  const onFinal = (e: WsEvent) => {
    const msgId = e.msg_id as string
    let msg = messages.value.find(m => m.id === `a-${msgId}`)
    if (!msg) {
      msg = { id: `a-${msgId}`, role: 'assistant', content: '', timestamp: Date.now() }
      pushMessage(messages, msg)
    }
    msg.content = e.reply as string
    msg.emotion = (e.emotion as string) || undefined
    msg.stickerUrl = (e.sticker_url as string) || undefined
    msg.audioUrl = (e.audio_url as string) || undefined
    msg.audioPending = (e.audio_pending as boolean) || false
    msg.imageUrls = (e.image_urls as string[]) || []
    msg.videoUrl = (e.video_url as string) || undefined
    msg.agent = e.agent as string
    msg.streaming = false
    if (msg.emotion) lastEmotion.value = msg.emotion
    isProcessing.value = false
    currentStage.value = ''
    statusText.value = ''
    pendingMsgId.value = ''
  }

  // Task 6: 异步 TTS 合成完成 —— 更新对应消息的 audioUrl
  const onAudioReady = (e: WsEvent) => {
    const msgId = e.msg_id as string
    const msg = messages.value.find(m => m.id === `a-${msgId}`)
    if (msg) {
      msg.audioUrl = (e.audio_url as string) || undefined
      msg.audioPending = false
    }
  }

  const onError = (e: WsEvent) => {
    isProcessing.value = false
    currentStage.value = ''
    pendingMsgId.value = ''
    pushMessage(messages, {
      id: `err-${Date.now()}`,
      role: 'system',
      content: e.code === 'ABORTED' ? t('chat.aborted') : t('chat.errorOccurred') + e.message,
      timestamp: Date.now(),
    })
  }

  const onAgentChanged = (e: WsEvent) => {
    currentAgent.value = e.agent as string
  }

  const onGreeting = (e: WsEvent) => {
    pushMessage(messages, {
      id: `greet-${Date.now()}`,
      role: 'assistant',
      content: e.text as string,
      emotion: '喜悦',
      stickerUrl: (e.sticker_url as string) || undefined,
      audioUrl: (e.audio_url as string) || undefined,
      timestamp: Date.now(),
    })
    lastEmotion.value = '喜悦'
    greetingPing.value++
  }

  // 注册所有 WS 事件处理器
  const wsHandlers: [string, (e: WsEvent) => void][] = [
    ['connected', onConnected],
    ['ws_connected', onWsConnected],
    ['ws_disconnected', onWsDisconnected],
    ['status', onStatus],
    ['stream_text', onStreamText],
    ['tool_status', onToolStatus],
    ['tool_event', onToolEvent],
    ['final', onFinal],
    ['error', onError],
    ['stream_event', onStreamEvent],
    ['audio_ready', onAudioReady],
    ['agent_changed', onAgentChanged],
    ['greeting', onGreeting],
  ]
  wsHandlers.forEach(([type, handler]) => ws.on(type, handler))

  function cleanup() {
    wsHandlers.forEach(([type, handler]) => ws.off(type, handler))
    pendingTimers.forEach(id => clearTimeout(id))
    pendingTimers.length = 0
  }

  function sendMessage(request: ChatRequestSnapshot): ChatSendResult {
    if (!request.text.trim() && request.attachments.length === 0) {
      return { ok: false, reason: 'EMPTY_REQUEST' }
    }
    if (isProcessing.value) return { ok: false, reason: 'PROCESSING' }
    const msgId = `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
    const image = request.attachments.find(attachment => attachment.kind === 'image')
    const document = request.attachments.find(attachment => attachment.kind === 'document')
    const displayText = request.text.trim() || (image ? '📷 图片' : `📄 ${document?.name || ''}`)
    const payload: Record<string, unknown> = {
      type: 'chat',
      session_id: sessionId.value,
      agent: currentAgent.value,
      text: request.text,
      msg_id: msgId,
    }
    if (request.search) payload.search_mode = true
    if (request.think) payload.think_mode = true
    if (image) {
      payload.image_url = image.url
      payload.image_name = image.name
    }
    if (document?.path) {
      payload.doc_path = document.path
      payload.doc_name = document.name
    }
    if (!wsConnected.value || !ws.send(payload)) {
      return { ok: false, reason: 'DISCONNECTED' }
    }
    pushMessage(messages, {
      id: `u-${msgId}`, role: 'user', content: displayText, timestamp: Date.now(),
      imageUrl: image?.url,
      request: {
        ...request,
        attachments: request.attachments.map(attachment => ({ ...attachment })),
      },
    })
    isProcessing.value = true
    pendingMsgId.value = msgId
    return { ok: true, msgId }
  }

  function retryMessage(messageId: string): ChatSendResult {
    const message = messages.value.find(item => item.id === messageId)
    if (!message?.request) return { ok: false, reason: 'EMPTY_REQUEST' }
    return sendMessage(structuredClone(message.request))
  }

  function abort() {
    if (pendingMsgId.value) {
      ws.send({ type: 'abort', msg_id: pendingMsgId.value })
    }
  }

  function setAgent(agent: string) {
    if (agent === currentAgent.value) return
    currentAgent.value = agent
    ws.send({ type: 'set_agent', agent })
    const display = useAgentsStore().agents
      .find(a => a.name === agent)?.display_name || agent
    const id = `sys-${Date.now()}`
    pushMessage(messages, {
      id, role: 'system',
      content: tf('chat.agentTakeover', display),
      timestamp: Date.now(),
    })
    // 切换提示 3 秒后自动消失，不挡聊天
    const timerId = setTimeout(() => deleteMessage(id), 3000)
    pendingTimers.push(timerId)
  }

  async function newSession() {
    loadSessionGeneration++
    const data = await api.createSession()
    sessionId.value = data.session_id
    ws.send({ type: 'set_session', session_id: data.session_id })
    messages.value = []
    clearMarkdownCache()
  }

  /** 撤回/删除一条消息（仅从当前界面移除） */
  function deleteMessage(id: string) {
    const i = messages.value.findIndex(m => m.id === id)
    if (i >= 0) messages.value.splice(i, 1)
  }

  /** 重试：重发最后一条用户消息 */
  function retryLast(): ChatSendResult {
    if (isProcessing.value) return { ok: false, reason: 'PROCESSING' }
    // 反向查找最后一条用户消息的原始下标（避免 [...arr].reverse() 拷贝）
    let idx = -1
    for (let i = messages.value.length - 1; i >= 0; i--) {
      if (messages.value[i].role === 'user') { idx = i; break }
    }
    if (idx < 0) return { ok: false, reason: 'EMPTY_REQUEST' }
    const msg = messages.value[idx]
    return retryMessage(msg.id)
  }

  function clearMessages() {
    messages.value = []
    clearMarkdownCache()
  }

  async function loadSession(sid: string) {
    const generation = ++loadSessionGeneration
    sessionId.value = sid
    ws.send({ type: 'set_session', session_id: sid })
    const history = await api.getMessages(sid)
    if (generation !== loadSessionGeneration || sessionId.value !== sid) return
    clearMarkdownCache()
    messages.value = history.map(h => ({
      id: `h-${h.id}`,
      role: h.role as Message['role'],
      content: h.content,
      emotion: h.emotion || undefined,
      timestamp: h.timestamp * 1000,
      request: (h.request_context as Message['request']) || undefined,
    }))
  }

  return {
    messages, currentAgent, sessionId, isProcessing, currentStage, statusText,
    wsConnected, wsReconnecting, lastEmotion, greetingPing,
    sendMessage, retryMessage, abort, setAgent, newSession, loadSession,
    deleteMessage, retryLast, clearMessages, cleanup,
  }
})
