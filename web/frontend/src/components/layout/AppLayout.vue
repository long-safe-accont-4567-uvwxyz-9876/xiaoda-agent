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
/* 页面间 3D 叶片翻转转场（须全局：transition 类挂在子组件根元素上） */
.leaf-flip-enter-active,
.leaf-flip-leave-active {
  transition: transform 0.32s var(--ease-smooth), opacity 0.32s var(--ease-smooth);
  transform-style: preserve-3d;
}
.leaf-flip-enter-from {
  opacity: 0;
  transform: perspective(1200px) rotateY(10deg) translateX(36px) scale(0.985);
}
.leaf-flip-leave-to {
  opacity: 0;
  transform: perspective(1200px) rotateY(-10deg) translateX(-36px) scale(0.985);
}

@media (prefers-reduced-motion: reduce) {
  .leaf-flip-enter-from, .leaf-flip-leave-to { transform: none; }
}
</style>