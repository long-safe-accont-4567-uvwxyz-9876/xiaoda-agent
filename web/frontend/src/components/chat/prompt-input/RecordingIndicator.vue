<script setup lang="ts">
import { formatRecordingTime } from './formatRecordingTime'

defineProps<{
  time: number
}>()
</script>

<template>
  <!-- 录音波形区 -->
  <div class="recording-area">
    <div class="recording-indicator"></div>
    <span class="recording-time">{{ formatRecordingTime(time) }}</span>
    <div class="waveform">
      <span v-for="i in 5" :key="i" class="wave-bar" :style="{ animationDelay: `${i * 0.12}s` }"></span>
    </div>
  </div>
</template>

<style scoped>
/* 录音波形区 */
.recording-area {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0;
  min-height: 40px;
}

.recording-indicator {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #f87171;
  animation: pulse-red 1.2s ease-in-out infinite;
  flex-shrink: 0;
}

@keyframes pulse-red {
  0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(248, 113, 113, 0.6); }
  50% { opacity: 0.7; box-shadow: 0 0 0 6px rgba(248, 113, 113, 0); }
}

.recording-time {
  font-family: 'JetBrains Mono', monospace;
  font-size: 14px;
  color: #f87171;
  min-width: 44px;
}

.waveform {
  display: flex;
  align-items: center;
  gap: 3px;
  flex: 1;
  height: 24px;
}

.wave-bar {
  width: 3px;
  background: #f87171;
  border-radius: 2px;
  animation: wave-anim 0.8s ease-in-out infinite alternate;
}

@keyframes wave-anim {
  0% { height: 4px; }
  100% { height: 20px; }
}
</style>
