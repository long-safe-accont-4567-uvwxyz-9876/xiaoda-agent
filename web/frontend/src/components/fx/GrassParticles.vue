<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { useUiStore } from '../../stores/ui'

const ui = useUiStore()
const canvasEl = ref<HTMLCanvasElement | null>(null)

interface P {
  x: number; y: number; r: number; leaf: boolean
  vx: number; vy: number; phase: number; period: number; drift: number
}

let particles: P[] = []
let raf = 0
let lastFrame = 0
let running = false
let cachedCtx: CanvasRenderingContext2D | null = null
let mouseX = -9999
let mouseY = -9999
// 鼠标拖尾：背景同款叶片粒子（少量飘落）
let trailLeaves: Array<{
  x: number; y: number; vx: number; vy: number;
  size: number; rot: number; vrot: number; t: number;
}> = []
let lastTrailPos: { x: number; y: number } | null = null
let burstParticles: Array<{ x: number; y: number; vx: number; vy: number; t: number; rot: number }> = []

const DENSITY: Record<string, number> = { off: 0, low: 12, medium: 36, high: 60 }
const FRAME_MS = 1000 / 30

const GLOW_SIZE = 48
const GLOW_CORE_R = 8
let glowDot: HTMLCanvasElement | null = null

function initGlowDot() {
  glowDot = document.createElement('canvas')
  glowDot.width = GLOW_SIZE
  glowDot.height = GLOW_SIZE
  const g = glowDot.getContext('2d')!
  const cx = GLOW_SIZE / 2, cy = GLOW_SIZE / 2
  g.shadowColor = '#7fd650'
  g.shadowBlur = 6
  g.fillStyle = '#7fd650'
  g.beginPath()
  g.arc(cx, cy, GLOW_CORE_R, 0, Math.PI * 2)
  g.fill()
}

function count(): number {
  return DENSITY[ui.particles] ?? 36
}

function spawn(w: number, h: number): P {
  return {
    x: Math.random() * w,
    y: Math.random() * h,
    r: 2 + Math.random() * 3,
    leaf: Math.random() < 0.35,
    vx: 0.1 + Math.random() * 0.25,
    vy: -0.05 - Math.random() * 0.15,
    phase: Math.random() * Math.PI * 2,
    period: 3000 + Math.random() * 3000,
    drift: Math.random() * Math.PI * 2,
  }
}

function resize() {
  const c = canvasEl.value
  if (!c) return
  c.width = window.innerWidth
  c.height = window.innerHeight
  cachedCtx = c.getContext('2d')
  rebuild()
}

function rebuild() {
  const c = canvasEl.value
  if (!c) return
  const n = count()
  particles = Array.from({ length: n }, () => spawn(c.width, c.height))
}

function drawLeaf(ctx: CanvasRenderingContext2D, x: number, y: number, size: number, rot: number, alpha: number) {
  ctx.save()
  ctx.translate(x, y)
  ctx.rotate(rot)
  ctx.globalAlpha = alpha
  ctx.fillStyle = '#7fd650'
  ctx.beginPath()
  ctx.moveTo(0, -size)
  ctx.bezierCurveTo(size * 0.8, -size * 0.3, size * 0.8, size * 0.5, 0, size)
  ctx.bezierCurveTo(-size * 0.8, size * 0.5, -size * 0.8, -size * 0.3, 0, -size)
  ctx.fill()
  ctx.restore()
}

function frame(now: number) {
  raf = requestAnimationFrame(frame)
  if (now - lastFrame < FRAME_MS) return
  lastFrame = now
  const c = canvasEl.value
  if (!c) return
  if (!cachedCtx || cachedCtx.canvas !== c) cachedCtx = c.getContext('2d')
  if (!cachedCtx) return
  const ctx = cachedCtx
  const w = c.width, h = c.height
  ctx.clearRect(0, 0, w, h)

  // 鼠标轨迹（草元素叶片拖尾）—— 背景同款叶片，少量飘落，颜色淡
  if (ui.dendroCursorTrail && trailLeaves.length > 0) {
    const tNow = performance.now()
    trailLeaves = trailLeaves.filter(l => tNow - l.t < 1100)
    for (const l of trailLeaves) {
      const age = (tNow - l.t) / 1100
      const a = (1 - age) ** 1.4 * 0.4      // 颜色淡：最高 alpha 0.4
      l.x += l.vx
      l.y += l.vy
      l.vy += 0.02                           // 轻微下落
      l.rot += l.vrot
      drawLeaf(ctx, l.x, l.y, l.size, l.rot, a)
    }
  }

  // 常驻粒子（柏林噪声近似：sin 漂移 + 风场）
  for (const p of particles) {
    p.drift += 0.004
    p.x += p.vx + Math.sin(p.drift) * 0.3
    p.y += p.vy + Math.cos(p.drift * 0.7) * 0.15
    // 鼠标斥力（鼠标坐标是 CSS 像素，粒子是 canvas 内部坐标，需换算）
    const dx = p.x - mouseX * RENDER_SCALE, dy = p.y - mouseY * RENDER_SCALE
    const d2 = dx * dx + dy * dy
    if (d2 < 6400 && d2 > 1) {
      const f = (80 - Math.sqrt(d2)) / 80 * 0.8
      p.x += (dx / Math.sqrt(d2)) * f * 2
      p.y += (dy / Math.sqrt(d2)) * f * 2
    }
    if (p.x > w + 20) p.x = -10
    if (p.x < -20) p.x = w + 10
    if (p.y < -20) p.y = h + 10
    if (p.y > h + 20) p.y = -10
    const breathe = 0.35 + 0.25 * Math.sin(now / p.period * Math.PI * 2 + p.phase)
    if (p.leaf) {
      drawLeaf(ctx, p.x, p.y, p.r * 2, p.drift, breathe)
    } else {
      ctx.globalAlpha = breathe
      const s = GLOW_SIZE * p.r / GLOW_CORE_R
      ctx.drawImage(glowDot!, p.x - s / 2, p.y - s / 2, s, s)
    }
  }

  // 爆发粒子（发送消息的叶子、问候的蒲公英）
  const tNow = performance.now()
  burstParticles = burstParticles.filter(b => tNow - b.t < 1600)
  for (const b of burstParticles) {
    const age = (tNow - b.t) / 1600
    b.x += b.vx
    b.y += b.vy
    b.vy += 0.04
    b.rot += 0.08
    drawLeaf(ctx, b.x, b.y, 5, b.rot, (1 - age) * 0.8)
  }
  ctx.globalAlpha = 1
}

function onMouse(e: MouseEvent) {
  mouseX = e.clientX
  mouseY = e.clientY
  // 拖尾产生条件：仅看拖尾开关；背景同款叶片，距离过滤控制密度（少量）
  if (ui.dendroCursorTrail) {
    // 距离过滤：移动 > 18px 才生成一片叶子，避免密集
    if (!lastTrailPos || (lastTrailPos.x - e.clientX) ** 2 + (lastTrailPos.y - e.clientY) ** 2 > 324) {
      trailLeaves.push({
        x: e.clientX + (Math.random() - 0.5) * 6,
        y: e.clientY + (Math.random() - 0.5) * 6,
        vx: (Math.random() - 0.5) * 0.6,
        vy: -0.2 - Math.random() * 0.4,      // 轻微上飘
        size: 3 + Math.random() * 3,          // 3-6px 小叶片（同背景）
        rot: Math.random() * Math.PI * 2,
        vrot: (Math.random() - 0.5) * 0.1,
        t: performance.now(),
      })
      lastTrailPos = { x: e.clientX, y: e.clientY }
      if (trailLeaves.length > 22) trailLeaves.shift()   // 少量
    }
  }
}

function onVisibility() {
  if (document.hidden) stop()
  else start()
}

function start() {
  if (running || count() === 0) return
  running = true
  raf = requestAnimationFrame(frame)
}

function stop() {
  running = false
  cancelAnimationFrame(raf)
}

/** 对外：从某坐标爆发叶子（发送消息特效） */
function burst(x: number, y: number, n = 10) {
  for (let i = 0; i < n; i++) {
    const ang = Math.random() * Math.PI * 2
    const speed = 1 + Math.random() * 3
    burstParticles.push({
      x, y, vx: Math.cos(ang) * speed, vy: Math.sin(ang) * speed - 2,
      t: performance.now(), rot: Math.random() * Math.PI,
    })
  }
}

/** 对外：右上蒲公英雨（问候推送） */
function dandelionRain() {
  const w = window.innerWidth
  for (let i = 0; i < 16; i++) {
    burstParticles.push({
      x: w - Math.random() * w * 0.4, y: -10 - Math.random() * 60,
      vx: -0.5 - Math.random(), vy: 0.8 + Math.random() * 1.2,
      t: performance.now() + Math.random() * 800, rot: Math.random() * Math.PI,
    })
  }
}

defineExpose({ burst, dandelionRain })

// FPS 探测：启动 2s 后均值 <24fps 自动降级
let fpsFrames = 0
let fpsStart = 0
let fpsRAF = 0
function fpsProbe(now: number) {
  if (!fpsStart) fpsStart = now
  fpsFrames++
  if (now - fpsStart < 2000) {
    fpsRAF = requestAnimationFrame(fpsProbe)
  } else {
    const fps = fpsFrames / ((now - fpsStart) / 1000)
    if (fps < 24 && ui.particles !== 'off' && ui.particles !== 'low') {
      ui.setParticles('low')
    }
  }
}

watch(() => ui.particles, () => {
  if (count() > 0 && !glowDot) initGlowDot()
  rebuild()
  // 既无粒子又无拖尾才停止帧循环
  if (count() === 0 && !ui.dendroCursorTrail) stop()
  else start()
})

// 拖尾开关变化：开启时确保帧循环运行（trail 需要 rAF 渲染）
watch(() => ui.dendroCursorTrail, (v) => {
  if (v) {
    if (!glowDot) initGlowDot()
    start()
  } else if (count() === 0) {
    stop()
  }
})

onMounted(() => {
  window.addEventListener('resize', resize)
  window.addEventListener('mousemove', onMouse, { passive: true })
  document.addEventListener('visibilitychange', onVisibility)
  // reduced-motion：不再强制 off，保留 particles 设置，拖尾开关独立可控
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    if (ui.particles === 'off' && !ui.dendroCursorTrail) return
  }
  if (count() > 0 || ui.dendroCursorTrail) {
    if (!glowDot) initGlowDot()
  }
  resize()
  start()
  fpsRAF = requestAnimationFrame(fpsProbe)
})

onBeforeUnmount(() => {
  stop()
  if (fpsRAF) cancelAnimationFrame(fpsRAF)
  window.removeEventListener('resize', resize)
  window.removeEventListener('mousemove', onMouse)
  document.removeEventListener('visibilitychange', onVisibility)
})
</script>

<template>
  <canvas v-if="ui.particles !== 'off' || ui.dendroCursorTrail" ref="canvasEl" class="grass-particles"></canvas>
</template>

<style scoped>
.grass-particles {
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 9999;
}
</style>