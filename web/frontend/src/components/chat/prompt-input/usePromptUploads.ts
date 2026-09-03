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
  let uploadGeneration = 0
  let imagePreviewGeneration: number | null = null

  const hasAttachment = computed(() => uploadedImage.value !== null || uploadedDoc.value !== null)

  function clearImagePreview() {
    if (imagePreviewUrl.value) URL.revokeObjectURL(imagePreviewUrl.value)
    imagePreviewUrl.value = ''
    imagePreviewGeneration = null
  }

  function invalidateUpload() {
    uploadGeneration += 1
    if (imagePreviewGeneration !== null) clearImagePreview()
    if (uploadState.value === 'uploading') uploadState.value = 'idle'
  }

  function nextUploadGeneration() {
    invalidateUpload()
    return uploadGeneration
  }

  async function uploadFile(file: File) {
    const generation = nextUploadGeneration()
    // P0 修复（Task 1.9）：一键包含所有文件 — 自动检测类型并路由
    // 图片 → vision API（uploadImage），文档 → document_reader 工具（uploadDoc）
    // 用户要求"不要添加组件，一键包含所有文件"
    const cls = classifyUpload(file)
    if (!cls) {
      if (generation !== uploadGeneration) return
      uploadState.value = 'error'
      statusKey.value = 'promptInput.unsupportedFile'
      return
    }
    uploadState.value = 'uploading'
    statusKey.value = ''
    try {
      if (cls.kind === 'image') {
        clearImagePreview()
        imagePreviewUrl.value = URL.createObjectURL(file)
        imagePreviewGeneration = generation
        const result = await api.uploadImage(file)
        if (generation !== uploadGeneration) return
        uploadedImage.value = result
        imagePreviewGeneration = null
      } else {
        // 文档上传：不走 vision API，返回路径供 document_reader 工具使用
        const result = await api.uploadDoc(file)
        if (generation !== uploadGeneration) return
        uploadedDoc.value = result
      }
      uploadState.value = 'idle'
    } catch {
      if (generation !== uploadGeneration) return
      if (imagePreviewGeneration === generation) clearImagePreview()
      uploadedImage.value = null
      uploadedDoc.value = null
      uploadState.value = 'error'
      statusKey.value = 'promptInput.uploadFailed'
    }
  }

  function removeImage() {
    invalidateUpload()
    clearImagePreview()
    uploadedImage.value = null
  }

  // P0 新增（Task 1.9）：文档附件移除 — 与图片附件独立的清理路径
  function removeDoc() {
    invalidateUpload()
    uploadedDoc.value = null
  }

  function openLightbox() {
    showLightbox.value = true
  }

  function closeLightbox() {
    showLightbox.value = false
  }

  function resetAttachments() {
    invalidateUpload()
    clearImagePreview()
    uploadedImage.value = null
    uploadedDoc.value = null
  }

  onBeforeUnmount(() => {
    invalidateUpload()
    clearImagePreview()
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
