<script setup lang="ts">
import { ref, computed, nextTick, watch, onMounted, onBeforeUnmount } from 'vue'
import SumeruIcon from '../../components/fx/SumeruIcon.vue'
import { useMessage } from 'naive-ui'
import { getWsClient } from '../../api/ws'
import { t } from '../../i18n'
import type { ChatRequestSnapshot } from '../../stores/chat'
import WorkingDirSelector from '../workspace/WorkingDirSelector.vue'
import { usePromptUploads } from './prompt-input/usePromptUploads'
import { useVoiceRecording } from './prompt-input/useVoiceRecording'
import { useTextareaAutogrow } from './prompt-input/useTextareaAutogrow'
import { firstImageItemFile } from './prompt-input/fileIntake'
import { formatRecordingTime } from './prompt-input/formatRecordingTime'
import AttachmentPreviews from './prompt-input/AttachmentPreviews.vue'
import RecordingIndicator from './prompt-input/RecordingIndicator.vue'
import ImageLightbox from './prompt-input/ImageLightbox.vue'

const props = withDefaults(defineProps<{
  modelValue: string
  isLoading: boolean
  connected: boolean
  disabled?: boolean
  placeholder?: string
  // 斜杠命令面板的组合框 ARIA 状态（由父层驱动，挂到 textarea 上）
  comboboxExpanded?: boolean
  comboboxControls?: string
  comboboxActiveOption?: string
}>(), {
  disabled: false,
  placeholder: t('promptInput.inputPlaceholder'),
  comboboxExpanded: false,
  comboboxControls: undefined,
  comboboxActiveOption: undefined,
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'send': [request: ChatRequestSnapshot]
  'abort': []
  'keydown': [event: KeyboardEvent]
}>()

const message = useMessage()

const showSearch = ref(false)
const showThink = ref(false)
const statusKey = ref('')
const isDragging = ref(false)

const { textareaRef, autoGrow, focus, scheduleAutoGrow } = useTextareaAutogrow()

const {
  uploadedImage, uploadedDoc, imagePreviewUrl, uploadState,
  hasAttachment: uploadHasAttachment, showLightbox,
  uploadFile, removeImage, removeDoc, openLightbox, closeLightbox, resetAttachments,
} = usePromptUploads(statusKey)

const {
  isRecording, isTranscribing, recordingTime, toggleRecording,
} = useVoiceRecording({
  message,
  appendTranscript(text: string) {
    emit('update:modelValue', props.modelValue + text)
    nextTick(() => {
      autoGrow()
      focus()
    })
  },
})

const fileInputRef = ref<HTMLInputElement | null>(null)

// Keep the public component contract explicit while attachment state lives in the upload composable.
const hasAttachment = computed(() => uploadHasAttachment.value)
const hasSendableContent = computed(() => props.modelValue.trim().length > 0 || hasAttachment.value)
const canSend = computed(() =>
  hasSendableContent.value && uploadState.value !== 'uploading' &&
  !props.isLoading && props.connected && !props.disabled,
)
const statusText = computed(() => {
  if (uploadState.value === 'uploading') return t('promptInput.uploading')
  if (!props.connected) return t('promptInput.disconnectedDraftKept')
  return statusKey.value ? t(statusKey.value) : ''
})

const currentPlaceholder = computed(() => {
  if (showSearch.value) return t('promptInput.searchWeb') + '...'
  if (showThink.value) return t('promptInput.thinkingDeep') + '...'
  return props.placeholder
})

defineExpose({ focus, textareaRef, clearSubmittedDraft })

function onInput(e: Event) {
  const val = (e.target as HTMLTextAreaElement).value
  emit('update:modelValue', val)
  autoGrow()
}

function handleKeydown(e: KeyboardEvent) {
  emit('keydown', e)
  if (e.defaultPrevented) return
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

function handleSend() {
  if (!canSend.value) return
  const request: ChatRequestSnapshot = {
    text: props.modelValue.trim(),
    search: showSearch.value,
    think: showThink.value,
    attachments: [],
  }
  if (uploadedImage.value) request.attachments.push({ kind: 'image', ...uploadedImage.value })
  if (uploadedDoc.value) request.attachments.push({ kind: 'document', ...uploadedDoc.value })
  emit('send', request)
}

function clearSubmittedDraft() {
  resetAttachments()
  showSearch.value = false
  showThink.value = false
  scheduleAutoGrow()
}

function retryConnection() {
  statusKey.value = getWsClient().retry() ? 'promptInput.reconnecting' : 'promptInput.disconnectedDraftKept'
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
  const hit = firstImageItemFile(e.clipboardData?.items ?? null)
  if (!hit.found) return
  e.preventDefault()
  if (hit.file) await uploadFile(hit.file)
}

onMounted(() => {
  // 监听粘贴
  document.addEventListener('paste', onPaste as any)
})

onBeforeUnmount(() => {
  document.removeEventListener('paste', onPaste as any)
})

// 外部 modelValue 变化时自动增高
watch(() => props.modelValue, () => {
  scheduleAutoGrow()
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
    <AttachmentPreviews
      :image="uploadedImage"
      :preview-url="imagePreviewUrl"
      :doc="uploadedDoc"
      @remove-image="removeImage"
      @open-lightbox="openLightbox"
      @remove-doc="removeDoc"
    />

    <RecordingIndicator v-if="isRecording" :time="recordingTime" />

    <!-- Textarea 输入区 -->
    <textarea
      v-show="!isRecording"
      ref="textareaRef"
      class="prompt-textarea"
      :value="modelValue"
      :placeholder="currentPlaceholder"
      :disabled="disabled"
      rows="1"
      role="combobox"
      :aria-label="currentPlaceholder"
      aria-autocomplete="list"
      :aria-expanded="comboboxExpanded"
      :aria-controls="comboboxControls"
      :aria-activedescendant="comboboxActiveOption"
      @input="onInput"
      @keydown="handleKeydown"
    ></textarea>

    <!-- 底部功能按钮行 -->
    <div class="prompt-toolbar">
      <div class="toolbar-left">
        <!-- 附件上传 -->
        <button class="tool-btn" :title="t('promptInput.uploadAttachment')" :aria-label="t('promptInput.uploadAttachment')" @click="triggerFileInput" :disabled="disabled || uploadState === 'uploading'">
          <SumeruIcon name="paperclip" :size="15" variant="duo" tone="edit" interactive />
        </button>
        <input
          ref="fileInputRef"
          type="file"
          accept="image/*,.pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.txt,.md"
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
          :aria-label="t('promptInput.searchWeb')"
          @click="toggleSearch"
          :disabled="disabled"
        >
          <SumeruIcon name="flow" :size="15" variant="duo" interactive />
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
          :aria-label="t('promptInput.deepThink')"
          @click="toggleThink"
          :disabled="disabled"
        >
          <SumeruIcon name="models" :size="15" variant="duo" tone="magic" interactive />
            <transition name="label-fade">
              <span v-if="showThink" class="mode-label think-label">Think</span>
            </transition>
          </button>

          <!-- 工作目录选择器：紧挨深度思考按钮，授权 Agent 读写指定目录 -->
          <WorkingDirSelector />
        </div>

      <div class="toolbar-right">
        <!-- 语音按钮（无内容时显示） -->
        <button
          v-if="!hasSendableContent && !isLoading"
          class="tool-btn ghost"
          :class="{ 'is-transcribing': isTranscribing }"
          :title="t('promptInput.voiceInput')"
          :aria-label="t('promptInput.voiceInput')"
          @click="toggleRecording"
          :disabled="disabled || isTranscribing"
          :loading="isTranscribing"
        >
          <span v-if="isTranscribing" class="transcribing-spinner"></span>
          <span v-else>🎤</span>
        </button>

        <!-- 发送按钮（有内容时显示） -->
        <button
          v-if="hasSendableContent && !isLoading"
          class="send-btn dendro-btn"
          @click="handleSend"
          :disabled="!canSend"
          :title="t('promptInput.send')"
          :aria-label="t('promptInput.send')"
        >
          ↑
        </button>

        <!-- 停止按钮（isLoading 时显示） -->
        <button
          v-if="isLoading"
          class="stop-btn"
          @click="emit('abort')"
          :title="t('promptInput.abort')"
          :aria-label="t('promptInput.abort')"
        >
          <SumeruIcon name="stop" :size="15" variant="duo" tone="del" interactive />
        </button>
      </div>
    </div>

    <div v-if="statusText" class="prompt-status" role="status" aria-live="polite">
      <span>{{ statusText }}</span>
      <button v-if="!connected" class="reconnect-btn" @click="retryConnection" :aria-label="t('promptInput.reconnect')">
        {{ t('promptInput.reconnect') }}
      </button>
    </div>

    <ImageLightbox :show="showLightbox" :src="imagePreviewUrl || uploadedImage?.url || ''" @close="closeLightbox" />
  </div>
</template>

<style scoped>
.prompt-input {
  width: 100%;
  box-sizing: border-box;
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
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

/* Textarea */
.prompt-textarea {
  box-sizing: border-box;
  background: transparent;
  border: none;
  outline: none;
  resize: none;
  color: var(--moon);
  font-size: 14px;
  line-height: 1.5;
  width: 100%;
  min-width: 0;
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
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 4px 8px;
  min-width: 0;
  margin-top: 2px;
}

.toolbar-left {
  display: flex;
  flex: 1 1 auto;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px;
  min-width: 0;
}

.toolbar-right {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 4px;
  margin-left: auto;
}

.tool-btn {
  min-width: 28px;
  height: 28px;
  box-sizing: border-box;
  background: none;
  border: 1px solid transparent;
  cursor: pointer;
  color: var(--moon-dim);
  font-size: 15px;
  padding: 0 6px;
  border-radius: 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  flex-shrink: 0;
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
  border-color: var(--dendro);
}

.tool-btn.think-active {
  background: rgba(167, 139, 250, 0.15);
  color: #a78bfa;
  border-color: #a78bfa;
}

.mode-label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0;
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
  transform: scale(1.08);
  box-shadow: 0 0 12px rgba(248, 113, 113, 0.5);
}

.prompt-status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
  min-width: 0;
  padding-top: 2px;
  border-top: 1px solid rgba(127, 214, 80, 0.1);
  color: var(--moon-dim);
  font-size: 11px;
  line-height: 1.4;
}

.prompt-status > span {
  flex: 1 1 180px;
  min-width: 0;
  overflow-wrap: anywhere;
}

.reconnect-btn {
  min-height: 28px;
  flex: 0 0 auto;
  padding: 3px 8px;
  border: 1px solid rgba(127, 214, 80, 0.3);
  border-radius: 6px;
  background: rgba(127, 214, 80, 0.08);
  color: var(--dendro);
  font: inherit;
  cursor: pointer;
  transition: background 0.2s, border-color 0.2s;
}

.reconnect-btn:hover {
  border-color: var(--dendro);
  background: rgba(127, 214, 80, 0.14);
}

.tool-btn:focus-visible,
.send-btn:focus-visible,
.stop-btn:focus-visible,
.reconnect-btn:focus-visible {
  outline: 2px solid var(--dendro);
  outline-offset: 2px;
}

@media (max-width: 600px) {
  .prompt-input {
    padding: 8px;
  }

  .prompt-toolbar {
    align-items: flex-end;
    gap: 4px;
  }

  .toolbar-left {
    flex-basis: calc(100% - 40px);
    gap: 1px;
  }

  .toolbar-left :deep(.ws-tool-group) {
    gap: 1px;
  }

  .tool-divider,
  .toolbar-left :deep(.tool-divider) {
    margin-inline: 1px;
  }

  .tool-btn,
  .toolbar-left :deep(.ws-btn) {
    min-width: 28px;
    height: 28px;
    padding-inline: 5px;
    border-radius: 6px;
  }

  .mode-label {
    font-size: 10px;
  }

  .send-btn,
  .stop-btn {
    width: 30px;
    height: 30px;
  }

  .prompt-status {
    align-items: flex-start;
  }

  .prompt-status > span {
    flex-basis: min(220px, 100%);
  }
}

@media (max-width: 360px) {
  .prompt-input {
    padding-inline: 6px;
  }

  .tool-divider,
  .toolbar-left :deep(.tool-divider) {
    display: none;
  }
}
</style>
