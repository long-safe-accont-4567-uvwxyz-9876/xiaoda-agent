import { defineStore } from 'pinia'
import { ref } from 'vue'
import { getWsClient } from '../api/ws'
import type { WsEvent } from '../api/ws'
import { api } from '../api'
import { useAgentsStore } from './agents'
import { t, tf } from '../i18n'
import { clearMarkdownCache } from '../utils/markdown'

export interface ToolCall {
  tool: string
  argsPreview: string
  ok: boolean | null
  elapsedMs: number | null
  running: boolean
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
}

export const useChatStore = defineStore('chat', () => {
  const messages = ref<Message[]>([])
  const currentAgent = ref('xiaoda')
  const sessionId = ref('')
  const isProcessing = ref(false)
  const currentStage = ref('')
  const statusText = ref('')
  const wsConnected = ref(false)
  const lastEmotion = ref('平静')
  const pendingMsgId = ref('')
  const greetingPing = ref(0)  // 问候到达脉冲（GrassParticles 蒲公英雨）

  const ws = getWsClient()

  // 初始化时主动同步 WS 状态（避免竞态：WS 在 chat store 初始化前已连接，ws_connected 事件被错过）
  if (ws.connected) {
    wsConnected.value = true
  }

  ws.on('connected', (e: WsEvent) => {
    wsConnected.value = true
    // 重连后恢复会话与 agent（不丢状态）
    if (sessionId.value) {
      ws.send({ type: 'set_session', session_id: sessionId.value })
    } else {
      sessionId.value = e.session_id as string
    }
    if (currentAgent.value !== 'xiaoda') {
      ws.send({ type: 'set_agent', agent: currentAgent.value })
    }
  })
  ws.on('ws_connected', () => { wsConnected.value = true })
  ws.on('ws_disconnected', () => { wsConnected.value = false })

  ws.on('status', (e: WsEvent) => {
    currentStage.value = e.stage as string
    statusText.value = (e.text as string) || ''
  })

  // P0: 流式文本推送 —— 逐 token 拼接，实时渲染（在消息列表中显示"正在输入"的临时消息）
  ws.on('stream_text', (e: WsEvent) => {
    const msgId = e.msg_id as string
    if (!msgId) return
    let msg = messages.value.find(m => m.id === `a-${msgId}`)
    if (!msg) {
      msg = {
        id: `a-${msgId}`, role: 'assistant', content: '',
        streaming: true, timestamp: Date.now(),
      }
      messages.value.push(msg)
    }
    msg.content = (e.accumulated as string) || ''
    msg.streaming = true
  })

  // P0: 工具调用中间状态 —— 显示"正在调用 web_search..."
  ws.on('tool_status', (e: WsEvent) => {
    currentStage.value = 'tool'
    statusText.value = (e.label as string) || ''
  })

  ws.on('tool_event', (e: WsEvent) => {
    const msgId = (e.msg_id as string) || pendingMsgId.value
    if (!msgId) return
    let msg = messages.value.find(m => m.id === `a-${msgId}`)
    if (!msg) {
      msg = {
        id: `a-${msgId}`, role: 'assistant', content: '',
        streaming: true, toolCalls: [], timestamp: Date.now(),
      }
      messages.value.push(msg)
    }
    if (!msg.toolCalls) msg.toolCalls = []
    if (e.phase === 'start') {
      msg.toolCalls.push({
        tool: e.tool as string,
        argsPreview: (e.args_preview as string) || '',
        ok: null, elapsedMs: null, running: true,
      })
    } else {
      const tc = [...msg.toolCalls].reverse().find(t => t.tool === e.tool && t.running)
      if (tc) {
        tc.running = false
        tc.ok = e.ok as boolean
        tc.elapsedMs = e.elapsed_ms as number
      }
    }
  })

  ws.on('final', (e: WsEvent) => {
    const msgId = e.msg_id as string
    let msg = messages.value.find(m => m.id === `a-${msgId}`)
    if (!msg) {
      msg = { id: `a-${msgId}`, role: 'assistant', content: '', timestamp: Date.now() }
      messages.value.push(msg)
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
  })

  // Task 6: 异步 TTS 合成完成 —— 更新对应消息的 audioUrl
  ws.on('audio_ready', (e: WsEvent) => {
    const msgId = e.msg_id as string
    const msg = messages.value.find(m => m.id === `a-${msgId}`)
    if (msg) {
      msg.audioUrl = (e.audio_url as string) || undefined
      msg.audioPending = false
    }
  })

  ws.on('error', (e: WsEvent) => {
    isProcessing.value = false
    currentStage.value = ''
    pendingMsgId.value = ''
    messages.value.push({
      id: `err-${Date.now()}`,
      role: 'system',
      content: e.code === 'ABORTED' ? t('chat.aborted') : t('chat.errorOccurred') + e.message,
      timestamp: Date.now(),
    })
  })

  ws.on('agent_changed', (e: WsEvent) => {
    currentAgent.value = e.agent as string
  })

  ws.on('greeting', (e: WsEvent) => {
    messages.value.push({
      id: `greet-${Date.now()}`,
      role: 'assistant',
      content: e.text as string,
      emotion: '喜悦',
      audioUrl: (e.audio_url as string) || undefined,
      timestamp: Date.now(),
    })
    lastEmotion.value = '喜悦'
    greetingPing.value++
  })

  function sendMessage(text: string, imageUrl?: string) {
    if (!text.trim() || isProcessing.value) return
    const msgId = `${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
    // 用户消息文本中去掉 [Image: ...] 标记，图片通过 imageUrl 字段单独展示
    const displayText = text.replace(/\n?\[Image: [^\]]+\]\s*/g, '').trim() || '📷 图片'
    messages.value.push({
      id: `u-${msgId}`, role: 'user', content: displayText, timestamp: Date.now(),
      imageUrl,
    })
    isProcessing.value = true
    pendingMsgId.value = msgId
    ws.send({
      type: 'chat',
      session_id: sessionId.value,
      agent: currentAgent.value,
      text,
      msg_id: msgId,
    })
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
    messages.value.push({
      id, role: 'system',
      content: tf('chat.agentTakeover', display),
      timestamp: Date.now(),
    })
    // 切换提示 3 秒后自动消失，不挡聊天
    setTimeout(() => deleteMessage(id), 3000)
  }

  async function newSession() {
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

  /** 重试：移除最后一条助手回复，重发最后一条用户消息 */
  function retryLast() {
    if (isProcessing.value) return
    const lastUserIdx = [...messages.value].reverse().findIndex(m => m.role === 'user')
    if (lastUserIdx < 0) return
    const idx = messages.value.length - 1 - lastUserIdx
    const text = messages.value[idx].content
    // 移除该条用户消息之后的所有消息（旧回复/错误），重新发送
    messages.value.splice(idx)
    sendMessage(text)
  }

  function clearMessages() {
    messages.value = []
    clearMarkdownCache()
  }

  async function loadSession(sid: string) {
    sessionId.value = sid
    ws.send({ type: 'set_session', session_id: sid })
    const history = await api.getMessages(sid)
    messages.value = history.map(h => ({
      id: `h-${h.id}`,
      role: h.role as Message['role'],
      content: h.content,
      emotion: h.emotion || undefined,
      timestamp: h.timestamp * 1000,
    }))
  }

  return {
    messages, currentAgent, sessionId, isProcessing, currentStage, statusText,
    wsConnected, lastEmotion, greetingPing,
    sendMessage, abort, setAgent, newSession, loadSession,
    deleteMessage, retryLast, clearMessages,
  }
})