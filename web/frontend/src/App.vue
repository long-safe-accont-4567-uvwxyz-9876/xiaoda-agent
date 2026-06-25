<script setup lang="ts">
import { onMounted, ref, provide } from 'vue'
import { NConfigProvider, NMessageProvider, NDialogProvider, darkTheme } from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'
import { useAuthStore } from './stores/auth'
import { useRouter } from 'vue-router'
import { api } from './api'
import GrassParticles from './components/fx/GrassParticles.vue'

const auth = useAuthStore()
const router = useRouter()
const particlesRef = ref<InstanceType<typeof GrassParticles> | null>(null)

provide('particles', particlesRef)

onMounted(async () => {
  // 首次运行检测：如果 API Key 未配置，跳转到引导页面
  try {
    const data = await api.getSetupFirstRun()
    if (data?.first_run) {
      router.replace('/setup')
      return
    }
  } catch {
    // 检测失败，继续正常流程
  }
  // 正常流程：未登录则跳转登录页
  if (!auth.isLoggedIn) {
    router.replace('/login')
  }
})

const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#8fe560',
    primaryColorHover: '#a2f070',
    primaryColorPressed: '#6bc840',
    primaryColorSuppl: '#8fe560',
    bodyColor: 'transparent',
    cardColor: 'rgba(20, 40, 28, 0.45)',
    modalColor: 'rgba(20, 40, 28, 0.92)',
    popoverColor: 'rgba(18, 36, 26, 0.96)',
    tableColor: 'transparent',
    inputColor: 'rgba(15, 31, 23, 0.5)',
    borderColor: 'rgba(143, 229, 96, 0.18)',
    successColor: '#8fe560',
    errorColor: '#d96a5f',
    warningColor: '#e8d5a3',
  },
}
</script>

<template>
  <n-config-provider :theme="darkTheme" :theme-overrides="themeOverrides">
    <n-dialog-provider>
      <n-message-provider placement="top-right">
        <GrassParticles ref="particlesRef" />
        <router-view v-slot="{ Component }">
          <transition name="leaf-page" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </n-message-provider>
    </n-dialog-provider>
  </n-config-provider>
</template>

<style>
@import './styles/theme.css';
@import './styles/sumeru-tokens.css';

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html, body, #app {
  height: 100%;
  width: 100%;
  overflow: hidden;
}

/* 亮度调节：通过 CSS filter 全局应用 */
#app {
  filter: brightness(var(--app-brightness, 1.05));
  transition: filter 0.4s ease;
}

body {
  font-family: 'Noto Sans SC', system-ui, -apple-system, sans-serif;
  color: var(--moon);
  background: var(--forest-deep);
}

/* 叶片翻页转场 */
.leaf-page-enter-active,
.leaf-page-leave-active {
  transition: transform 0.35s var(--ease-smooth), opacity 0.35s var(--ease-smooth);
  transform-style: preserve-3d;
}
.leaf-page-enter-from {
  opacity: 0;
  transform: perspective(1200px) rotateY(12deg) translateX(40px);
}
.leaf-page-leave-to {
  opacity: 0;
  transform: perspective(1200px) rotateY(-12deg) translateX(-40px);
}

@media (prefers-reduced-motion: reduce) {
  .leaf-page-enter-from, .leaf-page-leave-to {
    transform: none;
  }
}

::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: rgba(127, 214, 80, 0.3); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(127, 214, 80, 0.5); }
</style>
