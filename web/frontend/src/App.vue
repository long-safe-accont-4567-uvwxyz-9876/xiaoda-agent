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
import DendroCursor from './components/fx/DendroCursor.vue'

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

// GPU 自适应降级（治本方案）：
// 不再按"是不是核显"出身一刀切（那会冤枉高配核显、漏掉低配独显）。
// 改为实际渲染帧率决定 + 软件渲染快速兜底。仅软件渲染（必降）立即降级，
// 其余启动后实测 2 秒渲染帧率，跑得动保持满特效，跑不动才降。
function detectSwiftshaderRenderer() {
  try {
    const canvas = document.createElement('canvas')
    const gl = (canvas.getContext('webgl') || canvas.getContext('experimental-webgl')) as WebGLRenderingContext | null
    if (!gl) return true
    const debugInfo = gl.getExtension('WEBGL_debug_renderer_info')
    const renderer = debugInfo ? String(gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL)) : ''
    return /swiftshader|llvmpipe|software|microsoft basic/i.test(renderer)
  } catch {
    return false
  }
}

// 运行时实测帧率：启动后采样 frameCount 帧，均值 < lowFps 判定为低性能。
// 不看出身、不冤枉高配核显，也不漏掉真正跑不动的独显。
function measureRuntimeFps(frameCount = 90, lowFps = 30): Promise<boolean> {
  return new Promise((resolve) => {
    let frames = 0
    let start = 0
    const tick = (now: number) => {
      if (!start) start = now
      frames++
      if (frames >= frameCount) {
        const fps = frames / ((now - start) / 1000)
        resolve(fps < lowFps)
        return
      }
      requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })
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
    // 检测失败：正常启动，不强制跳 setup。
    // 只有 first_run=true（必填 API Key 未配置）才跳 setup；
    // 其他异常（网络错误、HTTP 未就绪等）是正常的，不阻塞启动。
  }
  // 2. 非首次运行：未登录则跳转登录页（已登录的直接进主界面）
  if (!auth.isLoggedIn) {
    router.replace('/login')
  }
  // 3. 已登录：路由守卫会放行，无需额外跳转

  // 启动署名水印防删除守护
  startWatermarkGuard()

  // 弱 GPU 自适应降级：软件渲染必降（快速兜底），其余按运行时实测帧率决定
  // （治本：不看出身，跑得动满特效，跑不动才降）
  if (detectSwiftshaderRenderer()) {
    document.body.classList.add('low-gpu')
  } else {
    measureRuntimeFps().then((low) => {
      if (low) document.body.classList.add('low-gpu')
    })
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
  const el = (e.target as HTMLElement | null)?.closest?.(
    '.dendro-btn, .nav-item, .sponsor-entry, .agent-chip, .n-button, .n-tabs-tab, .n-radio-button, .n-switch'
  )
  if (el) sound.play('click')
}

onBeforeUnmount(() => {
  stopWatermarkGuard()
  ui.stopAutoCheck()
  window.removeEventListener('pointerdown', onGlobalTap)
})

const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#91e866',
    primaryColorHover: '#a9f183',
    primaryColorPressed: '#68c844',
    primaryColorSuppl: '#55d9b2',
    bodyColor: 'transparent',
    cardColor: 'rgba(13, 28, 21, 0.94)',
    modalColor: 'rgba(13, 28, 21, 0.96)',
    popoverColor: 'rgba(10, 23, 17, 0.98)',
    tableColor: 'transparent',
    inputColor: 'rgba(7, 18, 13, 0.72)',
    inputColorDisabled: 'rgba(13, 25, 19, 0.58)',
    borderColor: 'rgba(196, 238, 207, 0.15)',
    dividerColor: 'rgba(218, 245, 226, 0.09)',
    textColorBase: '#f4f8f3',
    textColor1: '#f4f8f3',
    textColor2: 'rgba(244, 248, 243, 0.84)',
    textColor3: 'rgba(222, 233, 225, 0.66)',
    placeholderColor: 'rgba(222, 233, 225, 0.42)',
    successColor: '#91e866',
    infoColor: '#70c7dc',
    errorColor: '#f07c72',
    warningColor: '#efbd64',
    borderRadius: '6px',
    borderRadiusSmall: '5px',
    fontWeightStrong: '700',
  },
  Button: {
    heightMedium: '38px',
    paddingMedium: '0 17px',
    borderRadiusTiny: '6px 3px 6px 3px',
    borderRadiusSmall: '8px 4px 8px 4px',
    borderRadiusMedium: '10px 5px 10px 5px',
    borderRadiusLarge: '12px 5px 12px 5px',
    color: 'rgba(9, 24, 16, 0.78)',
    colorHover: 'rgba(17, 39, 27, 0.92)',
    colorPressed: 'rgba(7, 19, 13, 0.96)',
    colorFocus: 'rgba(17, 39, 27, 0.92)',
    border: '1px solid rgba(210, 235, 207, 0.18)',
    borderHover: '1px solid rgba(187, 226, 165, 0.42)',
    borderPressed: '1px solid rgba(91, 181, 137, 0.4)',
    borderFocus: '1px solid rgba(224, 238, 181, 0.54)',
    colorSecondary: 'rgba(197, 229, 184, 0.07)',
    colorSecondaryHover: 'rgba(197, 229, 184, 0.13)',
    colorSecondaryPressed: 'rgba(82, 169, 128, 0.1)',
    colorTertiary: 'rgba(7, 22, 14, 0.42)',
    colorTertiaryHover: 'rgba(197, 229, 184, 0.09)',
    colorTertiaryPressed: 'rgba(82, 169, 128, 0.08)',
    colorQuaternary: 'transparent',
    colorQuaternaryHover: 'rgba(197, 229, 184, 0.08)',
    colorQuaternaryPressed: 'rgba(82, 169, 128, 0.07)',
    colorPrimary: '#78d84f',
    colorHoverPrimary: '#91e866',
    colorPressedPrimary: '#58b83b',
    colorFocusPrimary: '#91e866',
    borderPrimary: '1px solid rgba(236, 224, 164, 0.72)',
    borderHoverPrimary: '1px solid rgba(249, 239, 191, 0.92)',
    borderPressedPrimary: '1px solid rgba(189, 181, 123, 0.72)',
    borderFocusPrimary: '1px solid rgba(249, 239, 191, 0.92)',
    textColorPrimary: '#0a2414',
    textColorHoverPrimary: '#071b0f',
    textColorPressedPrimary: '#071b0f',
    textColorFocusPrimary: '#071b0f',
  },
  Card: {
    borderRadius: '8px',
  },
  Dialog: {
    borderRadius: '8px',
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
        <DendroCursor />
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
@import './styles/components.css';

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
  font-family: 'Noto Sans SC', 'Microsoft YaHei UI', 'PingFang SC', system-ui, sans-serif;
  color: var(--moon);
  background: var(--forest-deep);
  font-size: 14px;
  line-height: 1.55;
  text-rendering: optimizeLegibility;
  -webkit-font-smoothing: antialiased;
}

button,
input,
textarea,
select {
  font: inherit;
}

button,
a,
[role='button'] {
  -webkit-tap-highlight-color: transparent;
}

::selection {
  background: rgba(145, 232, 102, 0.24);
  color: var(--moon);
}

/* 叶片翻页转场 · v2 弹簧+柔焦 */
.leaf-page-enter-active {
  transition:
    transform 0.46s var(--ease-spring, cubic-bezier(0.22, 1.4, 0.36, 1)),
    opacity 0.3s var(--ease-smooth),
    filter 0.4s var(--ease-smooth);
  transform-style: preserve-3d;
  will-change: transform, opacity, filter;
}
.leaf-page-leave-active {
  transition:
    transform 0.24s cubic-bezier(0.5, 0, 0.75, 0),
    opacity 0.22s var(--ease-smooth),
    filter 0.22s var(--ease-smooth);
  transform-style: preserve-3d;
  will-change: transform, opacity, filter;
}
.leaf-page-enter-from {
  opacity: 0;
  transform: perspective(1200px) rotateY(9deg) translateX(34px) scale(0.985);
  filter: blur(8px);
}
.leaf-page-leave-to {
  opacity: 0;
  transform: perspective(1200px) rotateY(-7deg) translateX(-28px) scale(0.99);
  filter: blur(4px);
}

@media (prefers-reduced-motion: reduce) {
  .leaf-page-enter-from, .leaf-page-leave-to {
    transform: none;
    filter: none;
  }
}
body.low-gpu .leaf-page-enter-from,
body.low-gpu .leaf-page-leave-to {
  filter: none;
}

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  border: 2px solid transparent;
  border-radius: 8px;
  background: rgba(196, 238, 207, 0.22);
  background-clip: padding-box;
}
::-webkit-scrollbar-thumb:hover { background-color: rgba(145, 232, 102, 0.42); }

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