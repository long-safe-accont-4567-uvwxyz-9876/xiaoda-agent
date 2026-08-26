<script setup lang="ts">
/**
 * Agent 头像媒体：壁纸 → 可渲染头像
 *
 * - 静态图/动图（gif/webp）→ <img>
 * - 视频壁纸 → 优先后端 ffmpeg 抽好的首帧海报（wallpaper_poster）；
 *   无海报时用 <video #t=0.001> 定位到首帧展示（preload=metadata，不播放）
 * - HTML 壁纸无法作头像 → 挂载即报 error，由父级回退文字首字
 * - 任何加载失败 → emit('error')，父级决定回退方式
 */
import { computed, onMounted, ref, watch } from 'vue'
import { wallpaperKind } from '../../utils/wallpaper'

const props = defineProps<{ wallpaper?: string; poster?: string }>()
const emit = defineEmits<{ error: [] }>()

// 海报加载失败时降级为 <video> 原生首帧
const posterFailed = ref(false)

watch(() => [props.wallpaper, props.poster], () => { posterFailed.value = false })

const mode = computed(() => {
  const kind = wallpaperKind(props.wallpaper)
  if (kind === 'html' || !props.wallpaper) return 'none'
  if (kind === 'image') return 'img'
  if (props.poster && !posterFailed.value) return 'poster'
  return 'video'
})

onMounted(() => {
  if (mode.value === 'none' && props.wallpaper) emit('error')
})

function onImgError() {
  if (mode.value === 'poster') posterFailed.value = true
  else emit('error')
}

// 媒体分片定位首帧；部分浏览器需从事件确认 seek 完成后才绘帧，#t=0.001 已覆盖主流内核
const videoSrc = computed(() => `${props.wallpaper}#t=0.001`)
</script>

<template>
  <video v-if="mode === 'video'"
         class="avatar-media"
         :src="videoSrc"
         muted
         playsinline
         preload="metadata"
         @error="emit('error')"
  />
  <img v-else-if="mode === 'poster'" class="avatar-media" :src="props.poster" alt="" @error="onImgError" />
  <img v-else-if="mode === 'img'" class="avatar-media" :src="props.wallpaper" alt="" @error="onImgError" />
</template>

<style scoped>
.avatar-media {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
</style>
