<script setup lang="ts">
import { ref, onMounted, nextTick, watch } from 'vue'
import {
  NTabs, NTabPane, NButton, NInput, NSlider, NTag, NPopconfirm,
  NCollapse, NCollapseItem, useMessage,
} from 'naive-ui'
import { get, post, del } from '../api'
import { getWsClient } from '../api/ws'
import { renderMarkdown } from '../utils/markdown'
import * as echarts from 'echarts/core'
import { LineChart, PieChart, GraphChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([LineChart, PieChart, GraphChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const message = useMessage()
const ws = getWsClient()

// 情绪
const currentEmotion = ref<any>({})
const emotionChartEl = ref<HTMLElement | null>(null)
const pieChartEl = ref<HTMLElement | null>(null)

// 画像
const portrait = ref<any>({})
const portraitHistory = ref<any[]>([])
const consolidating = ref(false)

// 今日
const todayData = ref<any>({ items: [], stats: {} })

// 记忆与知识
const memories = ref<any[]>([])
const memQuery = ref('')
const importanceMin = ref(0)
const graphEl = ref<HTMLElement | null>(null)
const graphEntity = ref('用户')
const graphDepth = ref(1)
const activeTab = ref('emotion')
let knowledgeLoaded = false
const notes = ref<any[]>([])
const learnings = ref<any[]>([])
const instincts = ref<any[]>([])

const EMOTION_COLORS: Record<string, string> = {
  '喜悦': '#7fd650', '悲伤': '#60a5fa', '愤怒': '#f87171', '焦虑': '#fbbf24',
  '害羞': '#f9a8d4', '好奇': '#a78bfa', '思考': '#67e8f9', '恐惧': '#94a3b8', '平静': '#9ca3af',
}

onMounted(async () => {
  loadEmotion()
  loadPortrait()
  loadToday()
  loadMemories()
  loadNotes()
  loadLearning()
  ws.on('portrait_consolidated', onConsolidated)
})

watch(activeTab, async (tab) => {
  if (tab === 'memory' && !knowledgeLoaded) {
    knowledgeLoaded = true
    await nextTick()
    loadKnowledge()
  }
})

function onConsolidated(e: any) {
  consolidating.value = false
  if (e.ok) {
    message.success('画像整合完成 ✓')
    loadPortrait()
  } else {
    message.error(`整合失败：${e.error || '未知错误'}`)
  }
}

async function loadEmotion() {
  try {
    currentEmotion.value = await get('/insight/emotion/current')
    const history = await get<any[]>('/insight/emotion/history?days=7')
    await nextTick()
    renderEmotionCharts(history)
  } catch (e: any) { message.error(e.message) }
}

function renderEmotionCharts(history: any[]) {
  if (emotionChartEl.value) {
    const hours = [...new Set(history.map(h => h.hour))].sort()
    const emotions = [...new Set(history.map(h => h.emotion_label))]
    const chart = echarts.init(emotionChartEl.value)
    chart.setOption({
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
    const chart = echarts.init(pieChartEl.value)
    chart.setOption({
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

async function loadPortrait() {
  try {
    const data = await get('/insight/portrait')
    portrait.value = data.portrait || {}
    portraitHistory.value = data.history || []
  } catch (e: any) { message.error(e.message) }
}

async function consolidate() {
  consolidating.value = true
  try {
    await post('/insight/portrait/consolidate')
    message.info('整合已开始（完成后自动刷新）…')
  } catch (e: any) {
    consolidating.value = false
    message.error(e.message)
  }
}

async function loadToday() {
  try { todayData.value = await get('/insight/today') } catch (e: any) { message.error(e.message) }
}

async function loadMemories() {
  try {
    memories.value = await get<any[]>(
      `/insight/memories?q=${encodeURIComponent(memQuery.value)}&importance_min=${importanceMin.value}`)
  } catch (e: any) { message.error(e.message) }
}

async function removeMemory(id: number) {
  try {
    await del(`/insight/memories/${id}`, true)
    memories.value = memories.value.filter(m => m.id !== id)
    message.success('记忆已删除（含向量索引）')
  } catch (e: any) { message.error(e.message) }
}

async function loadKnowledge() {
  try {
    const data = await get(
      `/insight/knowledge/graph?entity=${encodeURIComponent(graphEntity.value)}&depth=${graphDepth.value}`)
    await nextTick()
    if (!graphEl.value) return
    const chart = echarts.init(graphEl.value)
    chart.setOption({
      tooltip: {},
      series: [{
        type: 'graph', layout: 'force', roam: true,
        force: { repulsion: 220, edgeLength: 110 },
        label: { show: true, color: '#f2f7ee', fontSize: 11 },
        edgeLabel: {
          show: true, fontSize: 9, color: '#e8d5a3',
          formatter: (p: any) => p.data.relation || '',
        },
        itemStyle: { color: '#7fd650' },
        lineStyle: { color: 'rgba(232, 213, 163, 0.5)' },
        data: data.nodes.map((n: any) => ({ name: n.name, value: n.kind, symbolSize: 26 })),
        links: data.edges.map((e: any) => ({ source: e.from, target: e.to, relation: e.relation })),
      }],
    })
  } catch (e: any) { message.error(e.message) }
}

async function loadNotes() {
  try { notes.value = await get<any[]>('/insight/notebook') } catch { /* */ }
}

async function loadLearning() {
  try {
    learnings.value = await get<any[]>('/insight/learnings')
    instincts.value = await get<any[]>('/insight/instincts')
  } catch { /* */ }
}

const kindIcon: Record<string, string> = {
  memory: '🌱', event: '⚙️', note: '📝', greeting: '💌',
}

function fmtTs(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
</script>

<template>
  <div class="insight-view">
    <h2 class="view-title">🌱 内在世界</h2>
    <n-tabs type="line" animated v-model:value="activeTab">
      <n-tab-pane name="emotion" tab="情绪">
        <div class="emotion-current glass-panel">
          <span class="emo-big" :style="{ color: EMOTION_COLORS[currentEmotion.primary] }">
            {{ currentEmotion.primary || '平静' }}
          </span>
          <span class="emo-sub">最近一次回复的情绪</span>
        </div>
        <div class="chart-row">
          <div class="glass-panel chart-box">
            <h4>7 天情绪河流</h4>
            <div ref="emotionChartEl" class="chart"></div>
          </div>
          <div class="glass-panel chart-box small">
            <h4>今日分布</h4>
            <div ref="pieChartEl" class="chart"></div>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="portrait" tab="认知 · 画像">
        <div class="portrait-head">
          <span v-if="portrait.version">版本 v{{ portrait.version }} ·
            {{ new Date((portrait.created_at || 0) * 1000).toLocaleString('zh-CN') }}</span>
          <n-button size="small" type="primary" :loading="consolidating" @click="consolidate">
            🔄 立即整合画像
          </n-button>
        </div>
        <div class="glass-panel portrait-card md-body"
             v-html="renderMarkdown(portrait.content || '（还没有形成画像呢～多和纳西妲聊聊吧）')"></div>
        <n-collapse style="margin-top: 12px">
          <n-collapse-item title="版本变更记录" name="log">
            <div v-for="h in portraitHistory" :key="h.version" class="history-row">
              <n-tag size="small" :bordered="false">v{{ h.version }}</n-tag>
              <span class="history-log">{{ h.change_log || '（无说明）' }}</span>
              <span class="history-time">{{ new Date(h.created_at * 1000).toLocaleString('zh-CN') }}</span>
            </div>
          </n-collapse-item>
        </n-collapse>
      </n-tab-pane>

      <n-tab-pane name="today" tab="今日事件">
        <div class="today-stats glass-panel">
          今天对话 {{ todayData.stats.conversations || 0 }} 轮 ·
          调用工具 {{ todayData.stats.tool_calls || 0 }} 次 ·
          新增记忆 {{ todayData.stats.memories || 0 }} 条
        </div>
        <div class="timeline">
          <div v-for="(item, i) in todayData.items" :key="i" class="timeline-item">
            <span class="tl-time">{{ fmtTs(item.ts) }}</span>
            <span class="tl-icon">{{ kindIcon[item.kind] || '·' }}</span>
            <span class="tl-text">{{ item.text || item.event_type }}</span>
          </div>
          <div v-if="!todayData.items.length" class="empty-state">
            <p>今天还没有发生事件呢～</p>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="memory" tab="记忆与知识">
        <div class="mem-toolbar glass-panel">
          <n-input v-model:value="memQuery" placeholder="语义搜索记忆…" clearable
                   style="max-width: 280px" @keydown.enter="loadMemories" />
          <label class="slider-label">
            重要度 ≥ {{ importanceMin.toFixed(1) }}
            <n-slider v-model:value="importanceMin" :min="0" :max="1" :step="0.1"
                      style="width: 140px" @update:value="loadMemories" />
          </label>
          <n-button size="small" @click="loadMemories">搜索</n-button>
        </div>
        <div class="mem-list">
          <div v-for="m in memories" :key="m.id" class="mem-row glass-panel">
            <div class="mem-main">
              <span class="mem-summary">{{ m.summary }}</span>
              <div class="mem-meta">
                <span>{{ '★'.repeat(Math.round((m.importance || 0) * 5)) || '☆' }}</span>
                <n-tag v-if="m.emotion_label" size="tiny" :bordered="false">{{ m.emotion_label }}</n-tag>
                <span>{{ new Date(m.timestamp * 1000).toLocaleString('zh-CN') }}</span>
                <n-tag v-if="m.via === 'vector'" size="tiny" type="info" :bordered="false">语义命中</n-tag>
              </div>
            </div>
            <n-popconfirm @positive-click="removeMemory(m.id)">
              <template #trigger><n-button size="tiny" type="error" quaternary>删</n-button></template>
              连带删除向量索引，不可恢复。确认？
            </n-popconfirm>
          </div>
        </div>

        <div class="glass-panel chart-box" style="margin-top: 14px">
          <div class="kg-toolbar">
            <h4>知识图谱</h4>
            <n-input v-model:value="graphEntity" placeholder="输入实体名聚焦…" size="small"
                     style="max-width: 200px" @keydown.enter="loadKnowledge" />
            <n-button size="tiny" :type="graphDepth === 1 ? 'primary' : 'default'"
                      @click="graphDepth = 1; loadKnowledge()">深度1</n-button>
            <n-button size="tiny" :type="graphDepth === 2 ? 'primary' : 'default'"
                      @click="graphDepth = 2; loadKnowledge()">深度2</n-button>
          </div>
          <div ref="graphEl" class="chart tall"></div>
        </div>

        <n-collapse style="margin-top: 14px">
          <n-collapse-item :title="`📝 笔记（${notes.length}）`" name="notes">
            <div v-for="n in notes" :key="n.id" class="note-row">
              <n-tag size="tiny" :bordered="false">{{ n.kind }}</n-tag>
              <span class="note-content">{{ n.content }}</span>
            </div>
          </n-collapse-item>
          <n-collapse-item :title="`🎓 学习记录（${learnings.length}）`" name="learnings">
            <div v-for="l in learnings" :key="l.id" class="note-row">
              <n-tag size="tiny" :type="l.priority === 'high' ? 'error' : l.priority === 'medium' ? 'warning' : 'default'"
                     :bordered="false">{{ l.priority }}</n-tag>
              <span class="note-content">{{ l.summary }}</span>
              <span class="note-extra">× {{ l.recurrence_count }}</span>
            </div>
          </n-collapse-item>
          <n-collapse-item :title="`⚡ 本能（${instincts.length}）`" name="instincts">
            <div v-for="ins in instincts" :key="ins.id" class="note-row">
              <span class="note-content">{{ ins.content || ins.summary || ins.trigger_pattern }}</span>
              <span class="note-extra">置信 {{ ((ins.confidence || 0) * 100).toFixed(0) }}%</span>
            </div>
          </n-collapse-item>
        </n-collapse>
      </n-tab-pane>
    </n-tabs>
  </div>
</template>

<style scoped>
.view-title { font-family: 'Noto Serif SC', serif; margin-bottom: 12px; }

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
.chart.tall { height: 380px; }

.portrait-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px; font-size: 13px; color: var(--moon-dim);
}
.portrait-card { padding: 18px 22px; line-height: 1.8; }

.history-row { display: flex; align-items: center; gap: 10px; padding: 4px 0; font-size: 13px; }
.history-log { flex: 1; color: var(--moon-dim); }
.history-time { font-size: 11px; color: var(--moon-dim); }

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

.mem-toolbar {
  display: flex; align-items: center; gap: 14px;
  padding: 12px 14px; margin-bottom: 12px; flex-wrap: wrap;
}
.slider-label { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--moon-dim); }

.mem-list { display: flex; flex-direction: column; gap: 8px; }
.mem-row { display: flex; align-items: center; gap: 12px; padding: 10px 14px; }
.mem-main { flex: 1; min-width: 0; }
.mem-summary { font-size: 13.5px; }
.mem-meta {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; color: var(--wisdom); margin-top: 4px;
}

.kg-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.kg-toolbar h4 { font-size: 13px; color: var(--dendro); margin-right: auto; }

.note-row { display: flex; align-items: center; gap: 10px; padding: 4px 0; font-size: 13px; }
.note-content { flex: 1; }
.note-extra { font-size: 11px; color: var(--moon-dim); }

.empty-state { padding: 30px; text-align: center; color: var(--moon-dim); }

:deep(.md-body p) { margin-bottom: 8px; }
:deep(.md-body h1), :deep(.md-body h2), :deep(.md-body h3) {
  color: var(--dendro); margin: 10px 0 6px; font-size: 16px;
}
:deep(.md-body ul) { padding-left: 20px; }
</style>
