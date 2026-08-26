<script setup lang="ts">
/**
 * Insight 视图（编排层）：各 Tab 的数据经 composables 加载，UI 块拆分至
 * components/insight/*Panel（2026-08-23 大文件拆分专项）。
 * 本文件保留：Tab 骨架、WS 订阅、知识图谱数据编排（搜索/展开/删除）、CRUD 编排。
 * 行为红线：onMounted 加载顺序、知识 Tab 懒加载时序（面板挂载触发）、
 * 确认弹窗与空态文案均与拆分前一致。
 */
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { NTabs, NTabPane, useMessage } from 'naive-ui'
import ViewTitleIcon from '../components/fx/ViewTitleIcon.vue'
import JSpacePanel from '../components/jspace/JSpacePanel.vue'
import EmotionPanel from '../components/insight/EmotionPanel.vue'
import PortraitPanel from '../components/insight/PortraitPanel.vue'
import TodayPanel from '../components/insight/TodayPanel.vue'
import MemoryPanel from '../components/insight/MemoryPanel.vue'
import KnowledgeGraphPanel from '../components/insight/KnowledgeGraphPanel.vue'
import NotesPanel from '../components/insight/NotesPanel.vue'
import LearningsPanel from '../components/insight/LearningsPanel.vue'
import InstinctsPanel from '../components/insight/InstinctsPanel.vue'
import XpPanel from '../components/insight/XpPanel.vue'
import XpLevelUpToast from '../components/insight/XpLevelUpToast.vue'
import CrudModal from '../components/insight/CrudModal.vue'
import { deleteKnowledgeEntity, deleteKnowledgeRelation, getKnowledgeGraph, listKnowledgeEntities, listKnowledgeRelations } from '../api'
import type { EntityItem, KgEdge2D, KgNode2D, RelationItem } from '../components/insight/types'
import { getWsClient, type WsEvent } from '../api/ws'
import { useKnowledgeGraphData } from '../composables/useKnowledgeGraphData'
import { useInsightEmotion } from '../composables/useInsightEmotion'
import { useInsightPortrait } from '../composables/useInsightPortrait'
import { useInsightToday } from '../composables/useInsightToday'
import { useInsightMemories } from '../composables/useInsightMemories'
import { useInsightNotes } from '../composables/useInsightNotes'
import { useInsightLearnings } from '../composables/useInsightLearnings'
import { useInsightXp } from '../composables/useInsightXp'
import { useInsightCrud } from '../composables/useInsightCrud'
import { t } from '../i18n'

const message = useMessage()
const ws = getWsClient()

// ── 各 Tab 数据（加载逻辑在 composables 内，行为原样）──
const { currentEmotion, history: emotionHistory, loadEmotion } = useInsightEmotion()
const { portrait, portraitHistory, consolidating, loadPortrait, consolidate, onConsolidated } = useInsightPortrait()
const { todayData, loadToday } = useInsightToday()
const { memories, memQuery, importanceMin, loadMemories, removeMemory } = useInsightMemories()
const { notes, loadNotes, removeNote } = useInsightNotes()
const { learnings, instincts, loadLearning, removeLearning, removeInstinct } = useInsightLearnings()
const { xpState, xpLevels, xpLevelUp, loadXpData, onXpLevelUp } = useInsightXp()

// ── 知识图谱数据编排 ──
const activeTab = ref('emotion')
const kgEntities = ref<EntityItem[]>([])
const kgRelations = ref<RelationItem[]>([])
const graphEntity = ref(t('insightView.graphEntityPh'))
// 图谱深度：1-5 自由调节（后端批量 BFS 任意深度 <70ms），不再固定两档
const graphDepth = ref<number>(1)
// 按需展开状态：已加载节点集合 + 展开中的节点（防重复请求）
const expandingNode = ref('')
// 增量图数据（搜索/展开合并后的累积结果）
// 数据层与 3D UniverseGraph 共享 useKnowledgeGraphData（2026-08-22 三可视化库专项）：
// 去重/degree 计算只此一份；echarts 节点形态 {name,value,kind} 经 makeNode 注入
const { nodes: kgNodes, edges: kgEdges, expandedIds: expandedNodes, reset: resetGraphAcc, merge: mergeGraphData, markExpanded } =
  useKnowledgeGraphData<KgNode2D, KgEdge2D>(
    (raw) => ({ name: String(raw.name), value: raw.kind, kind: raw.kind }),
    (raw) => raw,
  )

const knowledgePanel = ref<InstanceType<typeof KnowledgeGraphPanel> | null>(null)

function renderKnowledge() {
  // 面板未挂载（不在本 Tab）时跳过——与拆分前 graphEl 空守卫等价
  knowledgePanel.value?.render()
}

// ── 按需展开：拉取单节点 1 跳邻域合并进图 ──
async function expandNode(name: string) {
  if (expandingNode.value || expandedNodes.value.has(name)) return
  expandingNode.value = name
  try {
    const data = await getKnowledgeGraph(name, 1)
    mergeGraphData(data.nodes || [], data.edges || [])
    markExpanded(name)
    renderKnowledge()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    expandingNode.value = ''
  }
}

// 搜索/深度变化 → 重置累积图，从目标实体重新起步
async function resetAndLoadGraph(entity: string, depth: number) {
  resetGraphAcc()
  try {
    const data = await getKnowledgeGraph(entity, depth)
    mergeGraphData(data.nodes || [], data.edges || [])
    if (entity.trim()) markExpanded(entity.trim())
    renderKnowledge()
  } catch (e: any) { message.error(e.message) }
}

const _depthDebounce = ref<ReturnType<typeof setTimeout> | null>(null)
watch(graphDepth, (v) => {
  if (v === null || v === undefined) return
  if (_depthDebounce.value) clearTimeout(_depthDebounce.value)
  _depthDebounce.value = setTimeout(() => loadKnowledgeData(), 600)
})

async function loadKnowledgeData() {
  try {
    const [ents, rels] = await Promise.all([listKnowledgeEntities(), listKnowledgeRelations()])
    kgEntities.value = ents || []
    kgRelations.value = rels || []
    await resetAndLoadGraph(graphEntity.value.trim(), graphDepth.value)
  } catch (e: any) { message.error(e.message) }
}

async function removeKgEntity(name: string) {
  try {
    await deleteKnowledgeEntity(name)
    kgEntities.value = kgEntities.value.filter(e => e.name !== name)
    kgRelations.value = kgRelations.value.filter(r => r.from_entity !== name && r.to_entity !== name)
    message.success(t('insightView.entityDeleted'))
    await resetAndLoadGraph(graphEntity.value.trim(), graphDepth.value)
  } catch (e: any) { message.error(e.message) }
}

async function removeKgRelation(id: string) {
  try {
    await deleteKnowledgeRelation(id)
    kgRelations.value = kgRelations.value.filter(r => String(r.id) !== id)
    message.success(t('insightView.relationDeleted'))
    await resetAndLoadGraph(graphEntity.value.trim(), graphDepth.value)
  } catch (e: any) { message.error(e.message) }
}

// ── 共享 CRUD 模态框（提交后按实体类型刷新对应列表）──
const {
  showModal, modalType, formSeed, editing, modalTitle,
  openAdd, openEdit, handleModalOk,
} = useInsightCrud({
  memories: loadMemories,
  notes: loadNotes,
  learning: loadLearning,
  knowledge: loadKnowledgeData,
})

// WS 事件负载类型（JSON 直传，字段按后端 emit 约定收窄）
type PortraitConsolidatedEvent = WsEvent & { ok?: boolean; error?: string }
type XpLevelUpEvent = WsEvent & { level?: number; level_label?: string }

onMounted(async () => {
  loadEmotion()
  loadPortrait()
  loadToday()
  loadMemories()
  loadNotes()
  loadLearning()
  loadXpData()
  ws.on<PortraitConsolidatedEvent>('portrait_consolidated', onConsolidated)
  ws.on('knowledge_graph_changed', loadKnowledgeData)
  ws.on<XpLevelUpEvent>('xp_levelup', onXpLevelUp)
})

onBeforeUnmount(() => {
  ws.off<PortraitConsolidatedEvent>('portrait_consolidated', onConsolidated)
  ws.off('knowledge_graph_changed', loadKnowledgeData)
  ws.off<XpLevelUpEvent>('xp_levelup', onXpLevelUp)
  if (_depthDebounce.value) clearTimeout(_depthDebounce.value)
})
</script>

<template>
  <div class="insight-view">
    <h2 class="view-title view-title-icon"><ViewTitleIcon name="insight" /> {{ t('insightView.title') }}</h2>
    <n-tabs type="line" animated v-model:value="activeTab">
      <n-tab-pane name="emotion" :tab="t('insightView.emotion')">
        <EmotionPanel :current-emotion="currentEmotion" :history="emotionHistory" />
      </n-tab-pane>

      <n-tab-pane name="portrait" :tab="t('insightView.profile')">
        <PortraitPanel :portrait="portrait" :history="portraitHistory" :consolidating="consolidating"
                       @consolidate="consolidate" />
      </n-tab-pane>

      <n-tab-pane name="today" :tab="t('insightView.todayEvents')">
        <TodayPanel :items="todayData.items" :stats="todayData.stats" />
      </n-tab-pane>

      <n-tab-pane name="memory" :tab="t('insightView.memory')">
        <MemoryPanel v-model:query="memQuery" v-model:importance-min="importanceMin"
                     :memories="memories" @search="loadMemories" @add="openAdd('memory')"
                     @edit="(item) => openEdit('memory', item)" @remove="removeMemory" />
      </n-tab-pane>

      <n-tab-pane name="knowledge" :tab="t('insightView.knowledgeGraph')">
        <KnowledgeGraphPanel ref="knowledgePanel" :entities="kgEntities" :relations="kgRelations"
                             :nodes="kgNodes" :edges="kgEdges" :expanded-ids="expandedNodes"
                             :expanding-node="expandingNode" v-model:entity="graphEntity" v-model:depth="graphDepth"
                             @load="loadKnowledgeData" @expand="expandNode"
                             @add-entity="openAdd('entity')" @add-relation="openAdd('relation')"
                             @edit-entity="(item) => openEdit('entity', item)"
                             @edit-relation="(item) => openEdit('relation', item)"
                             @delete-entity="removeKgEntity" @delete-relation="removeKgRelation" />
      </n-tab-pane>

      <n-tab-pane name="notes" :tab="t('insightView.notes')">
        <NotesPanel :notes="notes" @add="openAdd('note')"
                    @edit="(item) => openEdit('note', item)" @remove="removeNote" />
      </n-tab-pane>

      <n-tab-pane name="learnings" :tab="t('insightView.learning')">
        <LearningsPanel :learnings="learnings" @add="openAdd('learning')"
                        @edit="(item) => openEdit('learning', item)" @remove="removeLearning" />
      </n-tab-pane>

      <n-tab-pane name="instincts" :tab="t('insightView.instinct')">
        <InstinctsPanel :instincts="instincts" @add="openAdd('instinct')"
                        @edit="(item) => openEdit('instinct', item)" @remove="removeInstinct" />
      </n-tab-pane>

      <n-tab-pane name="xp" :tab="'♥ ' + t('insightView.xp')">
        <XpPanel :xp-state="xpState" :xp-levels="xpLevels" />
      </n-tab-pane>

      <n-tab-pane name="jspace" :tab="'⬡ ' + t('jspace.title')">
        <JSpacePanel />
      </n-tab-pane>
    </n-tabs>

    <!-- XP 升级通知 -->
    <XpLevelUpToast :state="xpLevelUp" />

    <!-- 共享 CRUD 模态框 -->
    <CrudModal v-model:show="showModal" :type="modalType" :title="modalTitle"
               :seed="formSeed" :editing="editing" @ok="handleModalOk" />
  </div>
</template>

<style scoped>
.view-title { font-family: 'Noto Serif SC', serif; margin-bottom: 12px; }
</style>
