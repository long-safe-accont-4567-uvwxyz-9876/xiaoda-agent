<script setup lang="ts">
/**
 * 纳西妲宇宙 —— 3D 知识图谱全屏视图（编排层）
 *
 * 基于 3d-force-graph 渲染须弥配色的知识图谱。记忆生长在世界树上：
 * 程序化世界树居中，节点由锚点弹簧力安放在枝干上（高度数近主干、
 * 低度数在末梢），单击展开时新记忆沿父节点的枝头"向外生长"，
 * 叠加三层星空、Bloom 后处理、破土生长动画、节点高亮、闲置公转与 WS 实时同步。
 *
 * 逻辑分层拆分（./universe/，2026-08-23 拆解专项）：
 *   - types.ts       共享类型
 *   - theme.ts       须弥配色 / 类别映射
 *   - graphData.ts   数据→图结构纯函数（邻居索引 / 关系列表 / 转义）
 *   - starfield.ts   三层星空生成、自转与释放
 *   - ripples.ts     点击涟漪（geometry/material dispose 时序）
 *   - perfMonitor.ts 帧率采样与自动降档判定
 *   - worldTree.ts   世界树场景（递归分枝 / 锚点 / 光尘 / 冠层辉光）
 *   - engine.ts      three.js 场景引擎（实例生命周期 / Bloom / 锚点力 / 交互绑定 / 相机）
 * 本组件只保留：数据加载与按需展开编排、WS 同步、UI 状态与覆盖层。
 *
 * 适配说明（installed v1.80）：
 *  - 该版本无 graph.onEngineRender / graph.cameraAutoOrbit。
 *  - Bloom 通过官方 graph.postProcessingComposer()（自动创建 EffectComposer + RenderPass，
 *    引擎每帧自动调用 composer.render()），仅追加 UnrealBloomPass 即可。
 *  - 闲置公转通过 controlType:'orbit' 的 OrbitControls.autoRotate 实现（引擎每帧调用 controls.update）。
 */
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { NInput, NButton, NTag, useMessage, NInputNumber, NSelect, NPopconfirm } from 'naive-ui'
import {
  getKnowledgeGraph, getKnowledgeEntity, createKnowledgeEntity, updateKnowledgeEntity,
  deleteKnowledgeEntity, createKnowledgeRelation, deleteKnowledgeRelation,
} from '../../api'
import { getWsClient, type WsEvent } from '../../api/ws'
import { useKnowledgeGraphData } from '../../composables/useKnowledgeGraphData'
import { t, tf } from '../../i18n'
import { kindLabel } from './universe/theme'
import { findRelations } from './universe/graphData'
import { createUniverseEngine, detectWebGL, type UniverseEngine } from './universe/engine'
import type { GraphLink, GraphNode } from './universe/types'

const props = withDefaults(defineProps<{
  entity?: string
  depth?: number
  autoLoad?: boolean
  enableBloom?: boolean
}>(), {
  entity: '',
  depth: 1,
  autoLoad: true,
  enableBloom: true,
})

const emit = defineEmits<{ close: [] }>()

const message = useMessage()
const ws = getWsClient()

const containerEl = ref<HTMLDivElement | null>(null)
const nodes = ref<GraphNode[]>([])
const links = ref<GraphLink[]>([])
const loading = ref(false)
const selectedNode = ref<GraphNode | null>(null)
const hoveredNode = ref<GraphNode | null>(null)
const nodeCount = ref(0)
const degraded = ref(false)
const webglUnavailable = ref(false)

// 性能监测（Performance API）—— 帧率采集 + 自适应降级
const fps = ref(0)
const qualityTier = ref<'high' | 'medium' | 'low'>('high')
const toolbarCollapsed = ref(false)
// 边数过载标记：>900 边时初始档位直接降到 medium（聚焦式粒子已移除全量粒子开销，阈值放宽）
const heavyEdges = ref(false)

// 模态内部可控的检索 / 深度状态（与 prop 同步，但允许在浮层内独立切换）
const searchText = ref(props.entity || '')
const activeDepth = ref<number>(props.depth)

// 内部非响应式状态
let destroyed = false
let retryTimer: ReturnType<typeof setTimeout> | null = null
let debounceTimer: ReturnType<typeof setTimeout> | null = null
// 全量加载代际号：setActiveDepth 连击/快速连续操作时丢弃过期响应
// （此前 depth=1 与 depth=10 两个请求乱序返回，后到的小深度结果覆盖
//  已显示的大深度图——"深度调了没反应"的真凶，2026-08-27）
let loadSeq = 0
let depthDebounce: ReturnType<typeof setTimeout> | null = null
let resizeObserver: ResizeObserver | null = null
// 用户点击"仍要进入 3D"后置 true，loadGraph 跳过节点 >2000 降级判断
let bypassDegrade = false

// 场景引擎（three.js 实例唯一属主；降级重建期间同一引擎复用循环/定时器守卫）
let engine: UniverseEngine | null = null

function ensureEngine(): UniverseEngine {
  if (!engine) {
    engine = createUniverseEngine(containerEl, {
      enableBloom: props.enableBloom,
      expandedIds,
      hoveredNode,
      selectedNode,
      qualityTier,
      heavyEdges,
      fps,
      onExpandRequest: expandAround,
      // 空枝尖芽点点击 → "新记忆"面板（挂接父球留空 = 长成独立新枝）
      onBudClick: () => openCreate(),
    })
  }
  return engine
}

// ── 加载数据 ──
const RETRY_DELAYS = [1000, 2000, 4000]
// 按需展开状态：累积节点/边（去重合并走共享组合式函数），点击节点增量拉邻域
// 数据层已与 InsightView 2D 图合并为 useKnowledgeGraphData（2026-08-22 专项）
const acc = useKnowledgeGraphData<GraphNode, GraphLink>(
  (raw, degree) => ({
    id: String(raw.name),
    name: String(raw.name),
    kind: raw.kind,
    val: degree + 1,
  }),
  (raw) => ({ source: String(raw.from), target: String(raw.to), relation: raw.relation, id: raw.id }),
)
const accNodes = computed(() => acc.nodes.value)
const accLinks = computed(() => acc.edges.value)
const { expandedIds } = acc
const expandingName = ref('')

async function loadGraph(retries = 0) {
  if (destroyed || !containerEl.value) return
  // 容器尚未可见（模态隐藏）时跳过，等 ResizeObserver 唤醒
  if (containerEl.value.clientWidth === 0) return

  const seq = ++loadSeq
  loading.value = true
  try {
    // 按需展开模型：重置累积状态，拉起步实体在指定深度内的邻域；
    // 用户后续单击节点仍可增量展开，不再一次性拉全图
    acc.reset()
    // 根实体 = 搜索框当前关键词（2026-08-27 修"以我输入的关键词为核心"：
    // 此前恒用 props.entity（空）→ 永远全图模式以度数最高者为根，搜索
    // 只挪镜头不换树干，深度调整在 40 节点的小邻域里自然毫无变化）
    const startEntity = searchText.value.trim()
    engine?.resetWorld(startEntity) // 全量重载：清空落梢状态，检索实体优先作为根球
    const data = await getKnowledgeGraph(startEntity, activeDepth.value ?? 1)
    if (destroyed) return
    // 过期响应丢弃：期间用户又改了深度/刷新/WS 触发了新一次加载，
    // 本批数据作废（否则旧深度结果覆盖新深度图，且可能 merge 进
    // 新请求 reset 后的累积器造成节点半缺失）
    if (seq !== loadSeq) return

    acc.merge(data.nodes || [], data.edges || [])
    if (startEntity) acc.markExpanded(startEntity)

    applyAccumulatedToGraph()
  } catch (e: any) {
    if (seq !== loadSeq) return  // 已被新一次加载取代，静默退出
    if (retries < RETRY_DELAYS.length) {
      retryTimer = setTimeout(() => loadGraph(retries + 1), RETRY_DELAYS[retries])
    } else {
      message.error(e?.message || t('universeGraph.loadFailed'))
      loading.value = false
    }
  }
}

// 将累积数据应用到 3D 引擎（大图分阶段挂边，防初始帧卡死）
function applyAccumulatedToGraph() {
  if (destroyed || !containerEl.value) return

  nodes.value = [...accNodes.value]
  links.value = [...accLinks.value]
  nodeCount.value = accNodes.value.length

  // 渲染规模分级：>900 边强制 medium 起步（Bloom 关；粒子已改聚焦式，开销可控）
  heavyEdges.value = accLinks.value.length > 900

  // 性能保护：节点过多则降级（用户已点击"仍要进入 3D"则跳过）
  if (accNodes.value.length > 2000 && !bypassDegrade) {
    degraded.value = true
    loading.value = false
    // 释放已建实例（仅析构实例本身：星空/性能循环保持运行，重建后由守卫复用）
    engine?.destroyInstance()
    return
  }
  degraded.value = false

  const eng = ensureEngine()
  eng.updateNeighborIndex(accNodes.value, accLinks.value)
  if (!eng.hasInstance) eng.init()

  // setData 内部处理大图分阶段挂边；getLatest 保证延后触发时读到最新累积数据
  eng.setData(
    [...accNodes.value],
    [...accLinks.value],
    () => ({ nodes: [...accNodes.value], links: [...accLinks.value] }),
  )

  loading.value = false
  expandingName.value = ''

  eng.scheduleZoomToFit()
}

// ── 单击节点：增量拉取该节点 1 跳邻域合并进图 ──
async function expandAround(node: GraphNode) {
  const id = node.id as string
  if (expandingName.value || expandedIds.value.has(id)) return
  expandingName.value = node.name
  try {
    const data = await getKnowledgeGraph(node.name, 1)
    if (destroyed) return
    acc.merge(data.nodes || [], data.edges || [])
    expandedIds.value.add(id)
    applyAccumulatedToGraph()
    // 展开后聚焦到该节点
    setTimeout(() => { if (!destroyed) engine?.focusOnNode(node) }, 350)
  } catch (e: any) {
    console.debug('[universe] expand failed:', e?.message)
    expandingName.value = ''
  }
}

// ── 实体检索闪烁 ──
function focusOnEntity(name: string) {
  const target = nodes.value.find(n => n.name === name || (n.id as string) === name)
  if (!target) {
    message.info(tf('universeGraph.entityNotFound', name))
    return
  }
  if (!engine) return
  engine.focusOnNode(target)
  engine.flashNode(target.id as string)
}

// ── 深度切换 / 检索 ──
// 按需展开模型：深度只影响搜索起步范围（1-5），点击节点始终增量展开 1 跳
function setActiveDepth(d: number | null) {
  const v = Math.max(1, Math.min(12, Math.round(d ?? 1)))
  activeDepth.value = v
  // n-input-number 每击键都触发（输入"10"= 两次 loadGraph），300ms 防抖
  if (depthDebounce) clearTimeout(depthDebounce)
  depthDebounce = setTimeout(() => { depthDebounce = null; if (!destroyed) loadGraph() }, 300)
}

function onSearchEnter() {
  const q = searchText.value.trim()
  if (!q) return
  // 已在当前树根上的关键词不重复加载
  if (q === currentRoot.value) {
    focusOnEntity(q)
    return
  }
  // 换根重建：以关键词为树干、按当前深度重新外扩（后端 graph 接口
  // 实体不存在时返回空图，由 loadGraph 的空态自然呈现，不打断操作流）
  loadGraph()
}

/** 当前树的根实体（根球名称；空 = 全图模式） */
const currentRoot = computed(() => searchText.value.trim())

// 强制进入 3D（绕过降级）
function forceEnter3D() {
  bypassDegrade = true
  degraded.value = false
  // 下一帧重建
  requestAnimationFrame(() => loadGraph())
}

// 工具栏入口（视觉行为由引擎实现，与拆分前一致）
function resetView() {
  engine?.resetView()
}

function toggleLight() {
  engine?.toggleLight()
}

// ── WS 实时同步 ──
function onGraphChanged(_e: WsEvent) {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    if (!destroyed) loadGraph()
  }, 500)
}

// ── FPS 指示器配色 ──
const fpsClass = computed(() => {
  if (fps.value >= 50) return 'fps-good'
  if (fps.value >= 30) return 'fps-mid'
  return 'fps-low'
})

// ── 开灯/关灯标签（high 档=灯开，其余=灯关）──
const lightLabel = computed(() => qualityTier.value === 'high'
  ? t('universeGraph.lightOff') : t('universeGraph.lightOn'))

// ── 详情面板：选中节点的关系 ──
const selectedRelations = computed(() => {
  const node = selectedNode.value
  if (!node) return []
  return findRelations(links.value, node.id as string, 30, t('universeGraph.defaultRelation'))
})

// ── 记忆球编辑（2026-08-27 记忆树编辑专项）──
// 模式：view 只读详情 / edit 编辑实体 / create 新建实体（空枝尖或工具栏入口）
const detailMode = ref<'view' | 'edit' | 'create'>('view')
const editKind = ref('')
const editObs = ref('')
const editSaving = ref(false)
const createName = ref('')
// 新建：挂接父球（可选，建一条 parent → new 的关系让新球长在父球枝头）
const createParentId = ref<string | null>(null)
const createRelation = ref('')
const createLinkOptions = computed(() =>
  nodes.value.map(n => ({ label: n.name, value: n.id as string })))
// 删除实体：输入实体名确认（破坏性操作，防误删）
const deleteConfirmName = ref('')
const deleting = ref(false)
// 添加连接
const addLinkOpen = ref(false)
const addLinkTarget = ref<string | null>(null)
const addLinkRelation = ref('')
const addLinkSaving = ref(false)

const KIND_OPTIONS = ['entity', 'person', 'place', 'concept', 'event']

// 3D 场景点击其他节点时：气泡回到只读态（编辑/新建表单一律不跨节点保留）
watch(selectedNode, () => {
  if (detailMode.value !== 'create') resetDetailMode()
})

function openEdit() {
  const node = selectedNode.value
  if (!node) return
  detailMode.value = 'edit'
  editKind.value = node.kind || 'entity'
  editObs.value = ''
  // 拉观察记录（graph 接口只带 name/kind）
  getKnowledgeEntity(node.name)
    .then(row => { if (detailMode.value === 'edit') editObs.value = row.observations || '' })
    .catch(() => { /* 拉不到就留空，保存时以空串不覆盖语义仍成立 */ })
}

function resetDetailMode() {
  detailMode.value = 'view'
  deleteConfirmName.value = ''
  addLinkOpen.value = false
  createParentId.value = null
  createRelation.value = ''
  createName.value = ''
}

async function saveEntityEdit() {
  const node = selectedNode.value
  if (!node) return
  editSaving.value = true
  try {
    await updateKnowledgeEntity(node.name, { kind: editKind.value, observations: editObs.value })
    node.kind = editKind.value
    message.success(t('universeGraph.saved'))
    detailMode.value = 'view'
  } catch (e: any) {
    message.error(e.message)
  } finally {
    editSaving.value = false
  }
}

async function removeEntity() {
  const node = selectedNode.value
  if (!node || deleteConfirmName.value !== node.name) return
  deleting.value = true
  try {
    await deleteKnowledgeEntity(node.name)
    message.success(tf('universeGraph.deleted', node.name))
    selectedNode.value = null
    resetDetailMode()
    await refreshAfterMutation()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    deleting.value = false
  }
}

async function removeRelation(relId?: string) {
  if (!relId) return
  try {
    await deleteKnowledgeRelation(relId)
    message.success(t('universeGraph.relationDeleted'))
    await refreshAfterMutation()
  } catch (e: any) {
    message.error(e.message)
  }
}

async function submitAddLink() {
  const node = selectedNode.value
  if (!node || !addLinkTarget.value) return
  addLinkSaving.value = true
  try {
    await createKnowledgeRelation({
      from: node.name, to: addLinkTarget.value,
      relation: addLinkRelation.value.trim() || t('universeGraph.defaultRelation'),
    })
    message.success(t('universeGraph.relationAdded'))
    addLinkOpen.value = false
    addLinkTarget.value = null
    addLinkRelation.value = ''
    await refreshAfterMutation()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    addLinkSaving.value = false
  }
}

/** 新建实体：空枝尖入口不挂父球；记忆球入口必须选父球（关系可空用默认） */
function openCreate(parentId?: string) {
  resetDetailMode()
  detailMode.value = 'create'
  selectedNode.value = null
  createParentId.value = parentId ?? null
  editKind.value = 'entity'
  editObs.value = ''
  createRelation.value = ''
}

async function submitCreate(name: string) {
  const trimmed = name.trim()
  if (!trimmed) return
  editSaving.value = true
  try {
    await createKnowledgeEntity({ name: trimmed, kind: editKind.value, observations: editObs.value })
    if (createParentId.value) {
      const parent = nodes.value.find(n => n.id === createParentId.value)
      if (parent) {
        await createKnowledgeRelation({
          from: parent.name, to: trimmed,
          relation: createRelation.value.trim() || t('universeGraph.defaultRelation'),
        })
      }
    }
    message.success(tf('universeGraph.created', trimmed))
    resetDetailMode()
    await refreshAfterMutation()
    // 聚焦新球
    const fresh = nodes.value.find(n => n.name === trimmed)
    if (fresh) setTimeout(() => { if (!destroyed) engine?.focusOnNode(fresh) }, 600)
  } catch (e: any) {
    message.error(e.message)
  } finally {
    editSaving.value = false
  }
}

/** 增删改后局部刷新：不重置视角/树，仅合并新数据（保持场景连续性） */
async function refreshAfterMutation() {
  try {
    const focus = selectedNode.value?.name || ''
    const data = await getKnowledgeGraph(focus || searchText.value, activeDepth.value ?? 1)
    if (destroyed) return
    acc.merge(data.nodes || [], data.edges || [])
    applyAccumulatedToGraph()
  } catch { /* 刷新失败不打断操作流，WS 同步会再补 */ }
}

// ── 生命周期 ──
onMounted(() => {
  webglUnavailable.value = !detectWebGL()
  ws.on('knowledge_graph_changed', onGraphChanged)
  // 窗口隐藏/最小化：显式暂停 3d-force-graph 引擎（防 WebView2 rAF 节流边缘情况）
  document.addEventListener('visibilitychange', onVisibility)
  // 全屏体验：ESC 直接关闭
  window.addEventListener('keydown', onKeydown)

  // ResizeObserver：模态由 display 切换，容器尺寸从 0 变非 0 时再初始化
  if (containerEl.value) {
    resizeObserver = new ResizeObserver(() => {
      engine?.applyResize()
      const el = containerEl.value
      if (el && el.clientWidth > 0 && !(engine && engine.hasInstance) && props.autoLoad && !webglUnavailable.value && !degraded.value) {
        loadGraph()
      }
    })
    resizeObserver.observe(containerEl.value)
  }
})

function onVisibility() {
  if (!engine) return
  if (document.hidden) engine.pauseAnimation()
  else engine.resumeAnimation()
}

// 全屏体验：ESC 关闭
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') emit('close')
}

onBeforeUnmount(() => {
  destroyed = true
  ws.off('knowledge_graph_changed', onGraphChanged)
  document.removeEventListener('visibilitychange', onVisibility)
  window.removeEventListener('keydown', onKeydown)
  if (retryTimer) clearTimeout(retryTimer)
  if (debounceTimer) clearTimeout(debounceTimer)
  if (depthDebounce) clearTimeout(depthDebounce)
  loadSeq++  // 在途响应全部作废
  resizeObserver?.disconnect()
  resizeObserver = null
  // 引擎完整销毁链见 universe/engine.ts::shutdown（定时器→RAF→涟漪 dispose→渲染器析构）
  engine?.shutdown()
  engine = null
})

// 外部 entity / depth 变化
watch(() => props.entity, (v) => {
  searchText.value = v || ''
  if (v) focusOnEntity(v)
})
watch(() => props.depth, (d) => {
  activeDepth.value = d
  loadGraph()
})
</script>

<template>
  <div class="universe-root">
    <!-- 顶部工具栏（可收起） -->
    <div class="universe-toolbar glass-panel" :class="{ collapsed: toolbarCollapsed }">
      <template v-if="!toolbarCollapsed">
        <n-input
          v-model:value="searchText"
          size="small"
          :placeholder="t('universeGraph.searchPh')"
          style="max-width: 200px"
          @keydown.enter="onSearchEnter"
        />
        <div class="universe-depth-input" :title="t('universeGraph.depthHint')">
          <span class="universe-depth-label">{{ t('universeGraph.depthLabel') }}</span>
          <n-input-number
            v-model:value="activeDepth"
            size="small"
            :min="1"
            :max="12"
            :show-button="false"
            style="width: 64px"
            @update:value="setActiveDepth"
          />
        </div>
        <span v-if="expandingName" class="universe-expanding">{{ t('insightView.expanding') }}「{{ expandingName }}」…</span>
        <span class="universe-count">{{ nodeCount }} {{ t('universeGraph.nodeCount') }}</span>
        <span class="universe-fps" :class="fpsClass">{{ fps }} fps · {{ qualityTier }}</span>
        <n-button size="tiny" quaternary @click="resetView" :title="t('universeGraph.resetView')">{{ t('universeGraph.resetView') }}</n-button>
        <n-button size="tiny" quaternary @click="toggleLight">{{ lightLabel }}</n-button>
        <n-button size="tiny" quaternary @click="loadGraph()">{{ t('universeGraph.refresh') }}</n-button>
        <n-button size="tiny" type="primary" @click="openCreate()">{{ t('universeGraph.createTitle') }}</n-button>
        <n-button class="universe-close" size="tiny" type="primary" @click="emit('close')">{{ t('universeGraph.close') }}</n-button>
        <n-button size="tiny" quaternary @click="toolbarCollapsed = true">▴</n-button>
      </template>
      <n-button v-else size="tiny" quaternary @click="toolbarCollapsed = false">{{ t('universeGraph.collapseToolbar') }}</n-button>
    </div>

    <!-- 3D 容器 -->
    <div ref="containerEl" class="universe-canvas" />

    <!-- 世界树提示（左下角） -->
    <div class="universe-treehint">{{ t('universeGraph.treeHint') }}</div>

    <!-- 加载中 -->
    <div v-if="loading" class="universe-loading">
      <div class="sumeru-spinner" />
      <span>{{ t('universeGraph.loading') }}</span>
    </div>

    <!-- 降级提示 -->
    <div v-if="degraded" class="universe-degraded glass-panel">
      <p>{{ tf('universeGraph.degradedHint', nodeCount) }}</p>
      <n-button size="small" type="primary" @click="forceEnter3D">{{ t('universeGraph.forceEnter3d') }}</n-button>
    </div>

    <!-- WebGL 不可用 -->
    <div v-if="webglUnavailable" class="universe-degraded glass-panel">
      <p>{{ t('universeGraph.webglUnsupported') }}</p>
      <n-button size="small" type="primary" @click="emit('close')">{{ t('universeGraph.close') }}</n-button>
    </div>

    <!-- 节点详情/编辑气泡（view 只读 / edit 编辑实体 / create 新建实体） -->
    <div v-if="selectedNode || detailMode === 'create'" class="universe-detail glass-panel">
      <!-- 只读视图 -->
      <template v-if="detailMode === 'view' && selectedNode">
        <div class="detail-head">
          <span class="detail-name">{{ selectedNode.name }}</span>
          <n-tag size="tiny" :bordered="false">{{ kindLabel(selectedNode.kind) }}</n-tag>
          <span class="detail-degree">{{ t('universeGraph.degree') }} {{ (selectedNode.val ?? 1) - 1 }}</span>
          <n-button size="tiny" quaternary @click="selectedNode = null; resetDetailMode()">✕</n-button>
        </div>
        <div class="detail-actions">
          <n-button size="tiny" @click="openEdit">{{ t('universeGraph.edit') }}</n-button>
          <n-button size="tiny" @click="openCreate(selectedNode.id as string)">{{ t('universeGraph.addChild') }}</n-button>
        </div>
        <div class="detail-relations">
          <div v-for="r in selectedRelations" :key="(r.id ?? '') + r.other" class="rel-row">
            <span class="rel-arrow">{{ r.other }}</span>
            <n-tag size="tiny" type="info" :bordered="false">{{ r.relation }}</n-tag>
            <n-popconfirm v-if="r.id" @positive-click="removeRelation(r.id)">
              <template #trigger>
                <n-button size="tiny" quaternary type="error" class="rel-del">✕</n-button>
              </template>
              {{ t('universeGraph.delRelationConfirm') }} {{ r.other }}？
            </n-popconfirm>
          </div>
          <div v-if="!selectedRelations.length" class="rel-empty">{{ t('universeGraph.noRelations') }}</div>
        </div>
        <div v-if="!addLinkOpen" class="detail-actions">
          <n-button size="tiny" quaternary @click="addLinkOpen = true">+ {{ t('universeGraph.addRelation') }}</n-button>
        </div>
        <div v-else class="detail-form">
          <n-select v-model:value="addLinkTarget" size="tiny" filterable
                    :options="createLinkOptions" :placeholder="t('universeGraph.targetPh')" />
          <n-input v-model:value="addLinkRelation" size="tiny"
                   :placeholder="t('universeGraph.relationPh')" />
          <div class="form-row">
            <n-button size="tiny" type="primary" :loading="addLinkSaving" :disabled="!addLinkTarget"
                      @click="submitAddLink">{{ t('universeGraph.addRelation') }}</n-button>
            <n-button size="tiny" quaternary @click="addLinkOpen = false">{{ t('universeGraph.cancel') }}</n-button>
          </div>
        </div>
      </template>

      <!-- 编辑实体 -->
      <template v-else-if="detailMode === 'edit' && selectedNode">
        <div class="detail-head">
          <span class="detail-name">{{ selectedNode.name }}</span>
          <n-button size="tiny" quaternary @click="resetDetailMode">✕</n-button>
        </div>
        <div class="detail-form">
          <span class="form-label">{{ t('universeGraph.kindLabel') }}</span>
          <n-select v-model:value="editKind" size="tiny" :options="KIND_OPTIONS.map(k => ({ label: t('universeGraph.kind' + k), value: k }))" />
          <span class="form-label">{{ t('universeGraph.observations') }}</span>
          <n-input v-model:value="editObs" type="textarea" :rows="6"
                   :placeholder="t('universeGraph.obsPh')" />
          <div class="form-row">
            <n-button size="tiny" type="primary" :loading="editSaving" @click="saveEntityEdit">{{ t('universeGraph.save') }}</n-button>
            <n-button size="tiny" quaternary @click="resetDetailMode">{{ t('universeGraph.cancel') }}</n-button>
          </div>
        </div>
        <div class="detail-danger">
          <span class="form-label">{{ t('universeGraph.dangerZone') }}</span>
          <n-input v-model:value="deleteConfirmName" size="tiny"
                   :placeholder="t('universeGraph.deleteConfirmPh')" />
          <n-popconfirm :disabled="deleteConfirmName !== selectedNode.name" @positive-click="removeEntity">
            <template #trigger>
              <n-button size="tiny" type="error" :disabled="deleteConfirmName !== selectedNode.name" :loading="deleting">
                {{ t('universeGraph.deleteEntity') }}
              </n-button>
            </template>
            {{ t('universeGraph.deleteEntityConfirm') }}
          </n-popconfirm>
        </div>
      </template>

      <!-- 新建实体 -->
      <template v-else-if="detailMode === 'create'">
        <div class="detail-head">
          <span class="detail-name">{{ t('universeGraph.createTitle') }}</span>
          <n-button size="tiny" quaternary @click="resetDetailMode">✕</n-button>
        </div>
        <div class="detail-form">
          <template v-if="createParentId">
            <span class="form-label">{{ t('universeGraph.parentLabel') }}</span>
            <n-select v-model:value="createParentId" size="tiny" filterable :options="createLinkOptions" />
          </template>
          <span class="form-label">{{ t('universeGraph.nameLabel') }}</span>
          <n-input v-model:value="createName" size="tiny" :placeholder="t('universeGraph.namePh')"
                   @keydown.enter="submitCreate(createName)" />
          <span class="form-label">{{ t('universeGraph.kindLabel') }}</span>
          <n-select v-model:value="editKind" size="tiny" :options="KIND_OPTIONS.map(k => ({ label: t('universeGraph.kind' + k), value: k }))" />
          <template v-if="createParentId">
            <span class="form-label">{{ t('universeGraph.relationLabel') }}</span>
            <n-input v-model:value="createRelation" size="tiny" :placeholder="t('universeGraph.relationPh')" />
          </template>
          <span class="form-label">{{ t('universeGraph.observations') }}</span>
          <n-input v-model:value="editObs" type="textarea" :rows="4" :placeholder="t('universeGraph.obsPh')" />
          <div class="form-row">
            <n-button size="tiny" type="primary" :loading="editSaving" :disabled="!createName.trim()"
                      @click="submitCreate(createName)">{{ t('universeGraph.create') }}</n-button>
            <n-button size="tiny" quaternary @click="resetDetailMode">{{ t('universeGraph.cancel') }}</n-button>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.universe-root {
  position: fixed;
  inset: 0;
  width: 100vw;
  height: 100vh;
  background: var(--forest-deep);
  overflow: hidden;
  z-index: 1000;
}

.universe-canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
}

.universe-treehint {
  position: absolute;
  bottom: 14px;
  left: 16px;
  padding: 5px 12px;
  border-radius: 999px;
  font-size: 11.5px;
  color: var(--moon-dim);
  background: rgba(10, 20, 14, 0.45);
  border: 1px solid var(--glass-border);
  z-index: 10;
  pointer-events: none;
}

.universe-toolbar {
  position: absolute;
  top: 16px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  z-index: 10;
  transition: padding 0.2s ease;
}

.universe-toolbar.collapsed {
  padding: 4px 12px;
}

.universe-count {
  font-size: 12px;
  color: var(--moon-dim);
  margin-left: 4px;
}

.universe-depth-input {
  display: flex;
  align-items: center;
  gap: 6px;
}

.universe-depth-label {
  font-size: 12px;
  color: var(--moon-dim);
  white-space: nowrap;
}

.universe-expanding {
  font-size: 11px;
  color: var(--wisdom);
  animation: universe-pulse 1.2s ease-in-out infinite;
}

@keyframes universe-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}

.universe-fps {
  font-size: 11px;
  font-family: monospace;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(10, 20, 14, 0.45);
}

.universe-fps.fps-good {
  color: var(--dendro);
}

.universe-fps.fps-mid {
  color: var(--wisdom);
}

.universe-fps.fps-low {
  color: var(--alert);
}

.universe-close {
  margin-left: 8px;
}

.universe-loading {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  color: var(--moon);
  font-size: 14px;
  z-index: 10;
}

.sumeru-spinner {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  border: 3px solid var(--glass-border);
  border-top-color: var(--dendro);
  animation: sumeru-spin 0.9s linear infinite;
}

@keyframes sumeru-spin {
  to { transform: rotate(360deg); }
}

.universe-degraded {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  padding: 20px 28px;
  text-align: center;
  color: var(--moon);
  display: flex;
  flex-direction: column;
  gap: 14px;
  align-items: center;
  z-index: 10;
  max-width: 360px;
}

.universe-detail {
  position: absolute;
  top: 70px;
  right: 16px;
  width: 280px;
  padding: 14px 16px;
  z-index: 10;
}

.detail-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.detail-name {
  font-size: 15px;
  color: var(--dendro);
  font-weight: 600;
}

.detail-degree {
  font-size: 11px;
  color: var(--moon-dim);
  margin-left: auto;
}

.detail-relations {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 40vh;
  overflow-y: auto;
}

.rel-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.rel-arrow {
  color: var(--moon);
}

.rel-arrow::before {
  content: '→ ';
  color: var(--wisdom);
}

.rel-empty {
  font-size: 12px;
  color: var(--moon-dim);
  padding: 4px 0;
}

.detail-actions {
  display: flex;
  gap: 8px;
  margin: 8px 0;
}

.detail-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 8px 0;
}

.form-label {
  font-size: 11px;
  color: var(--moon-dim);
  margin-top: 2px;
}

.form-row {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}

.rel-del {
  margin-left: auto;
  opacity: 0.55;
}

.rel-row:hover .rel-del {
  opacity: 1;
}

.detail-danger {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--line-soft);
}
</style>
