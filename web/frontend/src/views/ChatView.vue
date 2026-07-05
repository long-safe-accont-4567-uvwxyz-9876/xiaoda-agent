<script setup lang="ts">
import { ref, nextTick, watch, onMounted, computed, inject } from 'vue'
import type { Ref } from 'vue'
import { NDrawer, NDrawerContent, NButton, NPopconfirm, useMessage } from 'naive-ui'
import { useChatStore } from '../stores/chat'
import { useUiStore } from '../stores/ui'
import { api, exportSessionUrl } from '../api'
import { renderMarkdown } from '../utils/markdown'
import ToolCallCard from '../components/chat/ToolCallCard.vue'
import ChatTerminal from '../components/chat/ChatTerminal.vue'
import SlashPalette from '../components/chat/SlashPalette.vue'
import PromptInput from '../components/chat/PromptInput.vue'
import SumeruIcon from '../components/fx/SumeruIcon.vue'
import ModelSelector from '../components/chat/ModelSelector.vue'
import { t } from '../i18n'

const chat = useChatStore()
const ui = useUiStore()
const message = useMessage()
const particles = inject<Ref<any>>('particles')

const inputText = ref('')
const messagesEl = ref<HTMLElement | null>(null)
const inputEl = ref<HTMLTextAreaElement | null>(null)
const paletteRef = ref<InstanceType<typeof SlashPalette> | null>(null)
const commands = ref<Array<{ name: string; description: string; owner_only: boolean }>>([])
const showSessions = ref(false)
const sessions = ref<any[]>([])
const playingUrl = ref('')
const lightboxUrl = ref('')
const authToken = localStorage.getItem('token') || ''
let audioEl: HTMLAudioElement | null = null

const showPalette = computed(() => inputText.value.startsWith('/') && !inputText.value.includes(' '))

onMounted(async () => {
  try {
    // 后端命令名自带 "/" 前缀，统一去掉，避免拼接成 "//cmd"
    const raw = await api.getCommands()
    commands.value = raw.map(c => ({ ...c, name: c.name.replace(/^\/+/, '') }))
  } catch { /* 忽略 */ }
})

watch(() => chat.messages.length, async () => {
  await nextTick()
  const el = messagesEl.value
  if (!el) return
  // 仅在用户位于底部附近时自动滚动，避免打断上翻阅读
  const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
  if (distanceFromBottom > 100) return
  // 流式期间用 auto（即时跟随），非流式时用 smooth
  const isStreaming = chat.messages.some(m => m.streaming)
  el.scrollTo({ top: el.scrollHeight, behavior: isStreaming ? 'auto' : 'smooth' })
})

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
  } catch { /* TTS 不可用时静默 */ }
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
  if (audioEl) { audioEl.pause(); audioEl = null }
  if (playingUrl.value === url) { playingUrl.value = ''; return }
  audioEl = new Audio(url)
  playingUrl.value = url
  audioEl.onended = () => { playingUrl.value = '' }
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
  const text = inputText.value.trim()
  if (!text || chat.isProcessing) return
  chat.sendMessage(text)
  inputText.value = ''
  // 发送特效：从输入框爆叶子
  const rect = inputEl.value?.getBoundingClientRect()
  if (rect) particles?.value?.burst?.(rect.left + rect.width / 2, rect.top, 10)
  autoGrow()
}

function handlePromptSend(text: string, options: { search?: boolean; think?: boolean; imageUrl?: string }) {
  if (!text || chat.isProcessing) return
  let finalText = text
  if (options.search) finalText = `[Search: ${text}]`
  else if (options.think) finalText = `[Think: ${text}]`
  if (options.imageUrl) {
    finalText += `\n[Image: ${options.imageUrl}]`
  }
  chat.sendMessage(finalText, options.imageUrl)
  inputText.value = ''
  // 发送特效
  const rect = inputEl.value?.getBoundingClientRect()
  if (rect) particles?.value?.burst?.(rect.left + rect.width / 2, rect.top, 10)
}

function handleKeydown(e: KeyboardEvent) {
  if (showPalette.value && paletteRef.value?.hasItems()) {
    if (e.key === 'ArrowDown') { e.preventDefault(); paletteRef.value.move(1); return }
    if (e.key === 'ArrowUp') { e.preventDefault(); paletteRef.value.move(-1); return }
    if (e.key === 'Tab' || e.key === 'Enter') { e.preventDefault(); paletteRef.value.confirm(); return }
    if (e.key === 'Escape') { inputText.value = ''; return }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

function selectCommand(name: string) {
  inputText.value = `/${name.replace(/^\/+/, '')} `
  inputEl.value?.focus()
}

function autoGrow() {
  const el = inputEl.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 120) + 'px'
}

async function openSessions() {
  showSessions.value = true
  try { sessions.value = await api.getSessions() } catch (e: any) { message.error(e.message) }
}

async function switchSession(sid: string) {
  try {
    await chat.loadSession(sid)
    showSessions.value = false
  } catch (e: any) { message.error(e.message) }
}

async function removeSession(sid: string) {
  try {
    await api.deleteSession(sid)
    sessions.value = sessions.value.filter(s => s.session_id !== sid)
    message.success(t('chatView.session') + ' ' + t('deleted'))
  } catch (e: any) { message.error(e.message) }
}

async function startNew() {
  await chat.newSession()
  showSessions.value = false
  message.success(t('chatView.newSessionStarted'))
}

function copyText(text: string) {
  navigator.clipboard.writeText(text)
  message.success(t('chatView.copied'))
}

function resend(msg: { content: string }) {
  if (chat.isProcessing) return
  chat.sendMessage(msg.content)
}

function clearAll() {
  chat.clearMessages()
  message.success(t('chatView.cleared'))
}

function onModelChange(_provider: string, _modelId: string) {
}

function fmtTime(ts: number): string {
  return new Date(ts).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

const emotionColors: Record<string, string> = {
  '喜悦': '#7fd650', '悲伤': '#60a5fa', '愤怒': '#f87171', '焦虑': '#fbbf24',
  '害羞': '#f9a8d4', '好奇': '#a78bfa', '思考': '#67e8f9', '恐惧': '#94a3b8', '平静': '#9ca3af',
}
</script>

<template>
  <div class="chat-view">
    <div class="chat-toolbar">
      <n-button size="tiny" quaternary @click="openSessions">
        <template #icon><SumeruIcon name="sessions" :size="15" /></template>{{ t('chatView.session') }}
      </n-button>
      <n-button size="tiny" quaternary @click="startNew">
        <template #icon><SumeruIcon name="sprout" :size="15" /></template>{{ t('chatView.newChat') }}
      </n-button>
      <n-button size="tiny" quaternary @click="clearAll">
        <template #icon><SumeruIcon name="trash" :size="15" /></template>{{ t('chatView.clear') }}
      </n-button>
      <a v-if="chat.sessionId" class="export-link"
         :href="exportSessionUrl(chat.sessionId)" target="_blank">⬇ {{ t('chatView.export') }}</a>
      <ModelSelector style="margin-left: auto" @change="onModelChange" />
      <span class="session-label">{{ chat.sessionId }}</span>
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

          <div v-if="msg.role === 'assistant' && msg.streaming" class="message-content md_body streaming-text">{{ msg.content }}</div>
          <div v-else-if="msg.role === 'assistant'" class="message-content md_body"
               v-html="renderMarkdown(msg.content)"></div>
          <div v-else class="message-content plain">
            {{ msg.content }}
            <img v-if="msg.imageUrl" :src="msg.imageUrl" class="user-upload-img"
                 loading="lazy" :title="t('chatView.zoom')" @click="lightboxUrl = msg.imageUrl!" />
          </div>
          <span v-if="msg.streaming && !msg.content" class="cursor-blink">▌</span>

          <!-- 生成产物区（工具产出的图/视频/语音，与表情包分离） -->
          <div v-if="msg.imageUrls?.length || msg.videoUrl || msg.audioUrl" class="artifact-block">
            <span class="artifact-label">🎨 {{ t('chatView.artifacts') }}</span>
            <div v-if="msg.imageUrls?.length" class="media-grid">
              <img v-for="url in msg.imageUrls" :key="url" :src="url" class="media-image"
                   loading="lazy" :title="t('chatView.zoom')" @click="lightboxUrl = url" />
            </div>
            <video v-if="msg.videoUrl" :src="msg.videoUrl" controls class="media-video"></video>
            <audio v-if="msg.audioUrl" :src="msg.audioUrl" controls class="media-audio"></audio>
          </div>
          <!-- 表情包：贴在气泡尾部，不与产物混淆 -->
          <img v-if="msg.stickerUrl" :src="msg.stickerUrl + '?token=' + authToken" class="sticker-img"
               :title="t('chatView.zoom')" @click="lightboxUrl = msg.stickerUrl + '?token=' + authToken" />

          <div class="bubble-footer" v-if="!msg.streaming && msg.content && msg.role !== 'system'">
            <span class="msg-time">{{ fmtTime(msg.timestamp) }}</span>
            <template v-if="msg.role === 'assistant'">
              <button class="footer-btn" :class="{ playing: playingUrl && playingUrl === msg.audioUrl }"
                      :title="t('chatView.readAloud')" @click="speak(msg)"><SumeruIcon name="speak" :size="14" /></button>
              <button class="footer-btn" :title="t('chatView.copy')" @click="copyText(msg.content)"><SumeruIcon name="copy" :size="14" /></button>
              <button class="footer-btn" :title="t('chatView.regenerate')" @click="chat.retryLast()"><SumeruIcon name="retry" :size="14" /></button>
            </template>
            <template v-else>
              <button class="footer-btn" :title="t('chatView.copy')" @click="copyText(msg.content)"><SumeruIcon name="copy" :size="14" /></button>
              <button class="footer-btn" :title="t('chatView.resend')" @click="resend(msg)"><SumeruIcon name="retry" :size="14" /></button>
            </template>
            <button class="footer-btn" :title="t('chatView.withdraw')"
                    @click="chat.deleteMessage(msg.id)"><SumeruIcon name="trash" :size="14" /></button>
          </div>
        </div>
      </div>
      </transition-group>
    </div>

    <teleport to="body">
      <transition name="lightbox-fade">
        <div v-if="lightboxUrl" class="lightbox" @click="lightboxUrl = ''"
             @keydown.esc="lightboxUrl = ''" tabindex="-1">
          <img :src="lightboxUrl" :alt="t('chatView.preview')" />
        </div>
      </transition>
    </teleport>

    <div class="input-area-wrapper">
      <SlashPalette ref="paletteRef" :commands="commands" :filter="inputText"
                    :visible="showPalette" @select="selectCommand" />
      <PromptInput
        v-model="inputText"
        :is-loading="chat.isProcessing"
        :placeholder="t('chatView.inputPlaceholder')"
        @send="handlePromptSend"
        @abort="chat.abort()"
      />
    </div>

    <n-drawer v-model:show="showSessions" :width="340" placement="left">
      <n-drawer-content :title="'📂 ' + t('chatView.history')" closable>
        <div class="session-list">
          <div v-for="s in sessions" :key="s.session_id" class="session-item"
               :class="{ active: s.session_id === chat.sessionId }"
               @click="switchSession(s.session_id)">
            <div class="session-title">
              <span class="session-source" :class="s.source">{{
                s.source === 'qq' ? 'QQ' : s.source === 'cli' ? 'CLI' : 'Web' }}</span>
              {{ s.title || s.session_id }}
            </div>
            <div class="session-meta">
              <span>{{ s.message_count }} {{ t('chatView.messages') }} · {{ new Date(s.updated_at * 1000).toLocaleString('zh-CN') }}</span>
              <n-popconfirm @positive-click.stop="removeSession(s.session_id)">
                <template #trigger>
                  <button class="footer-btn" @click.stop>🗑</button>
                </template>
                {{ t('chatView.deleteConfirm') }}
              </n-popconfirm>
            </div>
            <div class="session-preview">{{ s.last_message }}</div>
          </div>
          <div v-if="!sessions.length" class="empty-state small">
            <p>{{ t('chatView.noHistory') }}</p>
          </div>
        </div>
      </n-drawer-content>
    </n-drawer>

    <!-- 纳西妲终端（右侧浮动面板，Teleport to body） -->
    <ChatTerminal />
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  gap: 8px;
}

.chat-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.export-link {
  font-size: 12px;
  color: var(--moon-dim);
  text-decoration: none;
}
.export-link:hover { color: var(--dendro); }

.session-label {
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
  max-width: 85%;
  animation: slideUp 0.3s var(--ease-smooth);
}
.message-row.user { align-self: flex-end; justify-content: flex-end; }
.message-row.assistant { align-self: flex-start; }
.message-row.system { align-self: center; max-width: 70%; }

.message-bubble {
  padding: 10px 16px;
  position: relative;
  line-height: 1.65;
  font-size: 14px;
  word-break: break-word;
  min-width: 60px;
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

.message-content.plain { white-space: pre-wrap; }
.message-content.streaming-text { white-space: pre-wrap; }
.user-upload-img {
  max-width: 240px; max-height: 240px; border-radius: 10px;
  object-fit: cover; cursor: zoom-in; margin-top: 6px; display: block;
}

.tool-calls { margin-bottom: 6px; }

.artifact-block {
  margin-top: 8px;
  padding: 8px 10px;
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
.media-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.media-image { max-width: 220px; max-height: 220px; border-radius: 8px; object-fit: cover; cursor: zoom-in; }
.media-video { max-width: 100%; border-radius: 8px; }
.media-audio { width: 100%; height: 36px; }
.sticker-img {
  max-width: 160px;
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
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  opacity: 0;
  transition: opacity 0.2s;
}
.message-bubble:hover .bubble-footer { opacity: 1; }
.msg-time { font-size: 11px; color: var(--moon-dim); }

.footer-btn {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--moon-dim);
  padding: 2px;
  display: inline-flex;
  align-items: center;
  transition: color 0.2s, transform 0.15s;
}
.footer-btn:hover { color: var(--dendro); transform: scale(1.15); }
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
  background: rgba(10, 20, 14, 0.8);
  border-radius: 8px;
  padding: 10px 12px;
  overflow-x: auto;
  margin: 6px 0;
  font-size: 12.5px;
  font-family: 'JetBrains Mono', monospace;
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
:deep(.md-body table) { border-collapse: collapse; margin: 6px 0; }
:deep(.md-body th), :deep(.md-body td) {
  border: 1px solid var(--glass-border);
  padding: 4px 10px;
  font-size: 13px;
}

@media (max-width: 768px) {
  .message-row { max-width: 95%; }
}
</style>
