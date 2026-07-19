<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useUiStore } from '../../stores/ui'

/**
 * 草元素光标 · 小叶片
 * - 叶片箭头快速贴合鼠标 + 露珠光环慢速拖尾（双层 lerp → 丝滑跟随）
 * - hover 可点击元素：叶片舒展、光环绽放
 * - 文本输入框：化作草茎 I 光标
 * - 点击：按压回弹 + 草光涟漪
 * - 触屏 / 弱 GPU / 减弱动效 自动降级；设置页可开关
 */

const ui = useUiStore()
const layer = ref<HTMLElement | null>(null)
const leafEl = ref<HTMLElement | null>(null)
const ringEl = ref<HTMLElement | null>(null)
const active = ref(false)
const shown = ref(false)

let mx = -100, my = -100   // 鼠标目标点
let lx = -100, ly = -100   // 叶片当前点（快层）
let rx = -100, ry = -100   // 光环当前点（慢层）
let raf = 0
let state: 'default' | 'pointer' | 'text' = 'default'
let pressing = false

const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
const coarsePointer = window.matchMedia('(pointer: coarse)').matches

const POINTER_SEL = [
  'a', 'button', '[role="button"]', 'label', 'select',
  '.n-button', '.nav-item', '.agent-chip', '.dendro-btn', '.sponsor-entry',
  '.n-switch', '.n-tabs-tab', '.n-radio-button', '.n-slider', '.n-slider-handle',
  '.n-base-selection', '.n-tag', '.n-pagination-item', '.n-rate', '[data-clickable]',
].join(', ')
const TEXT_SEL = 'input, textarea, [contenteditable="true"], .n-input__input-el, .n-input__textarea-el'

function applyStateClass() {
  const el = layer.value
  if (!el) return
  el.classList.toggle('dc-pointer', state === 'pointer')
  el.classList.toggle('dc-text', state === 'text')
  el.classList.toggle('dc-down', pressing)
}

function onMove(e: MouseEvent) {
  mx = e.clientX
  my = e.clientY
  if (!shown.value) {
    // 首次进入：直接瞬移到鼠标处，避免从角落飞入
    lx = mx; ly = my; rx = mx; ry = my
    shown.value = true
  }
}

function onOver(e: MouseEvent) {
  const t = e.target as HTMLElement | null
  if (!t || typeof t.closest !== 'function') return
  const next = t.closest(TEXT_SEL) ? 'text' : (t.closest(POINTER_SEL) ? 'pointer' : 'default')
  if (next !== state) {
    state = next
    applyStateClass()
  }
}

function onDown(e: PointerEvent) {
  pressing = true
  applyStateClass()
  spawnRipple(e.clientX, e.clientY)
}

function onUp() {
  if (!pressing) return
  pressing = false
  applyStateClass()
}

function onLeave() { shown.value = false }
function onEnter() { shown.value = true }

function spawnRipple(x: number, y: number) {
  const host = layer.value
  if (!host || reduceMotion) return
  const s = document.createElement('span')
  s.className = 'dc-ripple'
  s.style.left = `${x}px`
  s.style.top = `${y}px`
  host.appendChild(s)
  s.addEventListener('animationend', () => s.remove(), { once: true })
}

function loop() {
  const kLeaf = reduceMotion ? 1 : 0.55   // 叶片：快速贴合
  const kRing = reduceMotion ? 1 : 0.16   // 光环：慢速拖尾
  lx += (mx - lx) * kLeaf
  ly += (my - ly) * kLeaf
  rx += (mx - rx) * kRing
  ry += (my - ry) * kRing
  const l = leafEl.value
  const r = ringEl.value
  if (l) l.style.transform = `translate3d(${lx.toFixed(2)}px, ${ly.toFixed(2)}px, 0)`
  if (r) r.style.transform = `translate3d(${rx.toFixed(2)}px, ${ry.toFixed(2)}px, 0) translate(-50%, -50%)`
  raf = requestAnimationFrame(loop)
}

function enable() {
  if (coarsePointer || active.value) return
  active.value = true
  document.body.classList.add('dendro-cursor')
  window.addEventListener('mousemove', onMove, { passive: true })
  window.addEventListener('mouseover', onOver, { passive: true })
  window.addEventListener('pointerdown', onDown, { passive: true })
  window.addEventListener('pointerup', onUp, { passive: true })
  document.documentElement.addEventListener('mouseleave', onLeave)
  document.documentElement.addEventListener('mouseenter', onEnter)
  raf = requestAnimationFrame(loop)
}

function disable() {
  if (!active.value) return
  active.value = false
  shown.value = false
  document.body.classList.remove('dendro-cursor')
  window.removeEventListener('mousemove', onMove)
  window.removeEventListener('mouseover', onOver)
  window.removeEventListener('pointerdown', onDown)
  window.removeEventListener('pointerup', onUp)
  document.documentElement.removeEventListener('mouseleave', onLeave)
  document.documentElement.removeEventListener('mouseenter', onEnter)
  cancelAnimationFrame(raf)
}

onMounted(() => {
  if (ui.dendroCursor) enable()
  watch(() => ui.dendroCursor, v => { v ? enable() : disable() })
})

onBeforeUnmount(() => disable())
</script>

<template>
  <teleport to="body">
    <div v-if="active" ref="layer" class="dendro-cursor-layer" :class="{ 'dc-shown': shown }" aria-hidden="true">
      <!-- 慢速拖尾：露珠光环 -->
      <div ref="ringEl" class="dc-ring"></div>
      <!-- 快速贴合：小叶片光标 -->
      <div ref="leafEl" class="dc-leaf">
        <div class="dc-leaf-inner">
          <svg class="dc-svg-leaf" viewBox="0 0 32 32" width="30" height="30">
            <defs>
              <linearGradient id="dc-grad" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0" stop-color="#e9ffc9" />
                <stop offset="0.45" stop-color="#8fe560" />
                <stop offset="1" stop-color="#4fd6a5" />
              </linearGradient>
            </defs>
            <!-- 叶片：尖端朝左上（热点），向右下舒展 -->
            <path
              d="M3.5 3.5 C 11 4.5, 21 8.5, 25 18.5 C 25.8 21.5, 24.2 24.5, 21 25.5 C 12.5 22.5, 6 13.5, 3.5 3.5 Z"
              fill="url(#dc-grad)"
            />
            <!-- 叶脉 -->
            <path
              d="M5 5 C 10.5 8.5, 16.5 14.5, 20.5 23"
              stroke="rgba(18, 64, 38, 0.5)" stroke-width="1.1" fill="none" stroke-linecap="round"
            />
            <!-- 叶面露珠高光 -->
            <circle cx="10.5" cy="9" r="1.6" fill="rgba(255, 255, 255, 0.85)" />
          </svg>
          <div class="dc-ibeam"></div>
        </div>
      </div>
    </div>
  </teleport>
</template>

<style>
/* 非 scoped：光标层 teleport 到 body，且需全局接管系统光标 */
body.dendro-cursor,
body.dendro-cursor * {
  cursor: none !important;
}

.dendro-cursor-layer {
  position: fixed;
  inset: 0;
  z-index: 99999;
  pointer-events: none;
  opacity: 0;
  transition: opacity 0.25s ease;
}
.dendro-cursor-layer.dc-shown { opacity: 1; }

/* —— 光标本体：小叶片 —— */
.dc-leaf {
  position: absolute;
  top: 0;
  left: 0;
  will-change: transform;
}
.dc-leaf-inner {
  transform-origin: 4px 4px; /* 叶尖即热点，绕尖缩放 */
  transition: transform 0.28s var(--ease-spring, cubic-bezier(0.22, 1.4, 0.36, 1));
  filter: drop-shadow(0 0 6px rgba(143, 229, 96, 0.55)) drop-shadow(0 1px 2px rgba(0, 0, 0, 0.4));
}
.dc-svg-leaf { display: block; }

/* 草茎 I 光标（文本态才显示） */
.dc-ibeam {
  display: none;
  width: 3.5px;
  height: 22px;
  margin: 2px 0 0 2px;
  border-radius: 2px;
  background: linear-gradient(180deg, #d4ffb0, #8fe560 55%, #4fd6a5);
  box-shadow: 0 0 8px rgba(143, 229, 96, 0.7);
  position: relative;
}
.dc-ibeam::before,
.dc-ibeam::after {
  content: '';
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  width: 11px;
  height: 3.5px;
  border-radius: 2px;
  background: inherit;
}
.dc-ibeam::before { top: -1px; }
.dc-ibeam::after { bottom: -1px; }

/* hover 可点击：叶片舒展 + 微倾 */
.dc-pointer .dc-leaf-inner { transform: scale(1.18) rotate(-10deg); }
/* 按压回弹 */
.dc-down .dc-leaf-inner { transform: scale(0.82) rotate(4deg); }
.dc-pointer.dc-down .dc-leaf-inner { transform: scale(1.02) rotate(-4deg); }
/* 文本态：叶片收起，草茎显现 */
.dc-text .dc-svg-leaf { display: none; }
.dc-text .dc-ibeam { display: block; }

/* —— 拖尾露珠环 —— */
.dc-ring {
  position: absolute;
  top: 0;
  left: 0;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  border: 1.5px solid rgba(143, 229, 96, 0.5);
  background: radial-gradient(circle, rgba(212, 255, 176, 0.16) 0%, transparent 65%);
  box-shadow: 0 0 12px rgba(143, 229, 96, 0.25), inset 0 0 8px rgba(143, 229, 96, 0.15);
  will-change: transform;
  transition:
    width 0.3s var(--ease-spring, cubic-bezier(0.22, 1.4, 0.36, 1)),
    height 0.3s var(--ease-spring, cubic-bezier(0.22, 1.4, 0.36, 1)),
    border-color 0.25s ease, background 0.25s ease;
}
/* 环心一颗小露珠 */
.dc-ring::after {
  content: '';
  position: absolute;
  top: 50%;
  left: 50%;
  width: 4px;
  height: 4px;
  margin: -2px 0 0 -2px;
  border-radius: 50%;
  background: #e9ffc9;
  box-shadow: 0 0 6px rgba(233, 255, 201, 0.9);
}
.dc-pointer .dc-ring {
  width: 42px;
  height: 42px;
  border-color: rgba(212, 255, 176, 0.85);
  background: radial-gradient(circle, rgba(212, 255, 176, 0.22) 0%, transparent 65%);
}
.dc-text .dc-ring {
  width: 34px;
  height: 34px;
  border-color: rgba(79, 214, 165, 0.6);
}
.dc-down .dc-ring {
  width: 22px;
  height: 22px;
}

/* —— 点击草光涟漪 —— */
.dc-ripple {
  position: fixed;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  border: 2px solid rgba(212, 255, 176, 0.9);
  transform: translate(-50%, -50%);
  animation: dc-ripple 0.55s cubic-bezier(0.22, 1, 0.36, 1) forwards;
}
@keyframes dc-ripple {
  0%   { width: 8px;  height: 8px;  opacity: 0.9; }
  100% { width: 54px; height: 54px; opacity: 0; }
}

/* —— 降级：弱 GPU / 减弱动效 —— */
body.low-gpu .dc-leaf-inner { filter: none; }
body.low-gpu .dc-ring { box-shadow: none; background: transparent; }
body.low-gpu .dc-ring::after { box-shadow: none; }
@media (prefers-reduced-motion: reduce) {
  .dc-leaf-inner, .dc-ring { transition: none; }
  .dc-ripple { animation: none; opacity: 0; }
}
</style>
