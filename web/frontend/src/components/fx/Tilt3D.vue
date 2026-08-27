<script setup lang="ts">
import { ref, onBeforeUnmount } from 'vue'
import { useUiStore } from '../../stores/ui'

const props = withDefaults(defineProps<{ maxX?: number; maxY?: number; lift?: number }>(), {
  maxX: 7, maxY: 10, lift: 10,
})

const ui = useUiStore()
const el = ref<HTMLElement | null>(null)
const transform = ref('')
const transition = ref('transform .15s ease-out')
const sheenStyle = ref<Record<string, string>>({ opacity: '0' })
// 倾斜溢出遮挡修复：卡片 3D 倾斜时会溢出自身盒子压到相邻卡片，
// 而 DOM 靠后的卡片默认绘制在上层（向左偏时右侧邻卡盖过来最明显）。
// 悬停期间抬升本卡层级，回弹过渡走完后再归还默认层叠
const zBoost = ref(false)
let zTimer: ReturnType<typeof setTimeout> | null = null

function releaseZ() {
  if (zTimer) clearTimeout(zTimer)
  zTimer = setTimeout(() => {
    zBoost.value = false
    zTimer = null
  }, 500)
}

onBeforeUnmount(() => {
  if (zTimer) { clearTimeout(zTimer); zTimer = null }
})

function onMove(e: PointerEvent) {
  if (!ui.tilt3d || !el.value) return
  const rect = el.value.getBoundingClientRect()
  const px = (e.clientX - rect.left) / rect.width - 0.5
  const py = (e.clientY - rect.top) / rect.height - 0.5
  transition.value = 'transform .15s ease-out'
  transform.value =
    `perspective(800px) rotateX(${(-py * props.maxX).toFixed(2)}deg) ` +
    `rotateY(${(px * props.maxY).toFixed(2)}deg) translateZ(${props.lift}px)`
  zBoost.value = true
  if (zTimer) { clearTimeout(zTimer); zTimer = null }
  // 光泽跟随指针
  sheenStyle.value = {
    opacity: '1',
    background: `radial-gradient(circle at ${((px + 0.5) * 100).toFixed(1)}% ${((py + 0.5) * 100).toFixed(1)}%, rgba(232,213,163,0.14), rgba(127,214,80,0.05) 45%, transparent 70%)`,
  }
}

function onLeave() {
  transition.value = 'transform .45s cubic-bezier(.34,1.56,.64,1)'
  transform.value = ''
  sheenStyle.value = { opacity: '0' }
  releaseZ()
}
</script>

<template>
  <div ref="el" class="tilt3d" :style="{ transform, transition, zIndex: zBoost ? 10 : undefined }"
       @pointermove="onMove" @pointerleave="onLeave">
    <slot />
    <span class="tilt-sheen" :style="sheenStyle"></span>
  </div>
</template>

<style scoped>
.tilt3d {
  transform-style: preserve-3d;
  will-change: transform;
  position: relative;
}

.tilt-sheen {
  position: absolute;
  inset: 0;
  border-radius: inherit;
  pointer-events: none;
  transition: opacity 0.3s;
  border-radius: var(--glass-radius);
}
</style>
