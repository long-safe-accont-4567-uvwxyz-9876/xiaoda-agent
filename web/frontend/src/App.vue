<script setup lang="ts">
import { onMounted, ref, provide, onBeforeUnmount } from 'vue'
import { NConfigProvider, NMessageProvider, NDialogProvider, darkTheme } from 'naive-ui'
import type { GlobalThemeOverrides } from 'naive-ui'
import { useAuthStore } from './stores/auth'
import { useUiStore } from './stores/ui'
import { useRouter } from 'vue-router'
import { api } from './api'
import { t } from './i18n'
import { sound } from './utils/sound'
import GrassParticles from './components/fx/GrassParticles.vue'

const auth = useAuthStore()
const ui = useUiStore()
const router = useRouter()
const particlesRef = ref<InstanceType<typeof GrassParticles> | null>(null)
const booting = ref(true)

provide('particles', particlesRef)

// 署名水印防删除
const watermarkRef = ref<HTMLElement | null>(null)
let watermarkObserver: MutationObserver | null = null

async function checkSignature() {
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
}

function onVisibilityChange() {
  if (document.visibilityState === 'visible') checkSignature()
}

function startWatermarkGuard() {
  const wm = watermarkRef.value
  if (wm) {
    watermarkObserver = new MutationObserver(() => {
      if (wm && !document.body.contains(wm)) {
        document.body.appendChild(wm)
      }
    })
    watermarkObserver.observe(document.body, { childList: true, subtree: true })
  }

  checkSignature()
  document.addEventListener('visibilitychange', onVisibilityChange)
}

function stopWatermarkGuard() {
  if (watermarkObserver) {
    watermarkObserver.disconnect()
    watermarkObserver = null
  }
  document.removeEventListener('visibilitychange', onVisibilityChange)
}

// 简单 GPU 能力检测：如果 canvas getContext('webgl') 失败或 renderer 包含 SwiftShader/llvmpipe，标记为低性能
function detectLowGpu() {
  try {
    const canvas = document.createElement('canvas')
    const gl = (canvas.getContext('webgl') || canvas.getContext('experimental-webgl')) as WebGLRenderingContext | null
    if (!gl) return true
    const debugInfo = gl.getExtension('WEBGL_debug_renderer_info')
    const renderer = debugInfo ? gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) : ''
    // SwiftShader / llvmpipe / 软件渲染标记为低性能
    if (/swiftshader|llvmpipe|software|microsoft basic/i.test(renderer)) return true
    return false
  } catch {
    return false
  }
}

onMounted(async () => {
  // 1. 首次运行检测：API Key 未配置 → 跳转 setup 向导
  //    API Key 已配置但用户资料未完成 → 跳转资料编辑页
  try {
    const data = await api.getSetupFirstRun()
    if (data?.first_run) {
      router.replace('/setup')
      booting.value = false
      return
    }
    // API Key 已配置，检查用户资料是否完成（localStorage 缓存优先）
    if (!data?.profile_done && !localStorage.getItem('xiaoda_profile_done')) {
      // 需要先登录才能访问需要认证的 /setup/profile
      if (!auth.isLoggedIn) {
        router.replace('/login')
      } else {
        router.replace('/setup/profile')
      }
      booting.value = false
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
  startWatermarkGuard()

  // 弱 GPU 设备降级：移除 backdrop-filter
  if (detectLowGpu()) {
    document.body.classList.add('low-gpu')
  }

  // 草元素音效：首次手势解锁 AudioContext（浏览器自动播放策略）
  const unlock = () => {
    sound.unlock()
    window.removeEventListener('pointerdown', unlock)
    window.removeEventListener('keydown', unlock)
  }
  window.addEventListener('pointerdown', unlock, { passive: true })
  window.addEventListener('keydown', unlock)

  // 全局露珠点击音：草元素按钮与侧边导航
  window.addEventListener('pointerdown', onGlobalTap, { passive: true })

  booting.value = false
})

/** 命中 .dendro-btn / .nav-item / .sponsor-entry 时播放露珠音 */
function onGlobalTap(e: PointerEvent) {
  const el = (e.target as HTMLElement | null)?.closest?.('.dendro-btn, .nav-item, .sponsor-entry')
  if (el) sound.play('click')
}

onBeforeUnmount(() => {
  stopWatermarkGuard()
  ui.stopAutoCheck()
  window.removeEventListener('pointerdown', onGlobalTap)
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
        <div v-if="booting" class="boot-loading">🌿</div>
        <router-view v-else v-slot="{ Component }">
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

/* 亮度调节已移至 .agent-backdrop，仅对背景层生效，避免整页 GPU 合成 */

html, body, #app {
  height: 100%;
  width: 100%;
  overflow: hidden;
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

/* 全局署名水印（非 scoped）——移除 writing-mode 避免每帧重排 */
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
  max-height: 60vh;
}
.brand-watermark span {
  display: inline-block;
}

.boot-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  font-size: 48px;
  animation: boot-pulse 1.2s ease-in-out infinite;
}
@keyframes boot-pulse {
  0%, 100% { opacity: 0.6; transform: scale(1); }
  50% { opacity: 1; transform: scale(1.1); }
}
</style>