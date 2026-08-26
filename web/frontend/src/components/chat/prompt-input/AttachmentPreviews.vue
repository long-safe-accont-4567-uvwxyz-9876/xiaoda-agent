<script setup lang="ts">
import { t } from '../../../i18n'
import type { UploadedDoc, UploadedImage } from './usePromptUploads'

defineProps<{
  image: UploadedImage | null
  previewUrl: string
  doc: UploadedDoc | null
}>()

const emit = defineEmits<{
  'remove-image': []
  'open-lightbox': []
  'remove-doc': []
}>()
</script>

<template>
  <!-- 图片预览区 -->
  <transition name="preview-slide">
    <div v-if="image || previewUrl" class="image-preview-area">
      <div class="image-thumb" @click="emit('open-lightbox')">
        <img :src="previewUrl || image?.url" :alt="t('chatView.preview')" />
      </div>
      <button class="image-remove" @click="emit('remove-image')" :title="t('promptInput.removeImage')" :aria-label="t('promptInput.removeImage')">✕</button>
    </div>
  </transition>

  <!-- P0 新增（Task 1.9）：文档附件预览 — 显示文件名+扩展名 chip，可移除 -->
  <transition name="preview-slide">
    <div v-if="doc" class="doc-preview-area">
      <div class="doc-chip">
        <span class="doc-icon">📄</span>
        <span class="doc-name">{{ doc.name }}</span>
        <span class="doc-ext">{{ doc.ext }}</span>
        <button class="doc-remove-btn" @click="emit('remove-doc')" :title="t('promptInput.removeDocument')" :aria-label="t('promptInput.removeDocument')">✕</button>
      </div>
    </div>
  </transition>
</template>

<style scoped>
/* 图片预览区 */
.image-preview-area {
  display: flex;
  align-items: center;
  gap: 8px;
  position: relative;
  padding: 4px 0;
}

/* P0 新增（Task 1.9）：文档附件预览区 — 复用 image-preview-area 布局 */
.doc-preview-area {
  display: flex;
  align-items: center;
  gap: 8px;
  position: relative;
  padding: 4px 0;
}

.doc-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 10px;
  background: var(--glass-bg, rgba(255, 255, 255, 0.08));
  border: 1px solid var(--glass-border, rgba(255, 255, 255, 0.12));
  font-size: 13px;
  max-width: 280px;
}

.doc-icon {
  font-size: 16px;
  flex-shrink: 0;
}

.doc-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex-shrink: 1;
  min-width: 0;
}

.doc-ext {
  font-size: 11px;
  opacity: 0.7;
  text-transform: uppercase;
  flex-shrink: 0;
  padding: 1px 5px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.1);
}

.doc-remove-btn {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: rgba(217, 106, 95, 0.9);
  color: #fff;
  border: none;
  cursor: pointer;
  font-size: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-left: 4px;
  transition: transform 0.15s;
}

.doc-remove-btn:hover {
  transform: scale(1.15);
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
</style>
