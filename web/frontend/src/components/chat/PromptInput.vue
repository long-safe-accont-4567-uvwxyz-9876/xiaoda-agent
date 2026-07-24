<script setup lang="ts">
import { ref, computed, nextTick, watch, onMounted, onBeforeUnmount } from 'vue'
import { useMessage } from 'naive-ui'
import { api } from '../../api'
import { t } from '../../i18n'

const props = withDefaults(defineProps<{
  modelValue: string
  isLoading: boolean
  disabled?: boolean
  placeholder?: string
}>(), {
  disabled: false,
  placeholder: t('promptInput.inputPlaceholder'),
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'send': [text: string, options: { search?: boolean; think?: boolean; imageUrl?: string }]
  'abort': []
}>()

const message = useMessage()

const showSearch = ref(false)
const showThink = ref(false)
const isRecording = ref(false)
const isTranscribing = ref(false)
const uploadedImage = ref<{ url: string; name: string } | null>(null)
const imagePreviewUrl = ref('')
const recordingTime = ref(0)
const showLightbox = ref(false)

const textareaRef = ref<HTMLTextAreaElement | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const isDragging = ref(false)

let mediaRecorder: MediaRecorder | null = null
let audioChunks: Blob[] = []
let recordingTimer: ReturnType<typeof setInterval> | null = null

const hasContent = computed(() => props.modelValue.trim().length > 0)

const currentPlaceholder = computed(() => {
  if (showSearch.value) return t('promptInput.searchWeb') + '...'
  if (showThink.value) return t('promptInput.thinkingDeep') + '...'
  return props.placeholder
})

function onInput(e: Event) {
  const val = (e.target as HTMLTextAreaElement).value
  emit('update:modelValue', val)
  autoGrow()
}

function autoGrow() {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 240) + 'px'
}

/** 聚焦输入框（供父组件通过 ref 调用，替代脆弱的 querySelector） */
function focus() {
  textareaRef.value?.focus()
}

defineExpose({ focus, textareaRef })

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

function handleSend() {
  const text = props.modelValue.trim()
  if (!text || props.isLoading || props.disabled) return
  const options: { search?: boolean; think?: boolean; imageUrl?: string } = {}
  if (showSearch.value) options.search = true
  if (showThink.value) options.think = true
  if (uploadedImage.value) options.imageUrl = uploadedImage.value.url
  emit('send', text, options)
  if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
  uploadedImage.value = null
  imagePreviewUrl.value = ''
  showSearch.value = false
  showThink.value = false
  nextTick(() => autoGrow())
}

function toggleSearch() {
  showSearch.value = !showSearch.value
  if (showSearch.value) showThink.value = false
}

function toggleThink() {
  showThink.value = !showThink.value
  if (showThink.value) showSearch.value = false
}

// 图片上传
function triggerFileInput() {
  fileInputRef.value?.click()
}

async function onFileSelected(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  await uploadFile(file)
  input.value = ''
}

async function uploadFile(file: File) {
  if (!file.type.startsWith('image/')) return
  try {
    if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
    imagePreviewUrl.value = URL.createObjectURL(file)
    const result = await api.uploadImage(file)
    uploadedImage.value = result
  } catch {
    if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
    imagePreviewUrl.value = ''
    uploadedImage.value = null
  }
}

function removeImage() {
  if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
  uploadedImage.value = null
  imagePreviewUrl.value = ''
}

function openLightbox() {
  showLightbox.value = true
}

function closeLightbox() {
  showLightbox.value = false
}

// 拖拽
function onDragOver(e: DragEvent) {
  e.preventDefault()
  isDragging.value = true
}

function onDragLeave() {
  isDragging.value = false
}

async function onDrop(e: DragEvent) {
  e.preventDefault()
  isDragging.value = false
  const file = e.dataTransfer?.files?.[0]
  if (file) await uploadFile(file)
}

// 粘贴
async function onPaste(e: ClipboardEvent) {
  const items = e.clipboardData?.items
  if (!items) return
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault()
      const file = item.getAsFile()
      if (file) await uploadFile(file)
      return
    }
  }
}

// 语音录音
async function toggleRecording() {
  if (isTranscribing.value) return  // 识别中不允许操作
  if (isRecording.value) {
    stopRecording()
  } else {
    await startRecording()
  }
}

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    mediaRecorder = new MediaRecorder(stream)
    audioChunks = []
    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data)
    }
    mediaRecorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop())
      const blob = new Blob(audioChunks, { type: 'audio/webm' })
      isTranscribing.value = true
      try {
        const result = await api.speechToText(new File([blob], 'recording.webm', { type: 'audio/webm' }))
        if (result.text) {
          emit('update:modelValue', props.modelValue + result.text)
          nextTick(() => {
            autoGrow()
            focus()
          })
        }
      } catch (e) {
        // 识别失败显示提示，不再静默吞错
        message.error(t('promptInput.voiceFailed'))
      } finally {
        isTranscribing.value = false
        isRecording.value = false
        recordingTime.value = 0
      }
    }
    mediaRecorder.start()
    isRecording.value = true
    recordingTime.value = 0
    recordingTimer = setInterval(() => {
      recordingTime.value++
    }, 1000)
  } catch {
    isRecording.value = false
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop()
  }
  if (recordingTimer) {
    clearInterval(recordingTimer)
    recordingTimer = null
  }
}

function formatRecordingTime(seconds: number): string {
  const m = Math.floor(seconds / 60)
  const s = seconds % 60
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`
}

onMounted(() => {
  // 监听粘贴
  document.addEventListener('paste', onPaste as any)
})

onBeforeUnmount(() => {
  document.removeEventListener('paste', onPaste as any)
  if (recordingTimer) clearInterval(recordingTimer)
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    mediaRecorder.stop()
  }
  if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
})

// 外部 modelValue 变化时自动增高
watch(() => props.modelValue, () => {
  nextTick(() => autoGrow())
})
</script>

<template>
  <div
    class="prompt-input glass-panel"
    :class="{ dragging: isDragging, disabled }"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <!-- 图片预览区 -->
    <transition name="preview-slide">
      <div v-if="uploadedImage || imagePreviewUrl" class="image-preview-area">
        <div class="image-thumb" @click="openLightbox">
          <img :src="imagePreviewUrl || uploadedImage?.url" :alt="t('chatView.preview')" />
        </div>
        <button class="image-remove" @click="removeImage" :title="t('promptInput.removeImage')">✕</button>
      </div>
    </transition>

    <!-- 录音波形区 -->
    <div v-if="isRecording" class="recording-area">
      <div class="recording-indicator"></div>
      <span class="recording-time">{{ formatRecordingTime(recordingTime) }}</span>
      <div class="waveform">
        <span v-for="i in 5" :key="i" class="wave-bar" :style="{ animationDelay: `${i * 0.12}s` }"></span>
      </div>
    </div>

    <!-- Textarea 输入区 -->
    <textarea
      v-show="!isRecording"
      ref="textareaRef"
      class="prompt-textarea"
      :value="modelValue"
      :placeholder="currentPlaceholder"
      :disabled="disabled"
      rows="1"
      @input="onInput"
      @keydown="handleKeydown"
    ></textarea>

    <!-- 底部功能按钮行 -->
    <div class="prompt-toolbar">
      <div class="toolbar-left">
        <!-- 附件上传 -->
        <button class="tool-btn" :title="t('promptInput.uploadImage')" @click="triggerFileInput" :disabled="disabled">
          📎
        </button>
        <input
          ref="fileInputRef"
          type="file"
          accept="image/*"
          style="display: none"
          @change="onFileSelected"
        />

        <!-- 分隔线 -->
        <span class="tool-divider"></span>

        <!-- 搜索模式 -->
        <button
          class="tool-btn"
          :class="{ active: showSearch, 'search-active': showSearch }"
          :title="t('promptInput.searchWeb')"
          @click="toggleSearch"
          :disabled="disabled"
        >
          🌐
          <transition name="label-fade">
            <span v-if="showSearch" class="mode-label search-label">Search</span>
          </transition>
        </button>

        <!-- 分隔线 -->
        <span class="tool-divider"></span>

        <!-- 深度思考 -->
        <button
          class="tool-btn"
          :class="{ active: showThink, 'think-active': showThink }"
          :title="t('promptInput.deepThink')"
          @click="toggleThink"
          :disabled="disabled"
        >
          🧠
          <transition name="label-fade">
            <span v-if="showThink" class="mode-label think-label">Think</span>
          </transition>
        </button>
      </div>

      <div class="toolbar-right">
        <!-- 语音按钮（无内容时显示） -->
        <button
          v-if="!hasContent && !isLoading"
          class="tool-btn ghost"
          :class="{ 'is-transcribing': isTranscribing }"
          :title="t('promptInput.voiceInput')"
          @click="toggleRecording"
          :disabled="disabled || isTranscribing"
          :loading="isTranscribing"
        >
          <span v-if="isTranscribing" class="transcribing-spinner"></span>
          <span v-else>🎤</span>
        </button>

        <!-- 发送按钮（有内容时显示） -->
        <button
          v-if="hasContent && !isLoading"
          class="send-btn dendro-btn"
          @click="handleSend"
          :disabled="disabled"
          :title="t('promptInput.send')"
        >
          ↑
        </button>

        <!-- 停止按钮（isLoading 时显示） -->
        <button
          v-if="isLoading"
          class="stop-btn"
          @click="emit('abort')"
          :title="t('promptInput.abort')"
        >
          ⏹
        </button>
      </div>
    </div>

    <!-- Lightbox -->
    <teleport to="body">
      <transition name="lightbox-fade">
        <div v-if="showLightbox" class="prompt-lightbox" @click="closeLightbox">
          <img :src="imagePreviewUrl || uploadedImage?.url" :alt="t('chatView.preview')" />
        </div>
      </transition>
    </teleport>
  </div>
</template>

<style scoped>
.prompt-input {
  border-radius: 24px;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  position: relative;
  transition: border-color 0.2s, box-shadow 0.2s;
}

.prompt-input.dragging {
  border-color: var(--dendro);
  box-shadow: 0 0 0 2px rgba(127, 214, 80, 0.25);
}

.prompt-input.disabled {
  opacity: 0.5;
  pointer-events: none;
}

/* 图片预览区 */
.image-preview-area {
  display: flex;
  align-items: center;
  gap: 8px;
  position: relative;
  padding: 4px 0;
}

.image-thumb {
  width: 64px;
  height: 64px;
  border-radius: 12px;
  overflow: hidden;
  cursor: zoom-in;
  border: 1px solid var(--glass-border);
  flex-shrink: 0;
}

.image-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.image-remove {
  position: absolute;
  top: 0;
  left: 52px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: rgba(217, 106, 95, 0.9);
  color: #fff;
  border: none;
  cursor: pointer;
  font-size: 11px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: transform 0.15s;
}

.image-remove:hover {
  transform: scale(1.15);
}

.preview-slide-enter-active,
.preview-slide-leave-active {
  transition: opacity 0.25s var(--ease-smooth), transform 0.25s var(--ease-smooth);
}

.preview-slide-enter-from,
.preview-slide-leave-to {
  opacity: 0;
  max-height: 0;
  padding: 0;
}

/* 录音波形区 */
.recording-area {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  min-height: 40px;
}

.recording-indicator {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #f87171;
  animation: pulse-red 1.2s ease-in-out infinite;
  flex-shrink: 0;
}

@keyframes pulse-red {
  0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(248, 113, 113, 0.6); }
  50% { opacity: 0.7; box-shadow: 0 0 0 6px rgba(248, 113, 113, 0); }
}

.recording-time {
  font-family: 'JetBrains Mono', monospace;
  font-size: 14px;
  color: #f87171;
  min-width: 44px;
}

.waveform {
  display: flex;
  align-items: center;
  gap: 3px;
  flex: 1;
  height: 24px;
}

.wave-bar {
  width: 3px;
  background: #f87171;
  border-radius: 2px;
  animation: wave-anim 0.8s ease-in-out infinite alternate;
}

@keyframes wave-anim {
  0% { height: 4px; }
  100% { height: 20px; }
}

/* Textarea */
.prompt-textarea {
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  color: var(--moon);
  font-size: 14px;
  line-height: 1.5;
  width: 100%;
  min-height: 24px;
  max-height: 240px;
  padding: 0;
  font-family: inherit;
}

.prompt-textarea::placeholder {
  color: var(--moon-dim);
}

.prompt-textarea:focus {
  outline: none;
}

/* 底部工具栏 */
.prompt-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 4px;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 4px;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 6px;
}

.tool-btn {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--moon-dim);
  font-size: 16px;
  padding: 4px 6px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  transition: background 0.2s, color 0.2s, border-color 0.2s;
  line-height: 1;
}

.tool-btn:hover {
  background: rgba(127, 214, 80, 0.08);
  color: var(--moon);
}

.tool-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.tool-btn.ghost {
  background: none;
}

.tool-btn.ghost:hover {
  background: rgba(127, 214, 80, 0.08);
}

.tool-btn.is-transcribing {
  cursor: wait;
  opacity: 0.7;
}

.transcribing-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: transcribing-spin 0.8s linear infinite;
}

@keyframes transcribing-spin {
  to { transform: rotate(360deg); }
}

.tool-btn.search-active {
  background: rgba(127, 214, 80, 0.15);
  color: var(--dendro);
  border: 1px solid var(--dendro);
}

.tool-btn.think-active {
  background: rgba(167, 139, 250, 0.15);
  color: #a78bfa;
  border: 1px solid #a78bfa;
}

.mode-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.3px;
}

.search-label {
  color: var(--dendro);
}

.think-label {
  color: #a78bfa;
}

.label-fade-enter-active,
.label-fade-leave-active {
  transition: opacity 0.2s, transform 0.2s;
}

.label-fade-enter-from,
.label-fade-leave-to {
  opacity: 0;
  transform: translateX(-4px);
}

/* 分隔线 */
.tool-divider {
  width: 1px;
  height: 16px;
  background: linear-gradient(
    to bottom,
    transparent,
    rgba(127, 214, 80, 0.3),
    transparent
  );
  margin: 0 2px;
  flex-shrink: 0;
}

/* 发送按钮 */
.send-btn {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 700;
  flex-shrink: 0;
}

.send-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 停止按钮 */
.stop-btn {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: #f87171;
  color: #fff;
  border: none;
  cursor: pointer;
  font-size: 12px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: transform 0.15s, box-shadow 0.15s;
}

.stop-btn:hover {
  transform: scale(1.1);
  box-shadow: 0 0 12px rgba(248, 113, 113, 0.5);
}

/* Lightbox */
.prompt-lightbox {
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

.prompt-lightbox img {
  max-width: 92vw;
  max-height: 92vh;
  border-radius: 12px;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.5);
}

.lightbox-fade-enter-active,
.lightbox-fade-leave-active {
  transition: opacity 0.25s;
}

.lightbox-fade-enter-from,
.lightbox-fade-leave-to {
  opacity: 0;
}
</style>