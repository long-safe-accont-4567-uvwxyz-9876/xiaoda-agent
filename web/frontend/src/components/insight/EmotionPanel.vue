<script setup lang="ts">
/**
 * 情绪 Tab：当前情绪卡 + 7 日情绪河流折线 + 今日情绪分布饼图。
 * 数据经 props 注入（useInsightEmotion 在视图层调用）；echarts 渲染与
 * resize 监听在本组件内（原 InsightView renderEmotionCharts 原样迁移）。
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart, PieChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import { EMOTION_COLORS } from '../../composables/useInsightEmotion'
import type { EmotionCurrent, EmotionHistoryRow } from './types'

echarts.use([LineChart, PieChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const props = defineProps<{
  currentEmotion: EmotionCurrent
  history: EmotionHistoryRow[]
}>()

const emotionChartEl = ref<HTMLElement | null>(null)
const pieChartEl = ref<HTMLElement | null>(null)
let emotionChart: echarts.ECharts | null = null
let pieChart: echarts.ECharts | null = null

function renderCharts(history: EmotionHistoryRow[]) {
  if (emotionChartEl.value) {
    const hours = [...new Set(history.map(h => h.hour))].sort()
    const emotions = [...new Set(history.map(h => h.emotion_label))]
    if (!emotionChart) emotionChart = echarts.init(emotionChartEl.value)
    emotionChart.setOption({
      tooltip: { trigger: 'axis' },
      legend: { textStyle: { color: '#f2f7ee' }, type: 'scroll' },
      grid: { left: 40, right: 16, top: 40, bottom: 40 },
      xAxis: { type: 'category', data: hours, axisLabel: { color: '#9ca3af', fontSize: 10 } },
      yAxis: { type: 'value', axisLabel: { color: '#9ca3af' }, splitLine: { lineStyle: { color: 'rgba(127,214,80,.08)' } } },
      series: emotions.map(e => ({
        name: e, type: 'line', smooth: true, stack: 'total', areaStyle: { opacity: 0.4 },
        color: EMOTION_COLORS[e],
        data: hours.map(h => history.find(x => x.hour === h && x.emotion_label === e)?.cnt || 0),
      })),
    })
  }
  if (pieChartEl.value) {
    const today = new Date().toISOString().slice(0, 10)
    const todayRows = history.filter(h => h.hour.startsWith(today))
    const byEmotion: Record<string, number> = {}
    for (const r of todayRows) byEmotion[r.emotion_label] = (byEmotion[r.emotion_label] || 0) + r.cnt
    if (!pieChart) pieChart = echarts.init(pieChartEl.value)
    pieChart.setOption({
      tooltip: {},
      series: [{
        type: 'pie', radius: ['38%', '68%'],
        label: { color: '#f2f7ee', fontSize: 11 },
        data: Object.entries(byEmotion).map(([name, value]) => ({
          name, value, itemStyle: { color: EMOTION_COLORS[name] },
        })),
      }],
    })
  }
}

// 数据到位后渲染（原 loadEmotion 的 await nextTick 时序保持）
watch(() => props.history, async (history) => {
  await nextTick()
  renderCharts(history || [])
})

let resizeTimer: ReturnType<typeof setTimeout> | null = null
function handleResize() {
  if (resizeTimer) clearTimeout(resizeTimer)
  resizeTimer = setTimeout(() => {
    emotionChart?.resize()
    pieChart?.resize()
  }, 200)
}

onMounted(() => window.addEventListener('resize', handleResize))
onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  if (resizeTimer) clearTimeout(resizeTimer)
  emotionChart?.dispose(); emotionChart = null
  pieChart?.dispose(); pieChart = null
})
</script>

<template>
  <div>
    <Tilt3D :max-x="4" :max-y="6"><div class="emotion-current glass-panel">
      <span class="emo-big" :style="{ color: EMOTION_COLORS[currentEmotion.primary || ''] }">
        {{ currentEmotion.primary || t('insightView.calm') }}
      </span>
      <span class="emo-sub">{{ t('insightView.lastEmotionDesc') }}</span>
    </div></Tilt3D>
    <div class="chart-row">
      <Tilt3D :max-x="4" :max-y="6" class="glass-panel chart-box">
        <h4>{{ t('insightView.emotionRiver7d') }}</h4>
        <div ref="emotionChartEl" class="chart"></div>
      </Tilt3D>
      <Tilt3D :max-x="4" :max-y="6" class="glass-panel chart-box small">
        <h4>{{ t('insightView.todayDist') }}</h4>
        <div ref="pieChartEl" class="chart"></div>
      </Tilt3D>
    </div>
  </div>
</template>

<style scoped>
.emotion-current {
  display: flex; align-items: baseline; gap: 14px;
  padding: 18px 22px; margin-bottom: 14px;
}
.emo-big { font-size: 32px; font-weight: 700; font-family: 'Noto Serif SC', serif; }
.emo-sub { color: var(--moon-dim); font-size: 13px; }

.chart-row { display: flex; gap: 14px; flex-wrap: wrap; }
.chart-box { flex: 2; padding: 14px 16px; min-width: 300px; }
.chart-box.small { flex: 1; min-width: 240px; }
.chart-box h4 { font-size: 13px; color: var(--dendro); margin-bottom: 8px; }
.chart { height: 260px; }
</style>
