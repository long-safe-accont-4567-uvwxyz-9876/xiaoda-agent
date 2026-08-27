<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import SideBar from './SideBar.vue'
import TopBar from './TopBar.vue'
import AgentBackdrop from './AgentBackdrop.vue'
import { useAuthStore } from '../../stores/auth'
import { useAgentsStore } from '../../stores/agents'
import { useUiStore } from '../../stores/ui'
import { getWsClient } from '../../api/ws'
import { useRouter, useRoute } from 'vue-router'

const auth = useAuthStore()
const agentsStore = useAgentsStore()
const ui = useUiStore()
const router = useRouter()
const route = useRoute()
const desktopSidebarExpanded = ref(false)
const mobileSidebarOpen = ref(false)

function closeMobileSidebar() {
  mobileSidebarOpen.value = false
}

function onShellKeydown(event: KeyboardEvent) {
  if (mobileSidebarOpen.value && event.key === 'Escape') {
    event.preventDefault()
    closeMobileSidebar()
  }
}

watch(() => route.fullPath, closeMobileSidebar)

onMounted(() => {
  window.addEventListener('keydown', onShellKeydown)
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', onShellKeydown)
})

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
    <div
      v-if="mobileSidebarOpen"
      class="mobile-overlay"
      @click="closeMobileSidebar"
    ></div>
    <SideBar
      :expanded="desktopSidebarExpanded"
      :mobile-open="mobileSidebarOpen"
      @update:expanded="desktopSidebarExpanded = $event"
      @close="closeMobileSidebar"
    />
    <div class="main-area">
      <TopBar
        :mobile-sidebar-open="mobileSidebarOpen"
        @toggle-sidebar="mobileSidebarOpen = !mobileSidebarOpen"
      />
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
  position: relative;
  display: flex;
  width: 100vw;
  height: 100dvh;
  overflow: hidden;
  isolation: isolate;
  background: var(--forest-deep);
}

.app-layout::after {
  content: '';
  position: fixed;
  inset: 0;
  z-index: 0;
  background-image:
    linear-gradient(rgba(145, 232, 102, 0.018) 1px, transparent 1px),
    linear-gradient(90deg, rgba(145, 232, 102, 0.018) 1px, transparent 1px);
  background-size: 32px 32px;
  mask-image: linear-gradient(to bottom, rgba(0, 0, 0, 0.46), transparent 72%);
  pointer-events: none;
}

.mobile-overlay {
  position: fixed;
  inset: 0;
  display: none;
  z-index: var(--z-overlay);
  background: rgba(2, 8, 5, 0.68);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
}

.main-area {
  position: relative;
  z-index: 1;
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
  overflow: hidden;
}

.content {
  position: relative;
  z-index: 2;
  flex: 1;
  overflow: auto;
  padding: 22px clamp(16px, 2.2vw, 32px) 32px;
  scroll-padding-top: 20px;
  /* 只留 layout：paint 会把容器变成后代绘制裁剪边界，
     Tilt3D 卡片贴边倾斜时溢出部分被裁平（看起来像被遮挡）；
     溢出剪裁已由 overflow:auto 保证，无需 paint */
  contain: layout;
}

.content > :deep(*) {
  width: min(100%, var(--content-max-width));
  margin-inline: auto;
}

@media (max-width: 768px) {
  .content {
    padding: 12px 10px 20px;
  }

  .mobile-overlay {
    display: block;
  }
}
</style>

<style>
/* Fast page transition with no overlapping route surfaces. */
.leaf-flip-enter-active {
  transition: opacity 0.14s var(--ease-smooth), transform 0.14s var(--ease-out);
}

.leaf-flip-leave-active {
  transition: opacity 0.1s var(--ease-smooth), transform 0.1s var(--ease-out);
  pointer-events: none;
}

.leaf-flip-enter-from {
  opacity: 0;
  transform: translateY(5px);
}

.leaf-flip-leave-to {
  opacity: 0;
  transform: translateY(-3px);
}

@media (prefers-reduced-motion: no-preference) {
  .content { scroll-behavior: smooth; }
}

@media (prefers-reduced-motion: reduce) {
  .leaf-flip-enter-active,
  .leaf-flip-leave-active {
    transition: none;
  }

  .leaf-flip-enter-from,
  .leaf-flip-leave-to {
    transform: none;
  }
}
</style>
