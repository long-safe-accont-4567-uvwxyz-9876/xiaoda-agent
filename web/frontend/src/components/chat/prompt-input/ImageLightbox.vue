<script setup lang="ts">
import { t } from '../../../i18n'

defineProps<{
  show: boolean
  src: string
}>()

const emit = defineEmits<{
  close: []
}>()
</script>

<template>
  <!-- Lightbox -->
  <teleport to="body">
    <transition name="lightbox-fade">
      <div v-if="show" class="prompt-lightbox" @click="emit('close')">
        <img :src="src" :alt="t('chatView.preview')" />
      </div>
    </transition>
  </teleport>
</template>

<style scoped>
/* Lightbox */
.prompt-lightbox {
  position: fixed;
  inset: 0;
  z-index: 1000;
  background: rgba(4, 12, 8, 0.82);
  backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: zoom-out;
}

.prompt-lightbox img {
  max-width: 92vw;
  max-height: 92vh;
  border-radius: 12px;
  box-shadow: 0 12px 48px rgba(0, 0, 0, 0.5);
}

.lightbox-fade-enter-active,
.lightbox-fade-leave-active {
  transition: opacity 0.25s;
}

.lightbox-fade-enter-from,
.lightbox-fade-leave-to {
  opacity: 0;
}
</style>
