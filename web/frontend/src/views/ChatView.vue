<script setup lang="ts">
import { ref, nextTick, watch, onMounted, onBeforeUnmount, onDeactivated, computed, inject, reactive } from 'vue'
import type { Ref } from 'vue'
import { NDrawer, NDrawerContent, NButton, NPopconfirm, useMessage } from 'naive-ui'
import { useChatStore } from '../stores/chat'
import type { ChatRequestSnapshot } from '../stores/chat'
import { useAuthStore } from '../stores/auth'
import { useUiStore } from '../stores/ui'
import { api, exportSessionDownload } from '../api'
import { getWsClient } from '../api/ws'
import { renderMarkdown } from '../utils/markdown'
import { replaceAgentNames } from '../utils/agentNames'
import ToolCallCard from '../components/chat/ToolCallCard.vue'
import ChatTerminal from '../components/chat/ChatTerminal.vue'
import SlashPalette from '../components/chat/SlashPalette.vue'
import PromptInput from '../components/chat/PromptInput.vue'
import SumeruIcon from '../components/fx/SumeruIcon.vue'
import ModelSelector from '../components/chat/ModelSelector.vue'
import CmdConfirmCard from '../components/workspace/CmdConfirmCard.vue'
import JSpacePanel from '../components/jspace/JSpacePanel.vue'
import { useWorkspaceStore } from '../stores/workspace'
import { t } from '../i18n'

defineOptions({ name: 'ChatView' })

const chat = useChatStore()
const auth = useAuthStore()
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

watch(() => chat.messages.length, () => {
  const el = messagesEl.value
  if (!el) return
  // 仅在用户位于底部附近时自动滚动，避免打断上翻阅读
  const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
  if (distanceFromBottom > 100) return
  // 流式期间用 auto（即时跟随），非流式时用 smooth
  const isStreaming = chat.messages.some(m => m.streaming)
  el.scrollTo({ top: el.scrollHeight, behavior: isStreaming ? 'auto' : 'smooth' })
}, { flush: 'post' })  // post：等 DOM 更新后再读取 scrollHeight 并滚动

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

async function speak(msg: { content: string; audioUrl?: string }) {
  if (msg.audioUrl) { play(msg.audioUrl); return }
  try {
    const r = await api.tts(msg.content.slice(0, 300))
    play(r.audio_url)
  } catch (e: any) {
    message.error(e.message || t('chatView.ttsFailed'))
  }
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
  try { sessions.value = await api.getSessions() } catch (e: any) { message.error(e.message) }
}

async function switchSession(sid: string) {
  if (loadingSessionId.value) return  // 防止重复点击
  loadingSessionId.value = sid
  try {
    await chat.loadSession(sid)
    showSessions.value = false
  } catch (e: any) { message.error(e.message) }
  finally { loadingSessionId.value = '' }
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
      </div>
      <div class="toolbar-meta">
        <ModelSelector @change="onModelChange" />
        <span v-if="chat.sessionId" class="session-label" :title="chat.sessionId">{{ chat.sessionId }}</span>
      </div>
    </div>

    <div class="messages-area" ref="messagesEl">
      <div v-if="chat.messages.length === 0" class="empty-state">
        <div class="empty-icon">🌿</div>
        <p>{{ t('chatView.emptyPlaceholder') }}</p>
      </div>

      <transition-group name="msg-fade">
      <div v-for="msg in chat.messages" :key="msg.id" class="message-row" :class="msg.role">
        <div class="message-bubble glass-panel" :class="[msg.role, msg.streaming ? 'streaming' : '']">
          <div v-if="msg.role === 'assistant' && msg.emotion" class="emotion-dot"
               :style="{ background: emotionColors[msg.emotion] || '#9ca3af' }"
               :title="msg.emotion"></div>

          <div v-if="msg.toolCalls?.length" class="tool-calls">
            <ToolCallCard v-for="(tc, i) in msg.toolCalls" :key="i" :call="tc" />
          </div>

          <div v-if="msg.role === 'assistant' && msg.streaming" class="message-content md-body streaming-text">{{ replaceAgentNames(msg.content) }}</div>
          <div v-else-if="msg.role === 'assistant'" class="message-content md-body"
               v-html="renderMarkdown(replaceAgentNames(msg.content))"></div>
          <div v-else class="message-content plain">
            {{ msg.content }}
            <img v-if="msg.imageUrl" :src="msg.imageUrl" class="user-upload-img"
                 :class="{ loaded: imgSettled.has(msg.imageUrl) }"
                 loading="lazy" :title="t('chatView.zoom')"
                 @load="onImgSettled(msg.imageUrl!)" @error="onImgSettled(msg.imageUrl!)"
                 @click="lightboxUrl = msg.imageUrl!" />
          </div>
          <span v-if="msg.streaming && !msg.content" class="cursor-blink">▌</span>

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
          <!-- 表情包：贴在气泡尾部，不与产物混淆 -->
          <img v-if="msg.stickerUrl" :src="msg.stickerUrl + '?token=' + auth.token" class="sticker-img"
               :title="t('chatView.zoom')" @click="lightboxUrl = msg.stickerUrl + '?token=' + auth.token" />

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

<style scoped>
.jspace-fab {
  position: fixed; bottom: 80px; right: 20px; z-index: 100;
  width: 40px; height: 40px; border-radius: 50%;
  border: 1px solid var(--moon-border, #333);
  background: var(--moon-bg-soft, #1a1a2e);
  color: var(--moon-dim);
  font-size: 18px; cursor: pointer;
  transition: all .2s; display: flex; align-items: center; justify-content: center;
}
.jspace-fab:hover { border-color: var(--moon-accent, #7c6fff); color: var(--moon-accent, #7c6fff); }
.jspace-fab.active { background: var(--moon-accent, #7c6fff); color: #fff; border-color: var(--moon-accent, #7c6fff); }

.chat-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 8px;
}

.chat-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  min-width: 0;
  flex-shrink: 0;
}

.toolbar-actions,
.toolbar-meta {
  display: flex;
  align-items: center;
  min-width: 0;
}

.toolbar-actions {
  flex: 1 1 auto;
  flex-wrap: wrap;
  gap: 2px;
}

.toolbar-meta {
  flex: 0 1 auto;
  gap: 8px;
  max-width: 100%;
  margin-left: auto;
}

.toolbar-meta :deep(.model-chip) {
  max-width: min(260px, 45vw);
}

.session-label {
  display: block;
  flex: 0 1 180px;
  min-width: 0;
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: rgba(242, 247, 238, 0.3);
  font-family: 'JetBrains Mono', monospace;
}

.messages-area {
  flex: 1;
  overflow-y: auto;
  padding: 8px 4px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  contain: layout paint;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--moon-dim);
  gap: 12px;
}
.empty-state.small { height: 120px; }
.empty-icon { font-size: 48px; animation: breathe 3s ease-in-out infinite; }

.message-row {
  display: flex;
  width: fit-content;
  max-width: min(85%, 900px);
  min-width: 0;
  animation: slideUp 0.3s var(--ease-smooth);
}
.message-row.user { align-self: flex-end; justify-content: flex-end; }
.message-row.assistant { align-self: flex-start; }
.message-row.system { align-self: center; max-width: 70%; }

.message-bubble {
  padding: 10px 16px;
  position: relative;
  max-width: 100%;
  min-width: 0;
  overflow: hidden;
  line-height: 1.65;
  font-size: 14px;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.message-bubble.user {
  background: rgba(127, 214, 80, 0.12);
  border-color: rgba(127, 214, 80, 0.25);
  border-radius: 16px 16px 4px 16px;
}
.message-bubble.assistant { border-radius: 16px 16px 16px 4px; }
.message-bubble.system {
  background: rgba(232, 213, 163, 0.08);
  border-color: rgba(232, 213, 163, 0.2);
  font-size: 13px;
  text-align: center;
  color: var(--wisdom);
}
.message-bubble.streaming { border-color: rgba(127, 214, 80, 0.35); }

/* 消息进出场（切换提示 3 秒自动淡出） */
.msg-fade-enter-active { transition: opacity 0.3s var(--ease-smooth), transform 0.3s var(--ease-smooth); }
.msg-fade-enter-from { opacity: 0; transform: translateY(10px); }
.msg-fade-leave-active { transition: opacity 0.45s var(--ease-smooth), transform 0.45s var(--ease-smooth); }
.msg-fade-leave-to { opacity: 0; transform: translateY(-8px) scale(0.97); }
.msg-fade-move { transition: transform 0.45s var(--ease-smooth); }

.emotion-dot {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.cursor-blink {
  display: inline;
  animation: blink 1s step-end infinite;
  color: var(--dendro);
}
@keyframes blink { 50% { opacity: 0; } }

.message-content {
  min-width: 0;
  max-width: 100%;
}
.message-content.plain { white-space: pre-wrap; }
.message-content.streaming-text { white-space: pre-wrap; }
.user-upload-img {
  width: auto;
  max-width: min(240px, 100%);
  max-height: 240px;
  border-radius: 8px;
  object-fit: contain;
  cursor: zoom-in;
  margin-top: 6px;
  display: block;
  opacity: 0; transition: opacity 0.3s var(--ease-smooth);
  background: rgba(127, 214, 80, 0.06);
}
.user-upload-img.loaded { opacity: 1; }

.tool-calls { margin-bottom: 6px; }

.artifact-block {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  margin-top: 8px;
  padding: 8px 10px;
  overflow: hidden;
  border: 1px dashed rgba(232, 213, 163, 0.3);
  border-radius: 10px;
  background: rgba(232, 213, 163, 0.04);
}
.artifact-label {
  font-size: 11px;
  color: var(--wisdom);
  display: block;
  margin-bottom: 6px;
}
.media-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(160px, 100%), 1fr));
  gap: 8px;
  min-width: 0;
}
.media-image {
  width: 100%;
  max-width: 220px;
  height: auto;
  max-height: 220px;
  border-radius: 8px;
  object-fit: contain;
  cursor: zoom-in;
  opacity: 0; transition: opacity 0.3s var(--ease-smooth);
  background: rgba(127, 214, 80, 0.06);
}
.media-image.loaded { opacity: 1; }
.media-video {
  display: block;
  width: 100%;
  max-width: 100%;
  border-radius: 8px;
}
.media-audio {
  display: block;
  width: 100%;
  max-width: 100%;
  min-width: 0;
  height: 36px;
}
.sticker-img {
  width: auto;
  max-width: min(160px, 100%);
  height: auto;
  max-height: 160px;
  margin-top: 8px;
  border-radius: 12px;
  display: block;
  cursor: zoom-in;
  transition: transform 0.2s var(--ease-out);
}
.sticker-img:hover { transform: scale(1.04); }

.lightbox {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(4, 12, 8, 0.82);
  backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: zoom-out;
}
.lightbox img {
  max-width: 92vw;
  max-height: 92vh;
  border-radius: 12px;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.5);
}
.lightbox-fade-enter-active, .lightbox-fade-leave-active { transition: opacity 0.25s; }
.lightbox-fade-enter-from, .lightbox-fade-leave-to { opacity: 0; }

.bubble-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px;
  min-height: 28px;
  margin-top: 4px;
  opacity: 0;
  transition: opacity 0.2s;
}
.message-bubble:hover .bubble-footer,
.message-bubble:focus-within .bubble-footer { opacity: 1; }
.msg-time {
  margin-right: 2px;
  font-size: 11px;
  color: var(--moon-dim);
}

.footer-btn {
  width: 28px;
  height: 28px;
  background: none;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  color: var(--moon-dim);
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 28px;
  transition: color 0.2s, transform 0.15s, background 0.2s;
}
.footer-btn:hover {
  color: var(--dendro);
  background: rgba(127, 214, 80, 0.08);
  transform: scale(1.08);
}
.footer-btn.playing { animation: breathe 1s ease-in-out infinite; }

.input-area-wrapper {
  position: relative;
  flex-shrink: 0;
}

.session-list { display: flex; flex-direction: column; gap: 8px; }

.session-item {
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid var(--glass-border);
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s;
}
.session-item:hover { background: rgba(127, 214, 80, 0.06); }
.session-item.active { border-color: var(--dendro); }
/* 加载期间：列表显示等待光标作视觉反馈；仅当前加载项禁用交互。
   其余项的删除等操作仍可用——removeSession 与 loadSession 作用于不同会话，无状态冲突，
   全量禁用会在慢网络下冻结整个抽屉的删除能力（回归），故不采用。 */
.session-list.is-loading { cursor: wait; }
.session-item.loading { pointer-events: none; opacity: 0.55; }
.session-item.loading .session-title::after {
  content: '';
  display: inline-block;
  width: 10px; height: 10px;
  margin-left: 6px;
  border: 2px solid var(--dendro);
  border-top-color: transparent;
  border-radius: 50%;
  vertical-align: middle;
  animation: session-spin 0.7s linear infinite;
}
@keyframes session-spin { to { transform: rotate(360deg); } }

.session-source {
  font-size: 10px; padding: 1px 6px; border-radius: 8px; margin-right: 4px;
  background: rgba(127, 214, 80, 0.15); color: var(--dendro); font-weight: 700;
}
.session-source.qq { background: rgba(110, 168, 254, 0.15); color: #6ea8fe; }
.session-source.cli { background: rgba(232, 213, 163, 0.15); color: var(--wisdom); }

.session-title {
  font-size: 13px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.session-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
  color: var(--moon-dim);
  margin: 4px 0;
}
.session-preview {
  font-size: 12px;
  color: var(--moon-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@keyframes slideUp {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes breathe {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

/* Markdown 内样式 */
:deep(.md-body p) { margin: 0 0 6px; }
:deep(.md-body p:last-child) { margin-bottom: 0; }
:deep(.md-body pre.hljs) {
  display: block;
  width: 100%;
  max-width: 100%;
  box-sizing: border-box;
  background: rgba(10, 20, 14, 0.8);
  border-radius: 8px;
  padding: 10px 12px;
  overflow-x: auto;
  white-space: pre;
  margin: 6px 0;
  font-size: 12.5px;
  font-family: 'JetBrains Mono', monospace;
}
:deep(.md-body pre code) {
  display: block;
  width: max-content;
  min-width: 100%;
  box-sizing: border-box;
}
:deep(.md-body code:not(pre code)) {
  background: rgba(127, 214, 80, 0.12);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 12.5px;
  font-family: 'JetBrains Mono', monospace;
}
:deep(.md-body a) { color: var(--dendro); }
:deep(.md-body ul), :deep(.md-body ol) { padding-left: 20px; margin: 4px 0; }
:deep(.md-body blockquote) {
  border-left: 3px solid var(--dendro-dim);
  padding-left: 10px;
  color: var(--moon-dim);
  margin: 6px 0;
}
:deep(.md-body table) {
  display: block;
  width: max-content;
  max-width: 100%;
  overflow-x: auto;
  border-collapse: collapse;
  margin: 6px 0;
}
:deep(.md-body th), :deep(.md-body td) {
  border: 1px solid var(--glass-border);
  padding: 4px 10px;
  font-size: 13px;
}

@media (hover: none), (pointer: coarse) {
  .bubble-footer { opacity: 1; }
}

@media (max-width: 768px) {
  .chat-toolbar {
    align-items: stretch;
    gap: 4px;
  }

  .toolbar-actions {
    flex: 1 1 100%;
    gap: 0;
  }

  .toolbar-actions :deep(.n-button) {
    --n-height: 28px !important;
    --n-padding: 0 6px !important;
    font-size: 11px;
  }

  .toolbar-meta {
    flex: 1 1 100%;
    justify-content: flex-end;
    gap: 6px;
  }

  .toolbar-meta :deep(.model-chip) {
    max-width: min(230px, 62vw);
    min-height: 28px;
  }

  .session-label {
    flex-basis: 25vw;
    max-width: 25vw;
  }

  .messages-area {
    padding-inline: 0;
  }

  .message-row,
  .message-row.system {
    max-width: 96%;
  }

  .message-bubble {
    padding: 8px 10px;
  }

  .media-grid {
    grid-template-columns: repeat(auto-fit, minmax(min(128px, 100%), 1fr));
  }

  .media-image {
    max-width: 100%;
  }

  .jspace-fab {
    right: 12px;
    bottom: calc(150px + env(safe-area-inset-bottom, 0px));
    width: 36px;
    height: 36px;
    font-size: 16px;
  }
}

@media (max-width: 420px) {
  .toolbar-actions :deep(.n-button) {
    --n-padding: 0 4px !important;
  }

  .session-label {
    flex-basis: 20vw;
    max-width: 20vw;
  }
}
</style>