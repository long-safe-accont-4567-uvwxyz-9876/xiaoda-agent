<script setup lang="ts">
import { ref, nextTick, watch, onMounted, onBeforeUnmount, onDeactivated, onErrorCaptured, computed, inject, reactive } from 'vue'
import type { Ref } from 'vue'
import { NDrawer, NDrawerContent, NButton, NPopconfirm, useMessage } from 'naive-ui'
import { useChatStore } from '../stores/chat'
import type { ChatRequestSnapshot } from '../stores/chat'
import { useUiStore } from '../stores/ui'
import { api, exportSessionDownload } from '../api'
import { getWsClient } from '../api/ws'
import { renderMarkdown } from '../utils/markdown'
import { replaceAgentNames } from '../utils/agentNames'
import { useRenderedBand, escapeHtmlText } from '../composables/useRenderedBand'
import { useAutoScroll } from '../composables/useAutoScroll'
import { useAbortableRequests } from '../composables/useAbortableRequests'
import { isAbortError } from '../utils/abortRegistry'
import { reportError } from '../utils/errorLog'
import ToolCallCard from '../components/chat/ToolCallCard.vue'
import ChatTerminal from '../components/chat/ChatTerminal.vue'
import SlashPalette from '../components/chat/SlashPalette.vue'
import PromptInput from '../components/chat/PromptInput.vue'
import SumeruIcon from '../components/fx/SumeruIcon.vue'
import ModelSelector from '../components/chat/ModelSelector.vue'
import CmdConfirmCard from '../components/workspace/CmdConfirmCard.vue'
import JSpacePanel from '../components/jspace/JSpacePanel.vue'
import { useWorkspaceStore } from '../stores/workspace'
import { t, tf, state as i18nState } from '../i18n'

defineOptions({ name: 'ChatView' })

const chat = useChatStore()
const ui = useUiStore()
const ws = useWorkspaceStore()
const message = useMessage()
const particles = inject<Ref<any>>('particles')

const inputText = ref('')
const messagesEl = ref<HTMLElement | null>(null)
const paletteRef = ref<InstanceType<typeof SlashPalette> | null>(null)
const promptInputRef = ref<InstanceType<typeof PromptInput> | null>(null)
const commands = ref<Array<{ name: string; description: string; owner_only: boolean }>>([])
const showSessions = ref(false)
const sessions = ref<any[]>([])
const loadingSessionId = ref('')  // 正在切换加载的会话 id（用于显示加载态、防重复点击）
const playingUrl = ref('')
const lightboxUrl = ref('')
const lightboxRef = ref<HTMLElement | null>(null)
const paletteDismissed = ref(false)
const showJSpacePanel = ref(false)

watch(lightboxUrl, (url) => {
  if (url) nextTick(() => lightboxRef.value?.focus())
})
let audioEl: HTMLAudioElement | null = null

// 图片加载态：记录已加载完成（或加载失败）的图片 url，用于淡入显示
const imgSettled = reactive(new Set<string>())
function onImgSettled(url: string) { imgSettled.add(url) }
// 切换会话时清理：避免旧消息的图片 url 残留导致内存占用与状态错乱
watch(() => chat.sessionId, () => { imgSettled.clear() })

const showPalette = computed(() => !paletteDismissed.value && inputText.value.startsWith('/') && !inputText.value.includes(' '))

// 组合框 ARIA：面板可见且有候选项时对外暴露展开态与当前高亮项，供 textarea 引用
const paletteExpanded = computed(() => showPalette.value && !!paletteRef.value?.hasItems())
const paletteListboxId = computed(() => (paletteExpanded.value ? paletteRef.value?.listboxId : undefined))
const paletteActiveOption = computed(() => (paletteExpanded.value ? paletteRef.value?.activeOptionId : undefined))

watch(inputText, (value, previous) => {
  if (value !== previous && value !== '') paletteDismissed.value = false
})

function onGlobalKeydown(e: KeyboardEvent) {
  if (lightboxUrl.value && e.key === 'Escape') {
    lightboxUrl.value = ''
  }
}

// 命令确认请求 WS 处理：收到后端推送，设置 pendingCmdConfirm 触发卡片渲染
function onCmdConfirmRequest(data: any) {
  if (data?.request_id && data?.command) {
    ws.pendingCmdConfirm = {
      request_id: data.request_id,
      command: data.command,
      session_id: data.session_id || '',
    }
    nextTick(() => {
      const el = messagesEl.value
      if (el) el.scrollTop = el.scrollHeight
    })
  }
}

onMounted(async () => {
  document.addEventListener('keydown', onGlobalKeydown)
  getWsClient().on('cmd_confirm_request', onCmdConfirmRequest)
  try {
    // 后端命令名自带 "/" 前缀，统一去掉，避免拼接成 "//cmd"
    const raw = await api.getCommands()
    commands.value = raw.map(c => ({ ...c, name: c.name.replace(/^\/+/, '') }))
  } catch { /* 忽略 */ }
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onGlobalKeydown)
  getWsClient().off('cmd_confirm_request', onCmdConfirmRequest)
  if (audioEl) { audioEl.pause(); audioEl.onended = null; audioEl.onerror = null; audioEl.src = ''; audioEl = null }
  playingUrl.value = ''
})

onDeactivated(() => {
  if (audioEl) { audioEl.pause(); audioEl.onended = null; audioEl.onerror = null; audioEl.src = ''; audioEl = null }
  playingUrl.value = ''
})

// 自动跟随底部（审计 P0-2）：
// 原实现只 watch messages.length，而流式回复是"同一条消息 content 增长"，
// length 不变 —— 首帧之后视口就再也不动。这里改成同时感知条数与最后一条的
// 内容长度；是否真的滚动由 useAutoScroll 的跟随意图决定（用户上滑看历史时
// 不抢滚动，滚回底部附近自动恢复）。
const autoScroll = useAutoScroll(messagesEl)
watch(
  () => {
    const list = chat.messages
    const tail = list[list.length - 1]
    return `${list.length}:${tail?.content.length ?? 0}`
  },
  () => {
    const tail = chat.messages[chat.messages.length - 1]
    autoScroll.handleContentGrow(!!tail?.streaming)
  },
  { flush: 'post' },  // post：等 DOM 更新后再读取 scrollHeight 并滚动
)
// 用户自己发消息 → 无条件恢复跟随（否则上滑状态下发送后看不到自己的消息）
function resumeFollow() {
  autoScroll.forceFollow(true)
}

// Render markdown only near the viewport; off-band rows keep cheap escaped text.
// Streaming content is periodically remeasured, and list changes invalidate geometry.
const { rendered: renderedBand, refresh: refreshBand } = useRenderedBand(
  messagesEl,
  computed(() => chat.messages.length),
  {
    resampleMs: 250,
    resampleActive: () => chat.messages.some(m => m.streaming),
  },
)
function inBand(index: number): boolean {
  const set = renderedBand.value
  return !set || set.has(index)  // Render everything until the first measurement completes.
}
watch(() => chat.messages.length, () => refreshBand(true), { flush: 'post' })
watch(() => chat.sessionId, () => { nextTick(() => refreshBand(true)) })

// 问候到达 → 蒲公英雨
watch(() => chat.greetingPing, () => {
  particles?.value?.dandelionRain?.()
})

// 自动朗读：final 消息带 emotion 时
const finalAssistantCount = computed(() =>
  chat.messages.filter(m => m.role === 'assistant' && !m.streaming).length
)
watch(finalAssistantCount, async () => {
  if (!ui.autoSpeak) return
  const last = findLastFinalAssistant()
  if (!last || last.audioUrl) {
    if (last?.audioUrl) play(last.audioUrl)
    return
  }
  try {
    const r = await api.tts(last.content.slice(0, 300))
    play(r.audio_url)
  } catch { message.warning(t('chatView.ttsUnavailable')) }
})

/** 从尾部遍历查找最后一条已完成的助手消息，避免整体 reverse 拷贝 */
function findLastFinalAssistant() {
  for (let i = chat.messages.length - 1; i >= 0; i--) {
    const m = chat.messages[i]
    if (m.role === 'assistant' && !m.streaming && m.content) return m
  }
  return undefined
}

function play(url: string) {
  if (audioEl) { audioEl.pause(); audioEl.onended = null; audioEl.onerror = null; audioEl.src = ''; audioEl = null }
  if (playingUrl.value === url) { playingUrl.value = ''; return }
  audioEl = new Audio(url)
  playingUrl.value = url
  audioEl.onended = () => { playingUrl.value = '' }
  audioEl.onerror = () => { playingUrl.value = '' }
  audioEl.play().catch(() => { playingUrl.value = '' })
}

// 组件级在途请求登记（审计 P1-6）：卸载/失活即取消，
// 避免切页后慢请求回来继续播放音频或回写状态。
const abortable = useAbortableRequests()

async function speak(msg: { content: string; audioUrl?: string }) {
  if (msg.audioUrl) { play(msg.audioUrl); return }
  try {
    const r = await api.tts(msg.content.slice(0, 300), undefined, undefined, abortable.create())
    play(r.audio_url)
  } catch (e: any) {
    if (isAbortError(e)) return
    message.error(e?.message || t('chatView.ttsFailed'))
  }
}

/**
 * 行级 markdown 渲染兜底（审计 P0-1）：脏数据/异常语法让 renderMarkdown 抛错时，
 * 只把这一行降级成纯文本，不牵连整页。错误进全局缓冲，界面上不显示堆栈。
 */
function renderRowMarkdown(content: string): string {
  const text = replaceAgentNames(content)
  try {
    return renderMarkdown(text)
  } catch (err) {
    reportError(err, 'renderMarkdown')
    return escapeHtmlText(text)
  }
}

// 列表级错误边界（审计 P0-1）：任何一行渲染期抛出的错误都在这里被接住，
// 退化成可恢复的占位（清空消息列表即可继续用），而不是整页白屏。
const listError = ref('')
onErrorCaptured((err, _instance, info) => {
  reportError(err, info)
  listError.value = err instanceof Error ? err.message : String(err)
  return false
})
function recoverFromListError() {
  listError.value = ''
  chat.clearMessages()
}

function handleSend() {
  const request: ChatRequestSnapshot = {
    text: inputText.value.trim(),
    search: false,
    think: false,
    attachments: [],
  }
  const result = chat.sendMessage(request)
  if (!result.ok) return
  inputText.value = ''
  // 发送特效：从输入框爆叶子
  const rect = promptInputRef.value?.textareaRef?.getBoundingClientRect()
  if (rect) particles?.value?.burst?.(rect.left + rect.width / 2, rect.top, 10)
  autoGrow()
}

function handlePromptSend(request: ChatRequestSnapshot) {
  const result = chat.sendMessage(request)
  if (!result.ok) {
    if (result.reason === 'DISCONNECTED') message.warning(t('promptInput.disconnectedDraftKept'))
    return
  }
  inputText.value = ''
  resumeFollow()
  promptInputRef.value?.clearSubmittedDraft()
  // 发送特效
  const rect = promptInputRef.value?.textareaRef?.getBoundingClientRect()
  if (rect) particles?.value?.burst?.(rect.left + rect.width / 2, rect.top, 10)
}

function handleKeydown(e: KeyboardEvent) {
  if (showPalette.value && paletteRef.value?.hasItems()) {
    if (e.key === 'ArrowDown') { e.preventDefault(); paletteRef.value.move(1); return }
    if (e.key === 'ArrowUp') { e.preventDefault(); paletteRef.value.move(-1); return }
    if (e.key === 'Tab' || e.key === 'Enter') { e.preventDefault(); paletteRef.value.selectCurrent(); return }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); paletteDismissed.value = true; return }
  }
}

function selectCommand(name: string) {
  inputText.value = `/${name.replace(/^\/+/, '')} `
  nextTick(() => promptInputRef.value?.focus())
}

function autoGrow() {
  const el = promptInputRef.value?.textareaRef
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 120) + 'px'
}

async function openSessions() {
  showSessions.value = true
  try {
    sessions.value = await api.getSessions(abortable.create())
  } catch (e: any) {
    if (isAbortError(e)) return
    message.error(e.message)
  }
}

async function switchSession(sid: string) {
  if (loadingSessionId.value) return  // 防止重复点击
  loadingSessionId.value = sid
  try {
    await chat.loadSession(sid, abortable.create())
    showSessions.value = false
  } catch (e: any) {
    if (!isAbortError(e)) message.error(e.message)
  } finally { loadingSessionId.value = '' }
}

async function removeSession(sid: string) {
  try {
    await api.deleteSession(sid)
    sessions.value = sessions.value.filter(s => s.session_id !== sid)
    message.success(t('chatView.session') + ' ' + t('deleted'))
  } catch (e: any) { message.error(e.message) }
}
async function startNew() {
  if (loadingSessionId.value) return  // 加载进行中禁止新建，避免与 loadSession 竞态覆盖新会话
  await chat.newSession()
  showSessions.value = false
  message.success(t('chatView.newSessionStarted'))
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    message.success(t('chatView.copied'))
  } catch {
    message.error(t('chatView.copyFailed'))
  }
}

function resend(msg: { id: string }) {
  if (chat.isProcessing) return
  const result = chat.retryMessage(msg.id)
  if (!result.ok && result.reason === 'DISCONNECTED') {
    message.warning(t('promptInput.disconnectedDraftKept'))
  }
}

function clearAll() {
  if (loadingSessionId.value) return  // 加载进行中禁止清空，避免与 loadSession 竞态
  chat.clearMessages()
  message.success(t('chatView.cleared'))
}

function onModelChange(_provider: string, _modelId: string) {
}

function fmtTime(ts: number): string {
  return new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

// 17 类情绪标签（与 emotion_enum / emotion_simple / sticker 物理目录对齐）
const emotionColors: Record<string, string> = {
  '喜悦': '#7fd650', '兴奋': '#fb923c', '喜爱': '#ec4899',
  '害羞': '#f9a8d4', '悲伤': '#60a5fa', '愤怒': '#f87171',
  '惊讶': '#22d3ee', '困惑': '#a78bfa', '思考': '#67e8f9',
  '调皮': '#f472b6', '感动': '#fbbf24', '平静': '#9ca3af',
  '焦虑': '#fbbf24', '恐惧': '#94a3b8', '好奇': '#a78bfa',
  '撒娇': '#f9a8d4', '问候': '#34d399',
}
</script>

<template>
  <div class="chat-view">
    <div class="chat-toolbar">
      <div class="toolbar-actions">
        <n-button size="tiny" quaternary @click="openSessions">
          <template #icon><SumeruIcon name="sessions" :size="15" variant="duo" tone="view" interactive /></template>{{ t('chatView.session') }}
        </n-button>
        <n-button size="tiny" quaternary @click="startNew">
          <template #icon><SumeruIcon name="sprout" :size="15" variant="duo" tone="add" interactive /></template>{{ t('chatView.newChat') }}
        </n-button>
        <n-button size="tiny" quaternary @click="clearAll">
          <template #icon><SumeruIcon name="trash" :size="15" variant="duo" tone="del" interactive /></template>{{ t('chatView.clear') }}
        </n-button>
        <n-button v-if="chat.sessionId" size="tiny" quaternary
           @click="exportSessionDownload(chat.sessionId).catch(e => message.error(e.message))">
          <template #icon><SumeruIcon name="download" :size="15" variant="duo" tone="add" interactive /></template>{{ t('chatView.export') }}
        </n-button>
        <ModelSelector @change="onModelChange" />
      </div>
      <div class="toolbar-meta">
        <span v-if="chat.sessionId" class="session-label" :title="chat.sessionId">{{ chat.sessionId }}</span>
      </div>
    </div>

    <div class="messages-area" ref="messagesEl">
      <div v-if="listError" class="chat-row-error" role="alert">
        <div class="chat-row-error-title">⚠ {{ t('errorBoundary.title') }}</div>
        <div class="chat-row-error-detail">{{ listError }}</div>
        <button class="chat-row-error-btn" @click="recoverFromListError">{{ t('chatView.clear') }}</button>
      </div>

      <div v-if="chat.messages.length === 0" class="empty-state">
        <div class="empty-icon">🌿</div>
        <p>{{ t('chatView.emptyPlaceholder') }}</p>
      </div>

      <transition-group v-else name="msg-fade">
      <!-- v-memo（审计 P1-4）：流式 delta 只改最后一条的 content，父组件渲染
           函数却要为 1000 行重建 vnode。memo 命中时 Vue 直接复用上次的 vnode
           （patch 阶段 n1 === n2 直接返回），每帧成本从 O(行数) 降到 O(1)。
           依赖必须覆盖该行模板里读到的每一个响应式值，漏一个就会渲染陈旧。 -->
      <div v-for="(msg, index) in chat.messages" :key="msg.id" class="message-row" :class="msg.role"
           v-memo="[msg.id, msg.content, msg.role, msg.streaming, msg.emotion, msg.toolCalls, msg.toolCalls?.length, msg.imageUrl, msg.imageUrls, msg.videoUrl, msg.audioUrl, msg.stickerUrl, msg.degraded, msg.timestamp, inBand(index), playingUrl, imgSettled.size, i18nState.lang]">
        <div class="message-bubble glass-panel" :class="[msg.role, msg.streaming ? 'streaming' : '']">
          <div v-if="msg.role === 'assistant' && msg.emotion" class="emotion-dot"
               :style="{ background: emotionColors[msg.emotion] || '#9ca3af' }"
               :title="msg.emotion"></div>

          <div v-if="msg.toolCalls?.length" class="tool-calls">
            <ToolCallCard v-for="(tc, i) in msg.toolCalls" :key="i" :call="tc" />
          </div>

          <div v-if="msg.role === 'assistant' && msg.streaming" class="message-content md-body streaming-text">{{ replaceAgentNames(msg.content) }}</div>
          <div v-else-if="msg.role === 'assistant'" class="message-content md-body"
               :class="{ 'band-off': !inBand(index) }"
               v-html="inBand(index) ? renderRowMarkdown(msg.content) : escapeHtmlText(replaceAgentNames(msg.content))"></div>
          <div v-else class="message-content plain">
            {{ msg.content }}
            <img v-if="msg.imageUrl" :src="msg.imageUrl" class="user-upload-img"
                 :class="{ loaded: imgSettled.has(msg.imageUrl) }"
                 loading="lazy" :title="t('chatView.zoom')"
                 @load="onImgSettled(msg.imageUrl!)" @error="onImgSettled(msg.imageUrl!)"
                 @click="lightboxUrl = msg.imageUrl!" />
          </div>
          <span v-if="msg.streaming && !msg.content" class="cursor-blink">▌</span>

          <!-- 降级提示：轻量内联，不打断阅读也不弹窗 -->
          <div v-if="msg.role === 'assistant' && msg.degraded" class="degraded-hint">
            {{ tf('chat.degradedHint', msg.degraded.kind || '') }}
          </div>

          <!-- 生成产物区（工具产出的图/视频/语音，与表情包分离） -->
          <div v-if="msg.imageUrls?.length || msg.videoUrl || msg.audioUrl" class="artifact-block">
            <span class="artifact-label"><SumeruIcon name="palette" :size="14" variant="duo" tone="magic" interactive /> {{ t('chatView.artifacts') }}</span>
            <div v-if="msg.imageUrls?.length" class="media-grid">
              <img v-for="url in msg.imageUrls" :key="url" :src="url" class="media-image"
                   :class="{ loaded: imgSettled.has(url) }"
                   loading="lazy" :title="t('chatView.zoom')"
                   @load="onImgSettled(url)" @error="onImgSettled(url)"
                   @click="lightboxUrl = url" />
            </div>
            <video v-if="msg.videoUrl" :src="msg.videoUrl" controls class="media-video"></video>
            <audio v-if="msg.audioUrl" :src="msg.audioUrl" controls class="media-audio"></audio>
          </div>
          <!-- 表情包：贴在气泡尾部，不与产物混淆（凭据走 HttpOnly cookie，URL 不带 token） -->
          <img v-if="msg.stickerUrl" :src="msg.stickerUrl" class="sticker-img"
               :title="t('chatView.zoom')" @click="lightboxUrl = msg.stickerUrl" />

          <div class="bubble-footer" v-if="!msg.streaming && msg.content && msg.role !== 'system'">
            <span class="msg-time">{{ fmtTime(msg.timestamp) }}</span>
            <template v-if="msg.role === 'assistant'">
              <button class="footer-btn" :class="{ playing: playingUrl && playingUrl === msg.audioUrl }"
                      :title="t('chatView.readAloud')" @click="speak(msg)"><SumeruIcon name="speak" :size="14" variant="duo" interactive /></button>
              <button class="footer-btn" :title="t('chatView.copy')" @click="copyText(msg.content)"><SumeruIcon name="copy" :size="14" variant="duo" tone="edit" interactive /></button>
              <button class="footer-btn" :title="t('chatView.regenerate')" @click="chat.retryLast()"><SumeruIcon name="retry" :size="14" variant="duo" interactive /></button>
            </template>
            <template v-else>
              <button class="footer-btn" :title="t('chatView.copy')" @click="copyText(msg.content)"><SumeruIcon name="copy" :size="14" variant="duo" tone="edit" interactive /></button>
              <button class="footer-btn" :title="t('chatView.resend')" @click="resend(msg)"><SumeruIcon name="retry" :size="14" variant="duo" interactive /></button>
            </template>
            <button class="footer-btn" :title="t('chatView.withdraw')"
                    @click="chat.deleteMessage(msg.id)"><SumeruIcon name="trash" :size="14" variant="duo" tone="del" interactive /></button>
          </div>
        </div>
      </div>
      </transition-group>

      <!-- 命令确认问答卡片：Agent 执行非白名单命令时内联弹出 -->
      <transition name="msg-fade">
        <CmdConfirmCard
          v-if="ws.pendingCmdConfirm"
          :key="ws.pendingCmdConfirm.request_id"
          :request-id="ws.pendingCmdConfirm.request_id"
          :command="ws.pendingCmdConfirm.command"
          :session-id="ws.pendingCmdConfirm.session_id"
        />
      </transition>
    </div>

    <teleport to="body">
      <transition name="lightbox-fade">
        <div v-if="lightboxUrl" ref="lightboxRef" class="lightbox" @click="lightboxUrl = ''"
             tabindex="-1">
          <img :src="lightboxUrl" :alt="t('chatView.preview')" />
        </div>
      </transition>
    </teleport>

    <div class="input-area-wrapper">
      <SlashPalette ref="paletteRef" :commands="commands" :filter="inputText"
                    :visible="showPalette" @select="selectCommand" />
      <PromptInput
        ref="promptInputRef"
        v-model="inputText"
        :is-loading="chat.isProcessing"
        :connected="chat.wsConnected"
        :placeholder="t('chatView.inputPlaceholder')"
        :combobox-expanded="paletteExpanded"
        :combobox-controls="paletteListboxId"
        :combobox-active-option="paletteActiveOption"
        @send="handlePromptSend"
        @abort="chat.abort()"
        @keydown="handleKeydown"
      />
    </div>

    <n-drawer v-model:show="showSessions" width="min(340px, 92vw)" placement="left">
      <n-drawer-content :title="'📂 ' + t('chatView.history')" closable>
        <div class="session-list" :class="{ 'is-loading': !!loadingSessionId }">
          <div v-for="s in sessions" :key="s.session_id" class="session-item"
               :class="{ active: s.session_id === chat.sessionId, loading: loadingSessionId === s.session_id }"
               :aria-busy="loadingSessionId === s.session_id"
               @click="switchSession(s.session_id)">
            <div class="session-title">
              <span class="session-source" :class="s.source">{{
                s.source === 'qq' ? 'QQ' : s.source === 'cli' ? 'CLI' : s.source === 'wechat' ? '微信' : 'Web' }}</span>
              {{ replaceAgentNames(s.title || s.session_id) }}
            </div>
            <div class="session-meta">
              <span>{{ s.message_count }} {{ t('chatView.messages') }} · {{ new Date(s.updated_at * 1000).toLocaleString('zh-CN') }}</span>
              <n-popconfirm @positive-click.stop="removeSession(s.session_id)">
                <template #trigger>
                  <button class="footer-btn" @click.stop><SumeruIcon name="trash" :size="13" variant="duo" tone="del" interactive /></button>
                </template>
                {{ t('chatView.deleteConfirm') }}
              </n-popconfirm>
            </div>
            <div class="session-preview">{{ replaceAgentNames(s.last_message) }}</div>
          </div>
          <div v-if="!sessions.length" class="empty-state small">
            <p>{{ t('chatView.noHistory') }}</p>
          </div>
        </div>
      </n-drawer-content>
    </n-drawer>

    <!-- 小妲终端（右侧浮动面板，Teleport to body） -->
    <ChatTerminal />

    <!-- J-Space 浮窗切换按钮 -->
    <button class="jspace-fab" :class="{ active: showJSpacePanel }"
      :title="t('jspace.togglePanel')" @click="showJSpacePanel = !showJSpacePanel">
      ⬡
    </button>

    <!-- J-Space 浮窗抽屉 -->
    <n-drawer v-model:show="showJSpacePanel" width="min(380px, 92vw)" placement="right">
      <n-drawer-content :title="t('jspace.floatingPanel')" closable>
        <JSpacePanel :compact="true" />
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped src="./chat-view.css"></style>
