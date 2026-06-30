<script setup lang="ts">
import { onMounted, ref, provide, onUnmounted } from 'vue'
import { NConfigProvider, NMessageProvider, NDialogProvider, darkTheme } from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'
import { useAuthStore } from './stores/auth'
import { useRouter } from 'vue-router'
import { api } from './api'
import { t } from './i18n'
import GrassParticles from './components/fx/GrassParticles.vue'

const auth = useAuthStore()
const router = useRouter()
const particlesRef = ref<InstanceType<typeof GrassParticles> | null>(null)

provide('particles', particlesRef)

// 署名水印防删除
const watermarkRef = ref<HTMLElement | null>(null)
let observer: MutationObserver | null = null
let signatureCheckTimer: number | null = null

function setupWatermarkGuard() {
  if (!watermarkRef.value) return
  observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      for (const node of m.removedNodes) {
        if (node === watermarkRef.value || (node as HTMLElement).classList?.contains('brand-watermark')) {
          requestAnimationFrame(() => {
            if (watermarkRef.value && !document.body.contains(watermarkRef.value)) {
              document.body.appendChild(watermarkRef.value)
            }
          })
        }
      }
    }
  })
  observer.observe(document.body, { childList: true, subtree: true })

  signatureCheckTimer = window.setInterval(async () => {
    try {
      const data = await api.getBrandSignature()
      const expected = data.signature || ''
      const watermarks = document.querySelectorAll('.brand-watermark span')
      watermarks.forEach(el => {
        if (el.textContent !== expected && expected) {
          el.textContent = expected
        }
      })
    } catch { /* 静默失败 */ }
  }, 60000)
}

onMounted(async () => {
  // 1. 首次运行检测：API Key 未配置 → 跳转 setup 向导
  //    API Key 已配置但用户资料未完成 → 跳转资料编辑页
  try {
    const data = await api.getSetupFirstRun()
    if (data?.first_run) {
      router.replace('/setup')
      return
    }
    // API Key 已配置，检查用户资料是否完成（localStorage 缓存优先）
    if (!data?.profile_done && !localStorage.getItem('nahida_profile_done')) {
      // 需要先登录才能访问需要认证的 /setup/profile
      if (!auth.isLoggedIn) {
        router.replace('/login')
      } else {
        router.replace('/setup/profile')
      }
      return
    }
  } catch {
    // 检测失败，继续正常流程
  }
  // 2. 非首次运行：未登录则跳转登录页（已登录的直接进主界面）
  if (!auth.isLoggedIn) {
    router.replace('/login')
  }
  // 3. 已登录：路由守卫会放行，无需额外跳转

  // 启动署名水印防删除守护
  setupWatermarkGuard()
})

onUnmounted(() => {
  observer?.disconnect()
  if (signatureCheckTimer) clearInterval(signatureCheckTimer)
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
    <div ref="watermarkRef" class="brand-watermark" aria-hidden="true">
      <span>{{ t('brand_signature.full') }}</span>
    </div>
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
::-webkit-scrollbar-thumb { background: rgba(143, 229, 96, 0.3); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: rgba(143, 229, 96, 0.5); }

/* 全局署名水印（非 scoped） */
.brand-watermark {
  position: fixed;
  bottom: 8px;
  right: 12px;
  z-index: 9999;
  pointer-events: none;
  user-select: none;
  opacity: 0.18;
  font-size: 11px;
  color: var(--wisdom, #e8d5a3);
  font-family: 'Noto Serif SC', serif;
  letter-spacing: 1px;
  text-shadow: 0 0 4px rgba(0,0,0,0.5);
  writing-mode: vertical-rl;
  max-height: 60vh;
}
.brand-watermark span {
  display: inline-block;
}
</style>
