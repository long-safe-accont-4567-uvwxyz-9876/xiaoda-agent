<script setup lang="ts">
withDefaults(defineProps<{
  name: string
  size?: number
}>(), {
  size: 28,
})
</script>

<template>
  <span class="view-title-emblem" :style="{ width: `${size}px`, height: `${size}px` }" aria-hidden="true">
    <img :src="`/assets/view-icons/${name}.png`" alt="" />
  </span>
</template>

<style scoped>
.view-title-emblem {
  display: inline-grid;
  flex: 0 0 auto;
  place-items: center;
}

img {
  width: 128%;
  height: 128%;
  object-fit: contain;
  filter: drop-shadow(0 2px 4px rgba(0, 0, 0, 0.34));
}

@media (prefers-reduced-motion: no-preference) {
  .view-title-emblem {
    transition: transform var(--motion-fast) var(--ease-out), filter var(--motion-fast);
  }

  h2:hover .view-title-emblem {
    transform: translateY(-1px) scale(1.025);
  }
}
</style>
