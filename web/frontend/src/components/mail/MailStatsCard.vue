<script setup lang="ts">
/**
 * 状态统计卡：处理开关/模式/今日用量/累计处理/最近轮询。数据经 props 注入。
 */
import { NButton, NEmpty, NSpin, NStatistic } from 'naive-ui'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import { fmtTime } from './format'
import type { MailStats } from './types'

defineProps<{
  stats: MailStats | null
  loading: boolean
}>()

const emit = defineEmits<{
  (e: 'refresh'): void
}>()
</script>

<template>
  <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section animate-slide-up">
    <div class="section-head">
      <h3>{{ t('mailView.statsCard') }}</h3>
      <n-button size="small" :loading="loading" @click="emit('refresh')">{{ t('refresh') }}</n-button>
    </div>
    <n-spin :show="loading">
      <div class="stats-grid" v-if="stats">
        <div class="stat-item">
          <n-statistic :label="t('mailView.statEnabled')">
            <template #default>
              <span :class="['stat-state', stats.enabled ? 'on' : 'off']">
                ● {{ stats.enabled ? t('mailView.statOn') : t('mailView.statOff') }}
              </span>
            </template>
          </n-statistic>
        </div>
        <div class="stat-item">
          <n-statistic :label="t('mailView.statMode')">
            <span class="stat-val">{{ t(`mailView.modeLabel.${stats.mode}`) || stats.mode }}</span>
          </n-statistic>
        </div>
        <div class="stat-item">
          <n-statistic :label="t('mailView.statDailyCount')">
            <span class="stat-val">{{ stats.daily_count }} / {{ stats.max_per_day }}</span>
          </n-statistic>
        </div>
        <div class="stat-item">
          <n-statistic :label="t('mailView.statProcessedTotal')">
            <span class="stat-val">{{ stats.processed_total }}</span>
          </n-statistic>
        </div>
        <div class="stat-item wide">
          <n-statistic :label="t('mailView.statLastPoll')">
            <span class="stat-val mono">{{ fmtTime(stats.last_poll_time) }}</span>
          </n-statistic>
        </div>
      </div>
      <n-empty v-else style="padding: 24px 0" />
    </n-spin>
  </section></Tilt3D>
</template>

<style scoped>
.section { padding: 16px 18px; margin-bottom: 14px; }
.section-head { display: flex; align-items: center; justify-content: space-between; }
.section-head h3 { font-size: 14px; color: var(--dendro); margin: 0; }

/* 统计卡片 */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 16px;
  margin-top: 4px;
}
.stat-item {
  background: rgba(10, 24, 16, 0.4);
  border: 1px solid var(--glass-border);
  border-radius: 12px;
  padding: 14px 16px;
  transition: border-color 0.25s, box-shadow 0.25s;
}
.stat-item:hover { border-color: rgba(143, 229, 96, 0.35); box-shadow: var(--shadow-glow); }
.stat-item.wide { grid-column: span 2; }
.stat-state { font-size: 16px; font-weight: 600; }
.stat-state.on { color: var(--dendro); }
.stat-state.off { color: var(--moon-dim); }
.stat-val { font-size: 16px; font-weight: 600; color: var(--moon); }
.mono { font-family: 'JetBrains Mono', monospace; font-size: 13px; }

@media (max-width: 640px) {
  .stat-item.wide { grid-column: span 1; }
}
</style>
