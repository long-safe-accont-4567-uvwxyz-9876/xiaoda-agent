<script setup lang="ts">
/**
 * 壁纸字段：手输 URL + 上传 + 预览（图/GIF/视频/HTML/外域拦截占位）。
 * 上传成功后回写 v-model:wallpaper 并刷新 agents store（与原视图行为一致）。
 */
import { ref } from 'vue'
import { NButton, NInput, useMessage } from 'naive-ui'
import { post } from '../../api'
import { useAgentsStore } from '../../stores/agents'
import { t } from '../../i18n'

const props = defineProps<{
  /** 目标 agent 名；创建模式下无名称、隐藏上传按钮 */
  agentName: string
  isCreate: boolean
}>()

const wallpaper = defineModel<string>('wallpaper', { default: '' })

const message = useMessage()
const agentsStore = useAgentsStore()
const wpInput = ref<HTMLInputElement | null>(null)
const uploadingWp = ref(false)

function isVideoWallpaper(url: string): boolean {
  return /\.(mp4|webm)(\?|$)/i.test(url)
}

// 与 AgentBackdrop.sanitizeUrl 同规则：仅本站媒体路径可直接渲染，
// 手输的外域 URL 在预览中显示占位而非直接加载
function sanitizeWallpaperUrl(url: string): string | null {
  if (url.startsWith('/media/wallpapers/') || url.startsWith('/media/agents/')) return url
  if (url.startsWith('data:image/')) return url
  return null
}

const WP_LIMITS: Record<string, number> = {
  image: 8 * 1024 * 1024,
  gif: 20 * 1024 * 1024,
  video: 50 * 1024 * 1024,
}

function wallpaperKind(file: File): 'image' | 'gif' | 'video' | null {
  if (file.type === 'image/gif') return 'gif'
  if (file.type === 'video/mp4' || file.type === 'video/webm') return 'video'
  if (file.type.startsWith('image/')) return 'image'
  return null
}

function pickWallpaper(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const kind = wallpaperKind(file)
  if (!kind) {
    message.error(t('agentsView.wpUnsupported'))
    input.value = ''
    return
  }
  if (file.size > WP_LIMITS[kind]) {
    const key = kind === 'image' ? 'agentsView.imgTooLarge'
      : kind === 'gif' ? 'agentsView.gifTooLarge' : 'agentsView.videoTooLarge'
    message.error(t(key))
    input.value = ''
    return
  }
  const reader = new FileReader()
  reader.onload = async () => {
    uploadingWp.value = true
    try {
      const r = await post<any>(`/agents/${props.agentName}/wallpaper`,
        { data_url: reader.result })
      wallpaper.value = r.wallpaper
      message.success(t('agentsView.wallpaperUpdated'))
      await agentsStore.load()
    } catch (err: any) {
      message.error(err.message)
    } finally {
      uploadingWp.value = false
      input.value = ''
    }
  }
  reader.readAsDataURL(file)
}
</script>

<template>
  <div class="wallpaper-field">
    <div class="wallpaper-row">
      <n-input v-model:value="wallpaper"
               :placeholder="t('agentsView.wallpaperPh')" />
      <n-button v-if="!isCreate" :loading="uploadingWp" @click="wpInput?.click()">
        {{ t('agentsView.uploadImage') }}
      </n-button>
      <input ref="wpInput" type="file"
             accept="image/png,image/jpeg,image/webp,image/gif,video/mp4,video/webm"
             style="display: none" @change="pickWallpaper" />
    </div>
    <template v-if="wallpaper">
      <template v-if="sanitizeWallpaperUrl(wallpaper)">
        <video v-if="isVideoWallpaper(wallpaper)"
               class="wallpaper-preview" :src="wallpaper"
               autoplay loop muted playsinline />
        <iframe v-else-if="/\.html?(\?|$)/i.test(wallpaper)"
                class="wallpaper-preview" :src="wallpaper"
                sandbox="allow-scripts" referrerpolicy="no-referrer"
                title="wallpaper preview" />
        <div v-else class="wallpaper-preview"
             :style="{ backgroundImage: `url('${wallpaper}')` }" />
      </template>
      <div v-else class="wallpaper-preview wallpaper-preview-blocked">
        {{ t('agentsView.wpExternalBlocked') }}
      </div>
    </template>
    <span v-else class="wallpaper-hint">{{ t('agentsView.wallpaperHint') }}</span>
  </div>
</template>

<style scoped>
.wallpaper-field { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.wallpaper-row { display: flex; gap: 8px; }
.wallpaper-preview {
  height: 90px;
  border-radius: 10px;
  background: center/cover no-repeat;
  border: 1px solid var(--glass-border);
}
.wallpaper-hint { font-size: 12px; color: var(--moon-dim); }
</style>
