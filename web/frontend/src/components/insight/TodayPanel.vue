<script setup lang="ts">
/**
 * 今日 Tab：统计横幅 + 事件时间线。纯展示组件（fmtTs/kindIcon 原样迁移）。
 */
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import type { TodayItem, TodayStats } from './types'

defineProps<{
  items: TodayItem[]
  stats: TodayStats
}>()

const kindIcon: Record<string, string> = {
  memory: 'sprout', event: 'tools', note: 'note', greeting: 'mail',
}

function fmtTs(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div>
    <Tilt3D :max-x="4" :max-y="6"><div class="today-stats glass-panel">
      {{ t('insightView.todayRounds') }} {{ stats.conversations || 0 }} {{ t('insightView.roundsUnit') }} ·
      {{ t('insightView.toolCalls') }} {{ stats.tool_calls || 0 }} {{ t('insightView.times') }} ·
      {{ t('insightView.newMemories') }} {{ stats.memories || 0 }} {{ t('insightView.itemsUnit') }}
    </div></Tilt3D>
    <div class="timeline">
      <Tilt3D v-for="(item, i) in items" :key="i" :max-x="4" :max-y="6" class="timeline-item">
        <span class="tl-time">{{ fmtTs(item.ts) }}</span>
        <span class="tl-icon">{{ kindIcon[item.kind] || '·' }}</span>
        <span class="tl-text">{{ item.text || item.event_type }}</span>
      </Tilt3D>
      <div v-if="!items.length" class="empty-state">
        <p>{{ t('insightView.noEvents') }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.today-stats { padding: 12px 18px; margin-bottom: 14px; color: var(--wisdom); font-size: 14px; }

.timeline { display: flex; flex-direction: column; gap: 2px; }
.timeline-item {
  display: flex; align-items: baseline; gap: 10px;
  padding: 6px 10px; border-left: 2px solid var(--glass-border);
  margin-left: 40px; position: relative; font-size: 13.5px;
}
.tl-time {
  position: absolute; left: -48px; font-size: 11px;
  color: var(--moon-dim); font-family: 'JetBrains Mono', monospace;
}
.tl-icon { flex-shrink: 0; }
.tl-text { color: var(--moon); word-break: break-all; }

.empty-state { padding: 30px; text-align: center; color: var(--moon-dim); }
</style>
