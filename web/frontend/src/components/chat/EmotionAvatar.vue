<script setup lang="ts">
import { computed, watch, ref, onBeforeUnmount } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useRouter } from 'vue-router'
import { t } from '../../i18n'

const chat = useChatStore()
const router = useRouter()

// 9 类情绪 → 表现（资产暂缺时用光环色 + emoji 角标占位，切换逻辑真实接 emotion 字段）
const EMOTIONS: Record<string, { color: string; emoji: string }> = {
  '喜悦': { color: '#7fd650', emoji: '😊' },
  '悲伤': { color: '#60a5fa', emoji: '😢' },
  '愤怒': { color: '#f87171', emoji: '😠' },
  '焦虑': { color: '#fbbf24', emoji: '😰' },
  '害羞': { color: '#f9a8d4', emoji: '😳' },
  '好奇': { color: '#a78bfa', emoji: '🤔' },
  '思考': { color: '#67e8f9', emoji: '💭' },
  '恐惧': { color: '#94a3b8', emoji: '😨' },
  '平静': { color: '#9ca3af', emoji: '🌿' },
}

const current = computed(() => EMOTIONS[chat.lastEmotion] || EMOTIONS['平静'])
const pulse = ref(false)

let pulseRAF = 0

watch(() => chat.lastEmotion, () => {
  pulse.value = false
  cancelAnimationFrame(pulseRAF)
  pulseRAF = requestAnimationFrame(() => { pulse.value = true })
})

onBeforeUnmount(() => { cancelAnimationFrame(pulseRAF) })
</script>

<template>
  <div class="emotion-avatar" :class="{ pulse }"
       :style="{ '--ring': current.color }"
       :title="t('emotion.current') + '：' + (chat.lastEmotion || t('emotion.calm'))"
       @click="router.push('/insight')">
    <span class="face">🌱</span>
    <span class="badge">{{ current.emoji }}</span>
    <span class="ring"></span>
  </div>
</template>

<style scoped>
.emotion-avatar {
  position: relative;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: rgba(15, 31, 23, 0.8);
  border: 2px solid var(--ring);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: border-color 0.3s;
  flex-shrink: 0;
}

.face { font-size: 20px; }

.badge {
  position: absolute;
  bottom: -4px;
  right: -4px;
  font-size: 14px;
  filter: drop-shadow(0 1px 2px rgba(0,0,0,.5));
}

.ring {
  position: absolute;
  inset: -2px;
  border-radius: 50%;
  border: 2px solid var(--ring);
  opacity: 0;
  pointer-events: none;
}

.pulse .ring {
  animation: grass-ring 0.8s ease-out;
}

@keyframes grass-ring {
  0% { transform: scale(1); opacity: 0.9; }
  100% { transform: scale(1.9); opacity: 0; }
}
</style>