<script setup lang="ts">
import { ref, onMounted } from 'vue'
import SideBar from './SideBar.vue'
import TopBar from './TopBar.vue'
import AgentBackdrop from './AgentBackdrop.vue'
import { useAuthStore } from '../../stores/auth'
import { useAgentsStore } from '../../stores/agents'
import { useUiStore } from '../../stores/ui'
import { getWsClient } from '../../api/ws'
import { useRouter } from 'vue-router'

const auth = useAuthStore()
const agentsStore = useAgentsStore()
const ui = useUiStore()
const router = useRouter()
const sidebarExpanded = ref(false)

if (!auth.isLoggedIn) {
  router.replace('/login')
} else {
  onMounted(() => {
    const ws = getWsClient()
    if (!ws.connected && auth.token) {
      ws.connect(auth.token)
    }
    agentsStore.load().catch(() => {})
    ui.loadRemote()
  })
}
</script>

<template>
  <div class="app-layout">
    <AgentBackdrop />
    <SideBar :expanded="sidebarExpanded" @update:expanded="sidebarExpanded = $event" />
    <div class="main-area">
      <TopBar />
      <main class="content">
        <router-view v-slot="{ Component }">
          <transition name="leaf-flip" mode="out-in">
            <keep-alive include="ChatView">
              <component :is="Component" />
            </keep-alive>
          </transition>
        </router-view>
      </main>
    </div>
  </div>
</template>

<style scoped>
.app-layout {
  display: flex;
  height: 100vh;
  width: 100vw;
  overflow: hidden;
  position: relative;
}

.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
  position: relative;
  z-index: 1;
}

.content {
  flex: 1;
  overflow: auto;
  padding: 16px;
  position: relative;
  z-index: 2;
  contain: layout paint;
}

@media (max-width: 768px) {
  .content { padding: 8px; }
}
</style>

<style>
/* 页面间 3D 叶片翻转转场 · v2（须全局：transition 类挂在子组件根元素上） */
.leaf-flip-enter-active {
  transition:
    transform 0.42s var(--ease-spring, cubic-bezier(0.22, 1.4, 0.36, 1)),
    opacity 0.28s var(--ease-smooth),
    filter 0.36s var(--ease-smooth);
  transform-style: preserve-3d;
  will-change: transform, opacity, filter;
}
.leaf-flip-leave-active {
  transition:
    transform 0.2s cubic-bezier(0.5, 0, 0.75, 0),
    opacity 0.18s var(--ease-smooth),
    filter 0.18s var(--ease-smooth);
  transform-style: preserve-3d;
  will-change: transform, opacity, filter;
}
.leaf-flip-enter-from {
  opacity: 0;
  transform: perspective(1200px) rotateY(8deg) translateX(30px) scale(0.982);
  filter: blur(6px);
}
.leaf-flip-leave-to {
  opacity: 0;
  transform: perspective(1200px) rotateY(-6deg) translateX(-24px) scale(0.99);
  filter: blur(3px);
}

/* 级联入场：新页面的直接子元素依次浮现（新叶抽枝） */
.leaf-flip-enter-active > * {
  animation: leaf-item-in 0.5s var(--ease-spring, cubic-bezier(0.22, 1.4, 0.36, 1)) backwards;
}
.leaf-flip-enter-active > *:nth-child(1) { animation-delay: 0.04s; }
.leaf-flip-enter-active > *:nth-child(2) { animation-delay: 0.1s; }
.leaf-flip-enter-active > *:nth-child(3) { animation-delay: 0.16s; }
.leaf-flip-enter-active > *:nth-child(4) { animation-delay: 0.22s; }
.leaf-flip-enter-active > *:nth-child(5) { animation-delay: 0.28s; }
.leaf-flip-enter-active > *:nth-child(n+6) { animation-delay: 0.34s; }
@keyframes leaf-item-in {
  from { opacity: 0; transform: translateY(14px) scale(0.99); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}

/* 内容区滚动丝滑 */
@media (prefers-reduced-motion: no-preference) {
  .content { scroll-behavior: smooth; }
}

@media (prefers-reduced-motion: reduce) {
  .leaf-flip-enter-from, .leaf-flip-leave-to { transform: none; filter: none; }
  .leaf-flip-enter-active > * { animation: none; }
}
body.low-gpu .leaf-flip-enter-from,
body.low-gpu .leaf-flip-leave-to { filter: none; }
</style>