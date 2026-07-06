<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useAgentsStore } from '../../stores/agents'

const DEFAULT_BG = '/assets/webui_background.jpg'

const chat = useChatStore()
const agentsStore = useAgentsStore()

const targetUrl = computed(() => {
  const a = agentsStore.agents.find(x => x.name === chat.currentAgent)
  return a?.wallpaper || DEFAULT_BG
})

interface Layer { url: string; key: number }
const layers = ref<Layer[]>([{ url: DEFAULT_BG, key: 0 }])
let seq = 0
let pendingUrl = ''

watch(targetUrl, (url) => {
  pendingUrl = url
  if (topUrl() === url) return
  // 预加载完成再入场，避免切换瞬间露底闪黑
  const img = new Image()
  img.onload = () => { if (pendingUrl === url) pushLayer(url) }
  img.onerror = () => { if (pendingUrl === url) pushLayer(DEFAULT_BG) }
  img.src = url
}, { immediate: true })

function topUrl() {
  return layers.value[layers.value.length - 1]?.url
}

function pushLayer(url: string) {
  if (topUrl() === url) return
  layers.value.push({ url, key: ++seq })
  // 交叉淡化结束后丢弃被盖住的旧层
  setTimeout(() => {
    if (layers.value.length > 1) layers.value.splice(0, layers.value.length - 1)
  }, 1400)
}
</script>

<template>
  <div class="agent-backdrop" aria-hidden="true">
    <transition-group name="bg-fade">
      <div
        v-for="l in layers"
        :key="l.key"
        class="backdrop-layer"
        :style="{ backgroundImage: `url('${l.url}')` }"
      />
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

/* 新背景在旧背景之上淡入并缓缓沉降，旧层被盖住后静默移除 */
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
}

@media (prefers-reduced-motion: reduce) {
  .bg-fade-enter-from { transform: none; }
}
</style>