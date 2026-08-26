<script setup lang="ts">
/**
 * XP 亲密度 Tab：等级主卡 + 双列属性卡 + 等级阶梯 + 获取记录 + 里程碑。
 * 数据经 props 注入；来源文案/图标与时间格式化等纯展示逻辑原样迁移。
 */
import { computed } from 'vue'
import SumeruIcon from '../fx/SumeruIcon.vue'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import type { XpLevelConfig, XpState } from '../../api'

const props = defineProps<{
  xpState: XpState
  xpLevels: XpLevelConfig[]
}>()

const nextLevelLabel = computed(() => {
  const next = (props.xpState?.level || 1) + 1
  const found = props.xpLevels?.find(l => l.level === next)
  return found?.label || `LV${next}`
})

function formatTime(ts: string | number): string {
  if (!ts) return ''
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

const _sourceLabels: Record<string, string> = {
  chat: '日常对话',
  deep_chat: '深度对话',
  support: '情感支持',
  task_collab: '任务协作',
  daily_login: '每日登录',
}
const _sourceIcons: Record<string, string> = {
  chat: 'chat',
  deep_chat: 'note',
  support: 'insight',
  task_collab: 'agents',
  daily_login: 'sprout',
}
function getSourceLabel(source: string): string {
  return _sourceLabels[source] || source || '未知来源'
}
function getSourceIcon(source: string): string {
  return _sourceIcons[source] || 'sparkle'
}
</script>

<template>
  <div>
    <!-- 等级主卡片：大爱心 + 等级名 + XP -->
    <Tilt3D :max-x="4" :max-y="6"><div class="xp-hero glass-panel">
      <div class="xp-hero-heart" :class="'xp-lv-' + xpState.level">
        <span class="xp-heart-icon">♥</span>
        <span class="xp-hero-lv">LV{{ xpState.level }}</span>
      </div>
      <div class="xp-hero-info">
        <div class="xp-hero-label">{{ xpState.level_label || '陌生人' }}</div>
        <div class="xp-hero-xp">{{ xpState.xp ?? 0 }} <span class="xp-hero-unit">XP</span></div>
        <!-- 进度条 -->
        <div class="xp-bar-wrap">
          <div class="xp-bar-track">
            <div class="xp-bar-fill" :style="{ width: Math.round((xpState.progress || 0) * 100) + '%' }">
              <div class="xp-bar-glow"></div>
            </div>
          </div>
          <div class="xp-bar-label">
            <span>{{ Math.round((xpState.progress || 0) * 100) }}%</span>
            <span v-if="xpState.level < 6">距 {{ nextLevelLabel }} 还需 {{ (xpState.next_level_xp || 0) - (xpState.xp || 0) }} XP</span>
            <span v-else class="xp-max-label">最高等级 ♥</span>
          </div>
        </div>
      </div>
    </div></Tilt3D>

    <!-- 双列属性卡：主动性 & 情感丰富度 -->
    <div class="xp-attrs-row">
      <div class="xp-attr-card glass-panel">
        <div class="xp-attr-header">
          <span class="xp-attr-icon">⚡</span>
          <span class="xp-attr-title">{{ t('insightView.xpProactivity') }}</span>
        </div>
        <div class="xp-attr-gauge">
          <div class="xp-attr-bar">
            <div class="xp-attr-fill xp-attr-fill--proactive" :style="{ width: Math.round((xpState.level_config?.proactivity || 0) * 100) + '%' }"></div>
          </div>
          <span class="xp-attr-pct">{{ Math.round((xpState.level_config?.proactivity || 0) * 100) }}%</span>
        </div>
        <div class="xp-attr-desc">影响主动问候的频率和随机性</div>
      </div>
      <div class="xp-attr-card glass-panel">
        <div class="xp-attr-header">
          <span class="xp-attr-icon">💜</span>
          <span class="xp-attr-title">{{ t('insightView.xpEmotionalRichness') }}</span>
        </div>
        <div class="xp-attr-gauge">
          <div class="xp-attr-bar">
            <div class="xp-attr-fill xp-attr-fill--emotion" :style="{ width: Math.round((xpState.level_config?.emotional_richness || 0) * 100) + '%' }"></div>
          </div>
          <span class="xp-attr-pct">{{ Math.round((xpState.level_config?.emotional_richness || 0) * 100) }}%</span>
        </div>
        <div class="xp-attr-desc">关联 SOUL.md 中的情感表达深度</div>
      </div>
    </div>

    <!-- 等级阶梯 -->
    <div class="xp-levels-card glass-panel">
      <h4>{{ t('insightView.xpLevelProgress') }}</h4>
      <div class="xp-levels-track">
        <div v-for="(lvl, idx) in xpLevels" :key="lvl.level"
             class="xp-level-step" :class="{ 'xp-level-active': lvl.level <= xpState.level, 'xp-level-current': lvl.level === xpState.level }">
          <div class="xp-level-dot">
            <span v-if="lvl.level <= xpState.level">♥</span>
            <span v-else class="xp-level-dot-empty"></span>
          </div>
          <div class="xp-level-step-info">
            <span class="xp-level-step-lv">LV{{ lvl.level }}</span>
            <span class="xp-level-step-name">{{ lvl.label }}</span>
            <span class="xp-level-step-xp">{{ lvl.threshold }} XP</span>
          </div>
          <div v-if="idx < xpLevels.length - 1" class="xp-level-connector" :class="{ 'xp-level-connector-done': lvl.level < xpState.level }"></div>
        </div>
      </div>
    </div>

    <!-- XP 获取记录 -->
    <div class="xp-history-card glass-panel">
      <h4>{{ t('insightView.xpHistoryTitle') }}</h4>
      <div class="xp-history-list" v-if="xpState.history?.length">
        <div v-for="entry in xpState.history" :key="entry.timestamp" class="xp-history-item">
          <span class="xp-history-icon"><SumeruIcon :name="getSourceIcon(entry.source)" :size="14" /></span>
          <div class="xp-history-body">
            <span class="xp-history-label">{{ getSourceLabel(entry.source) }}</span>
            <span class="xp-history-time">{{ formatTime(entry.timestamp) }}</span>
          </div>
          <span class="xp-history-amount" :class="entry.amount > 0 ? 'positive' : 'negative'">
            {{ entry.amount > 0 ? '+' : '' }}{{ entry.amount }}
          </span>
        </div>
      </div>
      <div class="xp-empty" v-else>
        <span class="xp-empty-icon">♥</span>
        <span>{{ t('insightView.xpNoHistory') }}</span>
      </div>
    </div>

    <!-- 里程碑 -->
    <div class="xp-milestones-card glass-panel" v-if="xpState.milestones && (Array.isArray(xpState.milestones) ? xpState.milestones.length : Object.keys(xpState.milestones).length)">
      <h4>{{ t('insightView.xpMilestones') }}</h4>
      <div class="xp-milestones-list">
        <template v-if="Array.isArray(xpState.milestones)">
          <div v-for="(ms, idx) in xpState.milestones" :key="idx" class="xp-milestone-item">
            <span class="xp-milestone-icon">♥</span>
            <span class="xp-milestone-name">LV{{ ms.from_level }} → LV{{ ms.to_level }}</span>
            <span class="xp-milestone-date">{{ formatTime(ms.timestamp) }}</span>
          </div>
        </template>
        <template v-else>
          <div v-for="(date, milestone) in xpState.milestones" :key="milestone" class="xp-milestone-item">
            <span class="xp-milestone-icon">♥</span>
            <span class="xp-milestone-name">{{ milestone }}</span>
            <span class="xp-milestone-date">{{ formatTime(date) }}</span>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ── XP 亲密度 ── */
@keyframes xpHeartPulse {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.08); }
}
@keyframes xpBarShimmer {
  0% { background-position: -200% center; }
  100% { background-position: 200% center; }
}
@keyframes xpGlow {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 0.8; }
}

/* Hero card */
.xp-hero {
  display: flex; align-items: center; gap: 22px;
  padding: 28px 28px; margin-bottom: 16px;
  background: linear-gradient(135deg, rgba(220, 60, 80, 0.08) 0%, rgba(160, 80, 200, 0.06) 100%);
}
.xp-hero-heart {
  position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center;
  width: 90px; height: 90px; flex-shrink: 0;
}
.xp-heart-icon {
  font-size: 52px; line-height: 1;
  background: linear-gradient(135deg, #f472b6, #e879a9, #c084fc);
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
  animation: xpHeartPulse 2.4s ease-in-out infinite;
  filter: drop-shadow(0 0 12px rgba(244, 114, 182, 0.4));
}
.xp-hero-lv {
  position: absolute; bottom: 2px;
  font-size: 11px; font-weight: 800; font-family: 'JetBrains Mono', monospace;
  color: #f9a8d4; background: rgba(244, 114, 182, 0.15);
  padding: 1px 10px; border-radius: 10px;
  letter-spacing: 1px;
}
/* Per-level heart colors */
.xp-lv-1 .xp-heart-icon { background: linear-gradient(135deg, #9ca3af, #d1d5db); -webkit-background-clip: text; background-clip: text; filter: drop-shadow(0 0 8px rgba(156,163,175,0.3)); }
.xp-lv-2 .xp-heart-icon { background: linear-gradient(135deg, #60a5fa, #93c5fd); -webkit-background-clip: text; background-clip: text; filter: drop-shadow(0 0 10px rgba(96,165,250,0.4)); }
.xp-lv-3 .xp-heart-icon { background: linear-gradient(135deg, #f472b6, #c084fc); -webkit-background-clip: text; background-clip: text; filter: drop-shadow(0 0 12px rgba(244,114,182,0.4)); }
.xp-lv-4 .xp-heart-icon { background: linear-gradient(135deg, #fbbf24, #f59e0b); -webkit-background-clip: text; background-clip: text; filter: drop-shadow(0 0 14px rgba(251,191,36,0.5)); }
.xp-lv-5 .xp-heart-icon { background: linear-gradient(135deg, #f472b6, #c084fc, #60a5fa, #fbbf24); background-size: 300% 300%; animation: xpHeartPulse 2.4s ease-in-out infinite, xpBarShimmer 4s linear infinite; -webkit-background-clip: text; background-clip: text; filter: drop-shadow(0 0 18px rgba(244,114,182,0.6)); }
.xp-lv-6 .xp-heart-icon { background: linear-gradient(135deg, #ef4444, #f472b6, #c084fc, #818cf8, #60a5fa); background-size: 400% 400%; animation: xpHeartPulse 1.8s ease-in-out infinite, xpBarShimmer 3s linear infinite; -webkit-background-clip: text; background-clip: text; filter: drop-shadow(0 0 24px rgba(239,68,68,0.5)) drop-shadow(0 0 12px rgba(192,132,252,0.4)); }

.xp-hero-info { flex: 1; min-width: 0; }
.xp-hero-label { font-size: 18px; font-weight: 700; color: var(--moon); margin-bottom: 4px; }
.xp-hero-xp { font-size: 32px; font-weight: 800; font-family: 'JetBrains Mono', monospace; color: var(--dendro); line-height: 1.2; margin-bottom: 14px; }
.xp-hero-unit { font-size: 14px; font-weight: 600; color: var(--moon-dim); }

/* Progress bar */
.xp-bar-wrap { width: 100%; }
.xp-bar-track {
  height: 12px; background: rgba(127, 214, 80, 0.08);
  border-radius: 6px; overflow: hidden; position: relative;
  border: 1px solid rgba(127, 214, 80, 0.12);
}
.xp-bar-fill {
  height: 100%; border-radius: 6px; position: relative;
  background: linear-gradient(90deg, var(--dendro), #a3e635, #7fd650);
  background-size: 200% 100%;
  animation: xpBarShimmer 3s linear infinite;
  transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1);
}
.xp-bar-glow {
  position: absolute; right: 0; top: -2px; bottom: -2px; width: 20px;
  background: radial-gradient(ellipse at right, rgba(255,255,255,0.6), transparent);
  animation: xpGlow 1.5s ease-in-out infinite;
}
.xp-bar-label {
  display: flex; justify-content: space-between; margin-top: 6px;
  font-size: 11px; color: var(--moon-dim); font-family: 'JetBrains Mono', monospace;
}
.xp-max-label { color: var(--dendro); font-weight: 600; }

/* Attribute cards */
.xp-attrs-row {
  display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 16px;
}
.xp-attr-card { padding: 18px 20px; }
.xp-attr-header { display: flex; align-items: center; gap: 8px; margin-bottom: 14px; }
.xp-attr-icon { font-size: 18px; }
.xp-attr-title { font-size: 13px; font-weight: 600; color: var(--moon); }
.xp-attr-gauge { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
.xp-attr-bar {
  flex: 1; height: 8px; background: rgba(255,255,255,0.06);
  border-radius: 4px; overflow: hidden;
}
.xp-attr-fill {
  height: 100%; border-radius: 4px;
  transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1);
}
.xp-attr-fill--proactive { background: linear-gradient(90deg, #fbbf24, #f59e0b); }
.xp-attr-fill--emotion { background: linear-gradient(90deg, #c084fc, #a855f7); }
.xp-attr-pct {
  font-size: 16px; font-weight: 700; font-family: 'JetBrains Mono', monospace;
  color: var(--moon); min-width: 42px; text-align: right;
}
.xp-attr-desc {
  font-size: 11px; color: var(--moon-dim); line-height: 1.5;
  padding: 8px 10px; background: rgba(255,255,255,0.02);
  border-radius: 6px; border-left: 2px solid rgba(255,255,255,0.06);
}

/* Level staircase */
.xp-levels-card { padding: 18px 20px; margin-bottom: 16px; }
.xp-levels-card h4 { font-size: 13px; font-weight: 600; color: var(--dendro); margin-bottom: 16px; }
.xp-levels-track { display: flex; flex-direction: column; gap: 0; }
.xp-level-step {
  display: flex; align-items: center; gap: 14px;
  padding: 10px 0; position: relative;
}
.xp-level-dot {
  width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 14px;
  background: rgba(255,255,255,0.04); border: 2px solid rgba(255,255,255,0.08);
  color: var(--moon-dim);
  transition: background 0.3s, border-color 0.3s, color 0.3s;
}
.xp-level-active .xp-level-dot {
  background: rgba(220, 60, 80, 0.15); border-color: #dc3c50;
  color: #f472b6;
}
.xp-level-current .xp-level-dot {
  background: rgba(220, 60, 80, 0.25); border-color: #f472b6;
  box-shadow: 0 0 12px rgba(244, 114, 182, 0.3);
  animation: xpHeartPulse 2.4s ease-in-out infinite;
}
.xp-level-dot-empty {
  width: 8px; height: 8px; border-radius: 50%;
  background: rgba(255,255,255,0.12);
}
.xp-level-step-info { display: flex; flex-direction: column; gap: 2px; }
.xp-level-step-lv { font-size: 11px; font-weight: 700; font-family: 'JetBrains Mono', monospace; color: var(--dendro); }
.xp-level-step-name { font-size: 14px; font-weight: 600; color: var(--moon); }
.xp-level-step-xp { font-size: 11px; color: var(--moon-dim); font-family: 'JetBrains Mono', monospace; }
.xp-level-connector {
  position: absolute; left: 15px; top: 42px; bottom: -10px;
  width: 2px; background: rgba(255,255,255,0.06);
}
.xp-level-connector-done { background: rgba(220, 60, 80, 0.3); }

/* XP history */
.xp-history-card { padding: 18px 20px; margin-bottom: 16px; }
.xp-history-card h4 { font-size: 13px; font-weight: 600; color: var(--dendro); margin-bottom: 14px; }
.xp-history-list { display: flex; flex-direction: column; gap: 4px; }
.xp-history-item {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 12px; border-radius: 8px;
  transition: background 0.2s;
}
.xp-history-item:hover { background: rgba(255,255,255,0.03); }
.xp-history-icon { font-size: 18px; flex-shrink: 0; }
.xp-history-body { flex: 1; display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.xp-history-label { font-size: 13px; color: var(--moon); font-weight: 500; }
.xp-history-time { font-size: 11px; color: var(--moon-dim); font-family: 'JetBrains Mono', monospace; }
.xp-history-amount {
  font-size: 15px; font-family: 'JetBrains Mono', monospace; font-weight: 700;
  min-width: 48px; text-align: right; flex-shrink: 0;
}
.xp-history-amount.positive { color: #7fd650; }
.xp-history-amount.negative { color: #f87171; }
.xp-empty {
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  padding: 32px; text-align: center; color: var(--moon-dim); font-size: 13px;
}
.xp-empty-icon { font-size: 28px; opacity: 0.3; }

/* Milestones */
.xp-milestones-card { padding: 18px 20px; margin-bottom: 16px; }
.xp-milestones-card h4 { font-size: 13px; font-weight: 600; color: var(--dendro); margin-bottom: 14px; }
.xp-milestones-list { display: flex; flex-direction: column; gap: 8px; }
.xp-milestone-item { display: flex; align-items: center; gap: 10px; font-size: 13px; }
.xp-milestone-icon { font-size: 16px; color: #f472b6; }
.xp-milestone-name { flex: 1; color: var(--moon); font-weight: 500; }
.xp-milestone-date { font-size: 11px; color: var(--moon-dim); font-family: 'JetBrains Mono', monospace; }
</style>
