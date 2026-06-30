<script setup lang="ts">
// 草元素神之眼徽记（原神草元素符号的四叶印化简）
// 性能优化：glow 默认关闭（CSS filter: drop-shadow 与 animation: rotate 组合会导致 GPU 卡顿）
// 发光效果改用 SVG 内部渐变实现，避免 CSS filter 每帧重新光栅化
withDefaults(defineProps<{ size?: number; spin?: boolean; glow?: boolean }>(), {
  size: 36, spin: false, glow: false,
})
</script>

<template>
  <span class="dendro-emblem" :class="{ spin }" :style="{ width: size + 'px', height: size + 'px' }">
    <svg :width="size" :height="size" viewBox="0 0 48 48" fill="none">
      <!-- 发光底层（用 SVG 渐变替代 CSS filter，零合成层开销） -->
      <defs>
        <radialGradient v-if="glow" id="emblem-glow">
          <stop offset="0%" stop-color="rgba(127,214,80,0.3)" />
          <stop offset="100%" stop-color="rgba(127,214,80,0)" />
        </radialGradient>
      </defs>
      <circle v-if="glow" cx="24" cy="24" r="22" fill="url(#emblem-glow)" />
      <!-- 外环藤纹 -->
      <circle cx="24" cy="24" r="21" stroke="currentColor" stroke-width="1.2" opacity="0.35"
              stroke-dasharray="4 5" stroke-linecap="round" />
      <!-- 四片主叶 -->
      <g stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" fill="rgba(127,214,80,0.16)">
        <path d="M24 6c4 4.5 4 9.5 0 13-4-3.5-4-8.5 0-13Z" />
        <path d="M42 24c-4.5 4-9.5 4-13 0 3.5-4 8.5-4 13 0Z" />
        <path d="M24 42c-4-4.5-4-9.5 0-13 4 3.5 4 8.5 0 13Z" />
        <path d="M6 24c4.5-4 9.5-4 13 0-3.5 4-8.5 4-13 0Z" />
      </g>
      <!-- 核心智慧之种 -->
      <circle cx="24" cy="24" r="3.4" fill="currentColor" />
      <circle cx="24" cy="24" r="6.5" stroke="currentColor" stroke-width="1" opacity="0.5" />
    </svg>
  </span>
</template>

<style scoped>
.dendro-emblem {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--dendro);
}
/* 旋转动画用 will-change 优化，避免每帧重新合成 */
.dendro-emblem.spin svg {
  animation: emblem-spin 9s linear infinite;
  will-change: transform;
}
@keyframes emblem-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
</style>
