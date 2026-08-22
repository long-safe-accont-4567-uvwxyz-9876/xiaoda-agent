<script setup lang="ts">
import { computed, ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useAgentsStore } from '../../stores/agents'

const DEFAULT_BG = '/media/wallpapers/webui_background.jpg'

const chat = useChatStore()
const agentsStore = useAgentsStore()

const targetUrl = computed(() => {
  if (agentsStore.agents.length) {
    const a = agentsStore.agents.find(x => x.name === chat.currentAgent)
    if (a?.wallpaper) return a.wallpaper
  }
  return agentsStore.mainWallpaper || DEFAULT_BG
})

type LayerKind = 'image' | 'video' | 'html'
interface Layer { url: string; key: number; kind: LayerKind }
const layers = ref<Layer[]>([])
let seq = 0
let pendingUrl = ''
let pruneTimer: ReturnType<typeof setTimeout> | null = null

onMounted(() => {
  const initial = agentsStore.mainWallpaper || DEFAULT_BG
  pushLayer(initial)
})

onBeforeUnmount(() => {
  if (pruneTimer) { clearTimeout(pruneTimer); pruneTimer = null }
})

function layerKind(url: string): LayerKind {
  if (/\.(mp4|webm)(\?|$)/i.test(url)) return 'video'
  if (/\.html?(\?|$)/i.test(url)) return 'html'
  return 'image'
}

watch(targetUrl, (url) => {
  if (!url) return
  pendingUrl = url
  if (topUrl() === url) return
  const kind = layerKind(url)
  if (kind !== 'image') {
    // 视频/HTML 无 Image 预加载探活；错误处理在元素事件上
    // （视频 error→回退默认图；HTML 上传侧已静态校验）
    pushLayer(url)
    return
  }
  const img = new Image()
  img.onload = () => { if (pendingUrl === url) pushLayer(url) }
  img.onerror = () => {
    if (pendingUrl !== url) return
    // 如果失败的 URL 本身就是 DEFAULT_BG，不再重试，直接显示 tint 底色
    if (url === DEFAULT_BG) return
    pushLayer(DEFAULT_BG)
  }
  img.src = url
})

function topUrl() {
  return layers.value[layers.value.length - 1]?.url
}

function sanitizeUrl(url: string): string {
  if (url.startsWith('/media/wallpapers/') || url.startsWith('/media/agents/')) return url
  if (url.startsWith('data:image/')) return url
  return DEFAULT_BG
}

function pushLayer(url: string) {
  url = sanitizeUrl(url)
  if (topUrl() === url) return
  layers.value.push({ url, key: ++seq, kind: layerKind(url) })
  if (pruneTimer) clearTimeout(pruneTimer)
  pruneTimer = setTimeout(() => {
    if (layers.value.length > 1) layers.value.splice(0, layers.value.length - 1)
    pruneTimer = null
  }, 1400)
}

function videoError() {
  // 视频 canplay 前出错（404/解码失败）：回退默认背景，避免黑屏
  const top = topUrl()
  if (top && top !== DEFAULT_BG && layerKind(top) === 'video') pushLayer(DEFAULT_BG)
}
</script>

<template>
  <div class="agent-backdrop" aria-hidden="true">
    <transition-group name="bg-fade">
      <template v-for="l in layers" :key="l.key">
        <video
          v-if="l.kind === 'video'"
          class="backdrop-layer backdrop-video"
          :src="l.url"
          autoplay
          loop
          muted
          playsinline
          preload="auto"
          @error="videoError"
        />
        <iframe
          v-else-if="l.kind === 'html'"
          class="backdrop-layer backdrop-html"
          :src="l.url"
          sandbox="allow-scripts"
          referrerpolicy="no-referrer"
          title="dynamic wallpaper"
        />
        <div
          v-else
          class="backdrop-layer"
          :style="{ backgroundImage: `url('${l.url}')` }"
        />
      </template>
    </transition-group>
    <div class="backdrop-tint"></div>
  </div>
</template>

<style scoped>
.agent-backdrop {
  position: absolute;
  inset: 0;
  z-index: 0;
  overflow: hidden;
  background: var(--forest-deep);
}

.backdrop-layer {
  position: absolute;
  inset: 0;
  background-size: cover;
  background-position: center;
}

/* 视频层与图片层同构图：cover 等效于 object-fit + 全屏尺寸 */
.backdrop-video {
  object-fit: cover;
  width: 100%;
  height: 100%;
}

/* HTML 动画层：全屏、透明背景、不可交互（纯展示） */
.backdrop-html {
  width: 100%;
  height: 100%;
  border: none;
  background: transparent;
  pointer-events: none;
}

.bg-fade-enter-active {
  transition: opacity 1.1s var(--ease-smooth), transform 1.3s var(--ease-smooth);
}
.bg-fade-enter-from {
  opacity: 0;
  transform: scale(1.045);
}
.bg-fade-leave-active {
  transition: none;
}

.backdrop-tint {
  position: absolute;
  inset: 0;
  background: var(--backdrop-tint);
  pointer-events: none;
  opacity: calc(2 - var(--app-brightness, 1.05));
  transition: opacity 0.4s ease;
}

@media (prefers-reduced-motion: reduce) {
  .bg-fade-enter-from { transform: none; }
}
</style>
