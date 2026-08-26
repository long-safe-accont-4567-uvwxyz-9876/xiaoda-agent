import { ref, computed, onBeforeUnmount } from 'vue'
import type { Ref } from 'vue'
import { api } from '../../../api'
import { classifyUpload } from './classifyUpload'

export interface UploadedImage {
  url: string
  name: string
}

export interface UploadedDoc {
  url: string
  name: string
  path: string
  ext: string
}

export function usePromptUploads(statusKey: Ref<string>) {
  const uploadedImage = ref<UploadedImage | null>(null)
  const uploadedDoc = ref<UploadedDoc | null>(null)
  const imagePreviewUrl = ref('')
  const uploadState = ref<'idle' | 'uploading' | 'error'>('idle')
  const showLightbox = ref(false)

  const hasAttachment = computed(() => uploadedImage.value !== null || uploadedDoc.value !== null)

  async function uploadFile(file: File) {
    // P0 修复（Task 1.9）：一键包含所有文件 — 自动检测类型并路由
    // 图片 → vision API（uploadImage），文档 → document_reader 工具（uploadDoc）
    // 用户要求"不要添加组件，一键包含所有文件"
    const cls = classifyUpload(file)
    if (!cls) {
      uploadState.value = 'error'
      statusKey.value = 'promptInput.unsupportedFile'
      return
    }
    uploadState.value = 'uploading'
    statusKey.value = ''
    try {
      if (cls.kind === 'image') {
        if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
        imagePreviewUrl.value = URL.createObjectURL(file)
        const result = await api.uploadImage(file)
        uploadedImage.value = result
      } else {
        // 文档上传：不走 vision API，返回路径供 document_reader 工具使用
        const result = await api.uploadDoc(file)
        uploadedDoc.value = result
      }
      uploadState.value = 'idle'
    } catch {
      if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
      imagePreviewUrl.value = ''
      uploadedImage.value = null
      uploadedDoc.value = null
      uploadState.value = 'error'
      statusKey.value = 'promptInput.uploadFailed'
    }
  }

  function removeImage() {
    if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
    uploadedImage.value = null
    imagePreviewUrl.value = ''
  }

  // P0 新增（Task 1.9）：文档附件移除 — 与图片附件独立的清理路径
  function removeDoc() {
    uploadedDoc.value = null
  }

  function openLightbox() {
    showLightbox.value = true
  }

  function closeLightbox() {
    showLightbox.value = false
  }

  function resetAttachments() {
    if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
    uploadedImage.value = null
    uploadedDoc.value = null
    imagePreviewUrl.value = ''
  }

  onBeforeUnmount(() => {
    if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
  })

  return {
    uploadedImage,
    uploadedDoc,
    imagePreviewUrl,
    uploadState,
    showLightbox,
    hasAttachment,
    uploadFile,
    removeImage,
    removeDoc,
    openLightbox,
    closeLightbox,
    resetAttachments,
  }
}
