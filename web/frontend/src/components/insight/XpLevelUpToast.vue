<script setup lang="ts">
/**
 * XP 升级 Toast：fixed 定位浮层，挂在视图层（tabs 之外）。
 * 自动关闭定时器在 useInsightXp 内管理。
 */
import { t } from '../../i18n'
import type { XpLevelUpState } from '../../composables/useInsightXp'

defineProps<{
  state: XpLevelUpState
}>()
</script>

<template>
  <Transition name="xp-toast">
    <div v-if="state.show" class="xp-levelup-toast">
      <div class="xp-levelup-icon">🌟</div>
      <div class="xp-levelup-text">
        <div class="xp-levelup-title">{{ t('insightView.xpLevelUpTitle') }}</div>
        <div class="xp-levelup-detail">LV{{ state.level }} · {{ state.label }}</div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.xp-levelup-toast {
  position: fixed; top: 20px; right: 20px; z-index: 9999;
  display: flex; align-items: center; gap: 14px;
  padding: 16px 24px; border-radius: 14px;
  background: linear-gradient(135deg, rgba(127, 214, 80, 0.2), rgba(163, 230, 53, 0.15));
  border: 1px solid rgba(127, 214, 80, 0.4);
  backdrop-filter: blur(16px);
  box-shadow: 0 8px 32px rgba(127, 214, 80, 0.2);
}
.xp-levelup-icon { font-size: 32px; animation: xp-bounce 0.6s ease; }
.xp-levelup-title { font-size: 15px; font-weight: 700; color: var(--moon); }
.xp-levelup-detail { font-size: 13px; color: var(--dendro); font-family: 'JetBrains Mono', monospace; }

.xp-toast-enter-active { animation: xp-slide-in 0.4s ease; }
.xp-toast-leave-active { animation: xp-slide-out 0.3s ease; }
@keyframes xp-slide-in { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }
@keyframes xp-slide-out { from { transform: translateX(0); opacity: 1; } to { transform: translateX(100%); opacity: 0; } }
@keyframes xp-bounce { 0%, 100% { transform: scale(1); } 50% { transform: scale(1.3); } }
</style>
