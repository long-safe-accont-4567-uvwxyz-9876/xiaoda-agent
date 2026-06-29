<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import {
  NTabs, NTabPane, NButton, NInput, NSlider, NTag, NPopconfirm,
  NCollapse, NCollapseItem, NModal, NForm, NFormItem, NSelect,
  NSpace, useMessage,
} from 'naive-ui'
import { get, post, del } from '../api'
import {
  createMemory, updateMemory, deleteMemory,
  createNote, updateNote, deleteNote,
  createLearning, updateLearning, deleteLearning,
  createInstinct, updateInstinct, deleteInstinct,
  createKnowledgeEntity, updateKnowledgeEntity, deleteKnowledgeEntity,
  createKnowledgeRelation, updateKnowledgeRelation, deleteKnowledgeRelation,
  listKnowledgeEntities, listKnowledgeRelations, getKnowledgeGraph,
} from '../api'
import { getWsClient } from '../api/ws'
import UniverseGraph from '../components/knowledge/UniverseGraph.vue'
import { renderMarkdown } from '../utils/markdown'
import * as echarts from 'echarts/core'
import { LineChart, PieChart, GraphChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { t } from '../i18n'

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
const graphEntity = ref(t('insightView.graphEntityPh'))
const graphDepth = ref<1 | 2>(1)
const showUniverse = ref(false)
const activeTab = ref('emotion')
let knowledgeChart: echarts.ECharts | null = null
const kgEntities = ref<any[]>([])
const kgRelations = ref<any[]>([])
const notes = ref<any[]>([])
const learnings = ref<any[]>([])
const instincts = ref<any[]>([])

// ── CRUD 模态框 ──
type ModalType = 'memory' | 'note' | 'learning' | 'instinct' | 'entity' | 'relation' | null
const showModal = ref(false)
const modalType = ref<ModalType>(null)
const editingId = ref<number | string | null>(null)
const formModel = reactive<Record<string, any>>({})

const noteKindOptions = [
  { label: t('insightView.noteLabel'), value: 'note' },
  { label: t('insightView.taskLabel'), value: 'task' },
  { label: t('insightView.ideaLabel'), value: 'idea' },
]
const priorityOptions = [
  { label: t('insightView.impLow'), value: 'low' },
  { label: t('insightView.impMed'), value: 'medium' },
  { label: t('insightView.impHigh'), value: 'high' },
]

function openAddModal(type: ModalType) {
  modalType.value = type
  editingId.value = null
  Object.keys(formModel).forEach(k => delete formModel[k])
  if (type === 'memory') {
    formModel.summary = ''
    formModel.importance = 0.5
    formModel.emotion_label = ''
  } else if (type === 'note') {
    formModel.content = ''
    formModel.kind = 'note'
    formModel.tags = ''
  } else if (type === 'learning') {
    formModel.summary = ''
    formModel.pattern = ''
    formModel.priority = 'medium'
  } else if (type === 'instinct') {
    formModel.content = ''
    formModel.confidence = 0.5
  } else if (type === 'entity') {
    formModel.name = ''
    formModel.kind = ''
    formModel.observations = ''
  } else if (type === 'relation') {
    formModel.from = ''
    formModel.to = ''
    formModel.relation = ''
  }
  showModal.value = true
}

function openEditModal(type: ModalType, item: any) {
  modalType.value = type
  editingId.value = item.id
  Object.keys(formModel).forEach(k => delete formModel[k])
  if (type === 'memory') {
    formModel.summary = item.summary || ''
    formModel.importance = item.importance ?? 0.5
    formModel.emotion_label = item.emotion_label || ''
  } else if (type === 'note') {
    formModel.content = item.content || ''
    formModel.kind = item.kind || 'note'
    formModel.tags = item.tags || ''
  } else if (type === 'learning') {
    formModel.summary = item.summary || ''
    formModel.pattern = item.pattern || ''
    formModel.priority = item.priority || 'medium'
  } else if (type === 'instinct') {
    formModel.content = item.content || item.summary || ''
    formModel.confidence = item.confidence ?? 0.5
  } else if (type === 'entity') {
    editingId.value = item.name
    formModel.name = item.name || ''
    formModel.kind = item.kind || ''
    formModel.observations = item.observations || ''
  } else if (type === 'relation') {
    editingId.value = item.id
    formModel.from = item.from_entity || ''
    formModel.to = item.to_entity || ''
    formModel.relation = item.relation_type || ''
  }
  showModal.value = true
}

async function handleModalOk() {
  try {
    if (modalType.value === 'memory') {
      if (!formModel.summary) { message.warning(t('insightView.inputMemorySummary')); return }
      if (editingId.value) {
        await updateMemory(editingId.value as number, { summary: formModel.summary, importance: formModel.importance, emotion_label: formModel.emotion_label })
      } else {
        await createMemory({ summary: formModel.summary, importance: formModel.importance, emotion_label: formModel.emotion_label })
      }
      await loadMemories()
    } else if (modalType.value === 'note') {
      if (!formModel.content) { message.warning(t('insightView.inputNoteContent')); return }
      if (editingId.value) {
        await updateNote(editingId.value as number, { content: formModel.content, kind: formModel.kind, tags: formModel.tags })
      } else {
        await createNote({ content: formModel.content, kind: formModel.kind, tags: formModel.tags })
      }
      await loadNotes()
    } else if (modalType.value === 'learning') {
      if (!formModel.summary) { message.warning(t('insightView.inputLearningSummary')); return }
      if (editingId.value) {
        await updateLearning(editingId.value as number, { summary: formModel.summary, pattern: formModel.pattern, priority: formModel.priority })
      } else {
        await createLearning({ summary: formModel.summary, pattern: formModel.pattern, priority: formModel.priority })
      }
      await loadLearning()
    } else if (modalType.value === 'instinct') {
      if (!formModel.content) { message.warning(t('insightView.inputInstinctContent')); return }
      if (editingId.value) {
        await updateInstinct(editingId.value as number, { content: formModel.content, confidence: formModel.confidence })
      } else {
        await createInstinct({ content: formModel.content, confidence: formModel.confidence })
      }
      await loadLearning()
    } else if (modalType.value === 'entity') {
      if (!formModel.name) { message.warning(t('insightView.inputEntityName')); return }
      if (editingId.value) {
        await updateKnowledgeEntity(editingId.value as string, { kind: formModel.kind, observations: formModel.observations })
      } else {
        await createKnowledgeEntity({ name: formModel.name, kind: formModel.kind, observations: formModel.observations })
      }
      await loadKnowledgeData()
    } else if (modalType.value === 'relation') {
      if (!formModel.from || !formModel.to || !formModel.relation) { message.warning(t('insightView.inputRelationInfo')); return }
      if (editingId.value) {
        await updateKnowledgeRelation(editingId.value as string, { relation: formModel.relation })
      } else {
        await createKnowledgeRelation({ from: formModel.from, to: formModel.to, relation: formModel.relation })
      }
      await loadKnowledgeData()
    }
    showModal.value = false
    message.success(editingId.value ? t('insightView.updated') : t('insightView.created'))
  } catch (e: any) {
    message.error(e.message)
  }
}

const modalTitle = () => {
  const prefix = editingId.value ? t('insightView.editPrefix') : t('insightView.addPrefix')
  const names: Record<string, string> = { memory: t('insightView.namesMemory'), note: t('insightView.namesNote'), learning: t('insightView.namesLearning'), instinct: t('insightView.namesInstinct'), entity: t('insightView.namesEntity'), relation: t('insightView.namesRelation') }
  return prefix + (names[modalType.value || ''] || '')
}

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
  ws.on('knowledge_graph_changed', loadKnowledgeData)
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  if (resizeTimer) clearTimeout(resizeTimer)
  knowledgeChart?.dispose()
})

let resizeTimer: ReturnType<typeof setTimeout> | null = null
function handleResize() {
  if (resizeTimer) clearTimeout(resizeTimer)
  resizeTimer = setTimeout(() => {
    knowledgeChart?.resize()
  }, 200)
}

watch(activeTab, async (tab) => {
  if (tab === 'knowledge') {
    await nextTick()
    // 延迟等待 tab 动画完成后再初始化
    setTimeout(async () => {
      await loadKnowledgeData()
    }, 100)
  }
})

function onConsolidated(e: any) {
  consolidating.value = false
  if (e.ok) {
    message.success(t('insightView.consolidateDone'))
    loadPortrait()
  } else {
    message.error(`${t('insightView.consolidateFailed')}: ${e.error || t('insightView.unknownError')}`)
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
    message.info(t('insightView.consolidateStarted'))
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
    await deleteMemory(id)
    memories.value = memories.value.filter(m => m.id !== id)
    message.success(t('insightView.memoryDeleted'))
  } catch (e: any) { message.error(e.message) }
}

async function removeNote(id: number) {
  try {
    await deleteNote(id)
    notes.value = notes.value.filter(n => n.id !== id)
    message.success(t('insightView.noteArchived'))
  } catch (e: any) { message.error(e.message) }
}

async function removeLearning(id: number) {
  try {
    await deleteLearning(id)
    learnings.value = learnings.value.filter(l => l.id !== id)
    message.success(t('insightView.learningDeleted'))
  } catch (e: any) { message.error(e.message) }
}

async function removeInstinct(id: number) {
  try {
    await deleteInstinct(id)
    instincts.value = instincts.value.filter(i => i.id !== id)
    message.success(t('insightView.instinctDeleted'))
  } catch (e: any) { message.error(e.message) }
}

async function loadKnowledge() {
  try {
    const data = await getKnowledgeGraph(graphEntity.value, graphDepth.value)
    await nextTick()
    if (!graphEl.value) return
    if (knowledgeChart) { knowledgeChart.dispose() }
    knowledgeChart = echarts.init(graphEl.value)

    // 按节点对分组，计算每条边的独立曲率，避免重叠
    const pairKey = (a: string, b: string) => [a, b].sort().join('||')
    const pairCount: Record<string, number> = {}
    const pairIdx: Record<string, number> = {}
    for (const e of data.edges) {
      const k = pairKey(e.from, e.to)
      pairCount[k] = (pairCount[k] || 0) + 1
    }

    const links = data.edges.map((e: any) => {
      const k = pairKey(e.from, e.to)
      const total = pairCount[k]
      const idx = pairIdx[k] || 0
      pairIdx[k] = idx + 1
      // 单条边用小曲率，多条边均匀展开
      let curveness: number
      if (total === 1) {
        curveness = 0.1
      } else {
        // 均匀分布在 -0.4 ~ 0.4 之间
        curveness = -0.4 + (idx / (total - 1)) * 0.8
      }
      return {
        source: e.from,
        target: e.to,
        relation: e.relation,
        lineStyle: { curveness },
      }
    })

    // 力导向布局，拖拽后固定
    const nodeData = data.nodes.map((n: any) => ({
      name: n.name,
      value: n.kind,
      symbolSize: 26,
    }))

    knowledgeChart.setOption({
      tooltip: {
        triggerOn: 'click',
        formatter: (p: any) => {
          if (p.dataType === 'node') {
            return `<b>${p.data.name}</b><br/>${t('insightView.typeName')} ${p.data.value || ''}`
          }
          if (p.dataType === 'edge') {
            return `${p.data.source} → <b>${p.data.relation}</b> → ${p.data.target}`
          }
          return ''
        },
      },
      series: [{
        type: 'graph', layout: 'force', roam: true, draggable: true,
        force: { repulsion: 260, edgeLength: 120, gravity: 0.08, friction: 0.32 },
        label: { show: true, color: '#f2f7ee', fontSize: 11 },
        edgeLabel: {
          show: true, fontSize: 9, color: '#e8d5a3',
          formatter: (p: any) => p.data.relation || '',
        },
        itemStyle: { color: '#7fd650' },
        lineStyle: { color: 'rgba(232, 213, 163, 0.5)' },
        emphasis: { disabled: true },
        select: {
          focus: 'adjacency',
          lineStyle: { width: 3, color: '#fbbf24' },
          label: { fontSize: 14 },
          itemStyle: { shadowBlur: 10, shadowColor: '#fbbf24' },
        },
        data: nodeData,
        links,
      }],
    })

    // 拖拽松手后固定节点，不弹回
    knowledgeChart.on('mouseup', (params: any) => {
      if (params.dataType === 'node' && params.data) {
        const opt = knowledgeChart?.getOption() as any
        if (opt?.series?.[0]?.data) {
          const sData = opt.series[0].data.map((d: any) => {
            if (d.name === params.data.name) {
              return { ...d, fixed: true, x: params.data.x, y: params.data.y }
            }
            return d
          })
          knowledgeChart?.setOption({ series: [{ data: sData }] }, false)
        }
      }
    })
  } catch (e: any) { message.error(e.message) }
}

async function loadKnowledgeData() {
  try {
    const [ents, rels] = await Promise.all([listKnowledgeEntities(), listKnowledgeRelations()])
    kgEntities.value = ents || []
    kgRelations.value = rels || []
    await loadKnowledge()
  } catch (e: any) { message.error(e.message) }
}

async function removeKgEntity(name: string) {
  try {
    await deleteKnowledgeEntity(name)
    kgEntities.value = kgEntities.value.filter(e => e.name !== name)
    kgRelations.value = kgRelations.value.filter(r => r.from_entity !== name && r.to_entity !== name)
    message.success(t('insightView.entityDeleted'))
    await loadKnowledge()
  } catch (e: any) { message.error(e.message) }
}

async function removeKgRelation(id: string) {
  try {
    await deleteKnowledgeRelation(id)
    kgRelations.value = kgRelations.value.filter(r => String(r.id) !== id)
    message.success(t('insightView.relationDeleted'))
    await loadKnowledge()
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
    <h2 class="view-title">🌱 {{ t('insightView.title') }}</h2>
    <n-tabs type="line" animated v-model:value="activeTab">
      <n-tab-pane name="emotion" :tab="t('insightView.emotion')">
        <div class="emotion-current glass-panel">
          <span class="emo-big" :style="{ color: EMOTION_COLORS[currentEmotion.primary] }">
            {{ currentEmotion.primary || t('insightView.calm') }}
          </span>
          <span class="emo-sub">{{ t('insightView.lastEmotionDesc') }}</span>
        </div>
        <div class="chart-row">
          <div class="glass-panel chart-box">
            <h4>{{ t('insightView.emotionRiver7d') }}</h4>
            <div ref="emotionChartEl" class="chart"></div>
          </div>
          <div class="glass-panel chart-box small">
            <h4>{{ t('insightView.todayDist') }}</h4>
            <div ref="pieChartEl" class="chart"></div>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="portrait" :tab="t('insightView.profile')">
        <div class="portrait-head">
          <span v-if="portrait.version">{{ t('insightView.versionLabel') }} v{{ portrait.version }} ·
            {{ new Date((portrait.created_at || 0) * 1000).toLocaleString('zh-CN') }}</span>
          <n-button size="small" type="primary" :loading="consolidating" @click="consolidate">
            {{ t('insightView.consolidateBtn') }}
          </n-button>
        </div>
        <div class="glass-panel portrait-card md-body"
             v-html="renderMarkdown(portrait.content || t('insightView.noPortrait'))"></div>
        <n-collapse style="margin-top: 12px">
          <n-collapse-item :title="t('insightView.changeLog')" name="log">
            <div v-for="h in portraitHistory" :key="h.version" class="history-row">
              <n-tag size="small" :bordered="false">v{{ h.version }}</n-tag>
              <span class="history-log">{{ h.change_log || t('insightView.noDesc') }}</span>
              <span class="history-time">{{ new Date(h.created_at * 1000).toLocaleString('zh-CN') }}</span>
            </div>
          </n-collapse-item>
        </n-collapse>
      </n-tab-pane>

      <n-tab-pane name="today" :tab="t('insightView.todayEvents')">
        <div class="today-stats glass-panel">
          {{ t('insightView.todayRounds') }} {{ todayData.stats.conversations || 0 }} {{ t('insightView.roundsUnit') }} ·
          {{ t('insightView.toolCalls') }} {{ todayData.stats.tool_calls || 0 }} {{ t('insightView.times') }} ·
          {{ t('insightView.newMemories') }} {{ todayData.stats.memories || 0 }} {{ t('insightView.itemsUnit') }}
        </div>
        <div class="timeline">
          <div v-for="(item, i) in todayData.items" :key="i" class="timeline-item">
            <span class="tl-time">{{ fmtTs(item.ts) }}</span>
            <span class="tl-icon">{{ kindIcon[item.kind] || '·' }}</span>
            <span class="tl-text">{{ item.text || item.event_type }}</span>
          </div>
          <div v-if="!todayData.items.length" class="empty-state">
            <p>{{ t('insightView.noEvents') }}</p>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="memory" :tab="t('insightView.memory')">
        <div class="mem-toolbar glass-panel">
          <n-button size="small" type="primary" @click="openAddModal('memory')">+ {{ t('insightView.add') }}</n-button>
          <n-input v-model:value="memQuery" :placeholder="t('insightView.searchMemoryPh')" clearable
                   style="max-width: 280px" @keydown.enter="loadMemories" />
          <label class="slider-label">
            {{ t('insightView.importanceMin') }} ≥ {{ importanceMin.toFixed(1) }}
            <n-slider v-model:value="importanceMin" :min="0" :max="1" :step="0.1"
                      style="width: 140px" @update:value="loadMemories" />
          </label>
          <n-button size="small" @click="loadMemories">{{ t('insightView.searchBtn') }}</n-button>
        </div>
        <div class="mem-list">
          <div v-for="m in memories" :key="m.id" class="mem-row glass-panel">
            <div class="mem-main">
              <span class="mem-summary">{{ m.summary }}</span>
              <div class="mem-meta">
                <span>{{ '★'.repeat(Math.round((m.importance || 0) * 5)) || '☆' }}</span>
                <n-tag v-if="m.emotion_label" size="tiny" :bordered="false">{{ m.emotion_label }}</n-tag>
                <span>{{ new Date(m.timestamp * 1000).toLocaleString('zh-CN') }}</span>
                <n-tag v-if="m.via === 'vector'" size="tiny" type="info" :bordered="false">{{ t('insightView.semanticHit') }}</n-tag>
              </div>
            </div>
            <n-button size="tiny" quaternary @click="openEditModal('memory', m)">{{ t('insightView.edit') }}</n-button>
            <n-popconfirm @positive-click="removeMemory(m.id)">
              <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
              {{ t('insightView.deleteMemoryConfirm') }}
            </n-popconfirm>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="knowledge" :tab="t('insightView.knowledgeGraph')">
        <div class="glass-panel chart-box">
          <div class="kg-toolbar">
            <n-input v-model:value="graphEntity" :placeholder="t('insightView.entityFocusPh')" size="small"
                     style="max-width: 200px" @keydown.enter="loadKnowledgeData" />
            <n-button size="tiny" :type="graphDepth === 1 ? 'primary' : 'default'"
                      @click="graphDepth = 1; loadKnowledgeData()">{{ t('insightView.depth1') }}</n-button>
            <n-button size="tiny" :type="graphDepth === 2 ? 'primary' : 'default'"
                      @click="graphDepth = 2; loadKnowledgeData()">{{ t('insightView.depth2') }}</n-button>
            <n-button size="tiny" type="primary" @click="openAddModal('entity')">{{ t('insightView.addEntity') }}</n-button>
            <n-button size="tiny" type="primary" @click="openAddModal('relation')">{{ t('insightView.addRelation') }}</n-button>
            <n-button size="tiny" type="primary" @click="showUniverse = true">{{ t('insightView.fullscreen') }}</n-button>
          </div>
          <div ref="graphEl" class="chart tall"></div>
        </div>
        <div class="kg-lists">
          <div class="kg-section">
            <h4>{{ t('insightView.entitiesLabel') }} ({{ kgEntities.length }})</h4>
            <div class="item-list">
              <div v-for="e in kgEntities" :key="e.name" class="list-row glass-panel">
                <n-tag size="tiny" :bordered="false" v-if="e.kind">{{ e.kind }}</n-tag>
                <span class="note-content">{{ e.name }}</span>
                <n-button size="tiny" quaternary @click="openEditModal('entity', e)">{{ t('insightView.edit') }}</n-button>
                <n-popconfirm @positive-click="removeKgEntity(e.name)">
                  <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
                  {{ t('insightView.deleteEntityConfirm') }}
                </n-popconfirm>
              </div>
              <div v-if="!kgEntities.length" class="empty-state"><p>{{ t('insightView.noEntities') }}</p></div>
            </div>
          </div>
          <div class="kg-section">
            <h4>{{ t('insightView.relationsLabel') }} ({{ kgRelations.length }})</h4>
            <div class="item-list">
              <div v-for="r in kgRelations" :key="r.id" class="list-row glass-panel">
                <span class="kg-rel-from">{{ r.from_entity }}</span>
                <n-tag size="tiny" type="info" :bordered="false">{{ r.relation_type }}</n-tag>
                <span class="kg-rel-to">{{ r.to_entity }}</span>
                <n-button size="tiny" quaternary @click="openEditModal('relation', r)">{{ t('insightView.edit') }}</n-button>
                <n-popconfirm @positive-click="removeKgRelation(r.id)">
                  <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
                  {{ t('insightView.deleteRelationConfirm') }}
                </n-popconfirm>
              </div>
              <div v-if="!kgRelations.length" class="empty-state"><p>{{ t('insightView.noRelations') }}</p></div>
            </div>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="notes" :tab="t('insightView.notes')">
        <div class="tab-toolbar glass-panel">
          <n-button size="small" type="primary" @click="openAddModal('note')">+ {{ t('insightView.addNote') }}</n-button>
        </div>
        <div class="item-list">
          <div v-for="n in notes" :key="n.id" class="list-row glass-panel">
            <n-tag size="tiny" :bordered="false">{{ n.kind }}</n-tag>
            <span class="note-content">{{ n.content }}</span>
            <n-button size="tiny" quaternary @click="openEditModal('note', n)">{{ t('insightView.edit') }}</n-button>
            <n-popconfirm @positive-click="removeNote(n.id)">
              <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
              {{ t('insightView.archiveNoteConfirm') }}
            </n-popconfirm>
          </div>
        </div>
        <div v-if="!notes.length" class="empty-state"><p>{{ t('insightView.noNotes') }}</p></div>
      </n-tab-pane>

      <n-tab-pane name="learnings" :tab="t('insightView.learning')">
        <div class="tab-toolbar glass-panel">
          <n-button size="small" type="primary" @click="openAddModal('learning')">+ {{ t('insightView.addLearning') }}</n-button>
        </div>
        <div class="item-list">
          <div v-for="l in learnings" :key="l.id" class="list-row glass-panel">
            <n-tag size="tiny" :type="l.priority === 'high' ? 'error' : l.priority === 'medium' ? 'warning' : 'default'"
                   :bordered="false">{{ l.priority }}</n-tag>
            <span class="note-content">{{ l.summary }}</span>
            <span class="note-extra">× {{ l.recurrence_count }}</span>
            <n-button size="tiny" quaternary @click="openEditModal('learning', l)">{{ t('insightView.edit') }}</n-button>
            <n-popconfirm @positive-click="removeLearning(l.id)">
              <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
              {{ t('insightView.deleteLearningConfirm') }}
            </n-popconfirm>
          </div>
        </div>
        <div v-if="!learnings.length" class="empty-state"><p>{{ t('insightView.noLearning') }}</p></div>
      </n-tab-pane>

      <n-tab-pane name="instincts" :tab="t('insightView.instinct')">
        <div class="tab-toolbar glass-panel">
          <n-button size="small" type="primary" @click="openAddModal('instinct')">+ {{ t('insightView.addInstinct') }}</n-button>
        </div>
        <div class="item-list">
          <div v-for="ins in instincts" :key="ins.id" class="list-row glass-panel">
            <span class="note-content">{{ ins.content || ins.summary || ins.trigger_pattern }}</span>
            <span class="note-extra">{{ t('insightView.confidence') }} {{ ((ins.confidence || 0) * 100).toFixed(0) }}%</span>
            <n-button size="tiny" quaternary @click="openEditModal('instinct', ins)">{{ t('insightView.edit') }}</n-button>
            <n-popconfirm @positive-click="removeInstinct(ins.id)">
              <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
              {{ t('insightView.deleteInstinctConfirm') }}
            </n-popconfirm>
          </div>
        </div>
        <div v-if="!instincts.length" class="empty-state"><p>{{ t('insightView.noInstinct') }}</p></div>
      </n-tab-pane>
    </n-tabs>

    <!-- 共享 CRUD 模态框 -->
    <n-modal v-model:show="showModal" preset="card" :title="modalTitle()" style="max-width: 480px">
      <!-- 记忆表单 -->
      <n-form v-if="modalType === 'memory'" label-placement="left" label-width="70">
        <n-form-item :label="t('insightView.labelSummary')">
          <n-input v-model:value="formModel.summary" type="textarea" :placeholder="t('insightView.memoryContentPh')" :rows="3" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelImportance')">
          <n-slider v-model:value="formModel.importance" :min="0" :max="1" :step="0.1" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelEmotionTag')">
          <n-input v-model:value="formModel.emotion_label" :placeholder="t('insightView.emotionTagPh')" />
        </n-form-item>
      </n-form>
      <!-- 笔记表单 -->
      <n-form v-if="modalType === 'note'" label-placement="left" label-width="70">
        <n-form-item :label="t('insightView.labelContent')">
          <n-input v-model:value="formModel.content" type="textarea" :placeholder="t('insightView.noteContentPh')" :rows="4" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelType')">
          <n-select v-model:value="formModel.kind" :options="noteKindOptions" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelTags')">
          <n-input v-model:value="formModel.tags" :placeholder="t('insightView.tagsPh')" />
        </n-form-item>
      </n-form>
      <!-- 学习记录表单 -->
      <n-form v-if="modalType === 'learning'" label-placement="left" label-width="70">
        <n-form-item :label="t('insightView.labelSummary')">
          <n-input v-model:value="formModel.summary" type="textarea" :placeholder="t('insightView.learningSummaryPh')" :rows="3" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelMode')">
          <n-input v-model:value="formModel.pattern" :placeholder="t('insightView.modePh')" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelPriority')">
          <n-select v-model:value="formModel.priority" :options="priorityOptions" />
        </n-form-item>
      </n-form>
      <!-- 本能表单 -->
      <n-form v-if="modalType === 'instinct'" label-placement="left" label-width="70">
        <n-form-item :label="t('insightView.labelContent')">
          <n-input v-model:value="formModel.content" type="textarea" :placeholder="t('insightView.instinctContentPh')" :rows="3" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelConfidence')">
          <n-slider v-model:value="formModel.confidence" :min="0" :max="1" :step="0.1" />
        </n-form-item>
      </n-form>
      <!-- 实体表单 -->
      <n-form v-if="modalType === 'entity'" label-placement="left" label-width="70">
        <n-form-item :label="t('insightView.labelName')">
          <n-input v-model:value="formModel.name" :placeholder="t('insightView.entityNamePh')" :disabled="!!editingId" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelType')">
          <n-input v-model:value="formModel.kind" :placeholder="t('insightView.entityTypePh')" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelDesc')">
          <n-input v-model:value="formModel.observations" type="textarea" :placeholder="t('insightView.entityDescPh')" :rows="3" />
        </n-form-item>
      </n-form>
      <!-- 关系表单 -->
      <n-form v-if="modalType === 'relation'" label-placement="left" label-width="70">
        <n-form-item :label="t('insightView.labelStartEntity')">
          <n-input v-model:value="formModel.from" :placeholder="t('insightView.startEntityPh')" :disabled="!!editingId" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelRelation')">
          <n-input v-model:value="formModel.relation" :placeholder="t('insightView.relationPh')" />
        </n-form-item>
        <n-form-item :label="t('insightView.labelEndEntity')">
          <n-input v-model:value="formModel.to" :placeholder="t('insightView.endEntityPh')" :disabled="!!editingId" />
        </n-form-item>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button @click="showModal = false">{{ t('cancel') }}</n-button>
          <n-button type="primary" @click="handleModalOk">{{ t('ok') }}</n-button>
        </n-space>
      </template>
    </n-modal>

    <!-- 纳西妲宇宙 3D 全屏图谱 -->
    <n-modal
      v-model:show="showUniverse"
      :trap-focus="false"
      :close-on-esc="true"
      :mask-closable="true"
      :show-mask="false"
      display-directive="show"
      style="width:100vw;height:100vh;max-width:none;max-height:none"
    >
      <UniverseGraph :entity="graphEntity" :depth="graphDepth" @close="showUniverse = false" />
    </n-modal>
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

.tab-toolbar { display: flex; align-items: center; gap: 10px; padding: 10px 14px; margin-bottom: 10px; }
.item-list { display: flex; flex-direction: column; gap: 6px; }
.list-row { display: flex; align-items: center; gap: 10px; padding: 8px 14px; font-size: 13px; }

.empty-state { padding: 30px; text-align: center; color: var(--moon-dim); }

:deep(.md-body p) { margin-bottom: 8px; }
:deep(.md-body h1), :deep(.md-body h2), :deep(.md-body h3) {
  color: var(--dendro); margin: 10px 0 6px; font-size: 16px;
}
:deep(.md-body ul) { padding-left: 20px; }

.kg-lists { display: flex; gap: 14px; margin-top: 14px; flex-wrap: wrap; }
.kg-section { flex: 1; min-width: 300px; }
.kg-section h4 { font-size: 13px; color: var(--dendro); margin-bottom: 8px; }
.kg-rel-from, .kg-rel-to { font-size: 12px; color: var(--moon); }
.kg-rel-from::after { content: ' →'; color: var(--wisdom); margin: 0 4px; }
</style>
