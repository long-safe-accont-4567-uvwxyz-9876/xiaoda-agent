<script setup lang="ts">
/**
 * 纳西妲宇宙 —— 3D 知识图谱全屏视图（编排层）
 *
 * 基于 3d-force-graph 渲染须弥配色的知识图谱，叠加三层星空、Bloom 后处理、
 * 节点高亮、闲置公转与 WS 实时同步。
 *
 * 逻辑分层拆分（./universe/，2026-08-23 拆解专项）：
 *   - types.ts       共享类型
 *   - theme.ts       须弥配色 / 类别映射
 *   - graphData.ts   数据→图结构纯函数（邻居索引 / 关系列表 / 转义）
 *   - starfield.ts   三层星空生成、自转与释放
 *   - ripples.ts     点击涟漪（geometry/material dispose 时序）
 *   - perfMonitor.ts 帧率采样与自动降档判定
 *   - engine.ts      three.js 场景引擎（实例生命周期 / Bloom / 交互绑定 / 相机）
 * 本组件只保留：数据加载与按需展开编排、WS 同步、UI 状态与覆盖层。
 *
 * 适配说明（installed v1.80）：
 *  - 该版本无 graph.onEngineRender / graph.cameraAutoOrbit。
 *  - Bloom 通过官方 graph.postProcessingComposer()（自动创建 EffectComposer + RenderPass，
 *    引擎每帧自动调用 composer.render()），仅追加 UnrealBloomPass 即可。
 *  - 闲置公转通过 controlType:'orbit' 的 OrbitControls.autoRotate 实现（引擎每帧调用 controls.update）。
 */
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { NInput, NButton, NTag, useMessage, NInputNumber } from 'naive-ui'
import { getKnowledgeGraph } from '../../api'
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
// 边数过载标记：>600 边时初始档位直接降到 medium 并关粒子
const heavyEdges = ref(false)

// 模态内部可控的检索 / 深度状态（与 prop 同步，但允许在浮层内独立切换）
const searchText = ref(props.entity || '')
const activeDepth = ref<number>(props.depth)

// 内部非响应式状态
let destroyed = false
let retryTimer: ReturnType<typeof setTimeout> | null = null
let debounceTimer: ReturnType<typeof setTimeout> | null = null
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
    fx: 0, fy: 0, fz: 0,
  }),
  (raw) => ({ source: String(raw.from), target: String(raw.to), relation: raw.relation }),
)
const accNodes = computed(() => acc.nodes.value)
const accLinks = computed(() => acc.edges.value)
const { expandedIds } = acc
const expandingName = ref('')

async function loadGraph(retries = 0) {
  if (destroyed || !containerEl.value) return
  // 容器尚未可见（模态隐藏）时跳过，等 ResizeObserver 唤醒
  if (containerEl.value.clientWidth === 0) return

  loading.value = true
  try {
    // 按需展开模型：重置累积状态，只拉起步实体 depth=1 邻域（<400 边）
    // 用户后续单击节点增量展开，不再一次性拉全图
    acc.reset()
    const startEntity = props.entity.trim()
    const data = await getKnowledgeGraph(startEntity, Math.min(activeDepth.value ?? 6, 1))
    if (destroyed) return

    acc.merge(data.nodes || [], data.edges || [])
    if (startEntity) acc.markExpanded(startEntity)

    applyAccumulatedToGraph()
  } catch (e: any) {
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

  // 渲染规模分级：>600 边关粒子流（每条边一个动画粒子是最大 GPU 开销）
  heavyEdges.value = accLinks.value.length > 600

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

  // 大图（>400 节点）先只挂节点、延后 300ms 再挂边：力导向先散开节点，
  // 避免边+节点同时求解导致的初始帧卡死
  const bigGraph = accNodes.value.length > 400
  eng.setData(accNodes.value, bigGraph ? [] : accLinks.value)
  eng.configureForce(bigGraph)

  loading.value = false
  expandingName.value = ''

  eng.scheduleRelease(() => accNodes.value, () => accLinks.value)
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
  loadGraph()
}

function onSearchEnter() {
  const q = searchText.value.trim()
  if (!q) return
  focusOnEntity(q)
}

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
  return findRelations(links.value, node.id as string, 10, t('universeGraph.defaultRelation'))
})

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
        <n-button class="universe-close" size="tiny" type="primary" @click="emit('close')">{{ t('universeGraph.close') }}</n-button>
        <n-button size="tiny" quaternary @click="toolbarCollapsed = true">▴</n-button>
      </template>
      <n-button v-else size="tiny" quaternary @click="toolbarCollapsed = false">{{ t('universeGraph.collapseToolbar') }}</n-button>
    </div>

    <!-- 3D 容器 -->
    <div ref="containerEl" class="universe-canvas" />

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

    <!-- 节点详情浮层 -->
    <div v-if="selectedNode" class="universe-detail glass-panel">
      <div class="detail-head">
        <span class="detail-name">{{ selectedNode.name }}</span>
        <n-tag size="tiny" :bordered="false">{{ kindLabel(selectedNode.kind) }}</n-tag>
        <span class="detail-degree">{{ t('universeGraph.degree') }} {{ (selectedNode.val ?? 1) - 1 }}</span>
        <n-button size="tiny" quaternary @click="selectedNode = null">✕</n-button>
      </div>
      <div class="detail-relations">
        <div v-for="(r, i) in selectedRelations" :key="i" class="rel-row">
          <span class="rel-arrow">{{ r.other }}</span>
          <n-tag size="tiny" type="info" :bordered="false">{{ r.relation }}</n-tag>
        </div>
        <div v-if="!selectedRelations.length" class="rel-empty">{{ t('universeGraph.noRelations') }}</div>
      </div>
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
  background: rgba(0, 0, 0, 0.3);
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
</style>
