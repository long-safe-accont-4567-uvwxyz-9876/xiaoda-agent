<script setup lang="ts">
/**
 * 纳西妲宇宙 —— 3D 知识图谱全屏视图
 *
 * 基于 3d-force-graph 渲染须弥配色的知识图谱，叠加三层星空、Bloom 后处理、
 * 节点高亮、闲置公转与 WS 实时同步。
 *
 * 适配说明（installed v1.80）：
 *  - 该版本无 graph.onEngineRender / graph.cameraAutoOrbit。
 *  - Bloom 通过官方 graph.postProcessingComposer()（自动创建 EffectComposer + RenderPass，
 *    引擎每帧自动调用 composer.render()），仅追加 UnrealBloomPass 即可。
 *  - 闲置公转通过 controlType:'orbit' 的 OrbitControls.autoRotate 实现（引擎每帧调用 controls.update）。
 */
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { NInput, NButton, NTag, useMessage } from 'naive-ui'
import ForceGraph3D, { type NodeObject, type ForceGraph3DInstance } from '3d-force-graph'
import * as THREE from 'three'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'
import { getKnowledgeGraph } from '../../api'
import { getWsClient, type WsEvent } from '../../api/ws'

interface GraphNode extends NodeObject {
  name: string
  kind?: string
  val?: number
}

interface GraphLink {
  source: string | GraphNode
  target: string | GraphNode
  relation?: string
}

interface StarLayer {
  points: THREE.Points
  speed: number
}

interface Ripple {
  mesh: THREE.Mesh
  startTime: number
  duration: number
}

interface OrbitLikeControls {
  autoRotate: boolean
  autoRotateSpeed: number
  update?: (delta?: number) => void
  target?: THREE.Vector3
  mouseButtons?: { LEFT: number; MIDDLE: number; RIGHT: number }
}

const props = withDefaults(defineProps<{
  entity?: string
  depth?: 1 | 2
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
const graph = ref<ForceGraph3DInstance | null>(null)
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

// 模态内部可控的检索 / 深度状态（与 prop 同步，但允许在浮层内独立切换）
const searchText = ref(props.entity || '')
const activeDepth = ref<1 | 2>(props.depth)

// 内部非响应式状态
let composer: ReturnType<ForceGraph3DInstance['postProcessingComposer']> | null = null
let bloomPass: UnrealBloomPass | null = null
let starLayers: StarLayer[] = []
let starRAF = 0
let ripples: Ripple[] = []
let idleTimer: ReturnType<typeof setTimeout> | null = null
let retryTimer: ReturnType<typeof setTimeout> | null = null
let debounceTimer: ReturnType<typeof setTimeout> | null = null
let flashTimer: ReturnType<typeof setTimeout> | null = null
let zoomTimer: ReturnType<typeof setTimeout> | null = null
let releaseTimer: ReturnType<typeof setTimeout> | null = null
let resizeObserver: ResizeObserver | null = null
let destroyed = false
let initialized = false
// 用户点击"仍要进入 3D"后置 true，loadGraph 跳过节点 >2000 降级判断
let bypassDegrade = false
// 节点 id -> 邻居 id 集合（用于 hover 高亮）
let neighborsCache = new Map<string, Set<string>>()

// 性能监测内部变量
let perfRAF = 0
let frameCount = 0
let lastPerfTime = 0
let lowFpsStreak = 0

// ── 配色 ──
const COLOR_DENDRO = '#8fe560'
const COLOR_WISDOM = '#e8d5a3'
const COLOR_MOON = '#f2f7ee'
const COLOR_ALERT = '#d96a5f'
const COLOR_DIM = 'rgba(143,229,96,0.6)'
const COLOR_LINK = 'rgba(143,229,96,0.3)'
const COLOR_LINK_DIM = 'rgba(143,229,96,0.1)'
const COLOR_NODE_DIM = 'rgba(143,229,96,0.15)'
const BG_DEEP = '#0f1f17'

function colorForKind(kind?: string): string {
  if (!kind) return COLOR_DIM
  const k = kind.toLowerCase()
  if (k === 'person' || kind === '人物') return COLOR_DENDRO
  if (k === 'place' || k === 'location' || kind === '地点') return COLOR_WISDOM
  if (k === 'concept' || kind === '概念') return COLOR_MOON
  if (k === 'event' || kind === '事件') return COLOR_ALERT
  return COLOR_DIM
}

// ── WebGL 可用性检测 ──
function detectWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas')
    const ctx = canvas.getContext('webgl') || canvas.getContext('experimental-webgl')
    return !!ctx
  } catch {
    return false
  }
}

// ── 邻居索引 ──
function buildNeighbors(ns: GraphNode[], ls: GraphLink[]): Map<string, Set<string>> {
  const map = new Map<string, Set<string>>()
  for (const n of ns) map.set(n.id as string, new Set())
  for (const l of ls) {
    const s = linkId(l.source)
    const t = linkId(l.target)
    if (!map.has(s)) map.set(s, new Set())
    if (!map.has(t)) map.set(t, new Set())
    map.get(s)!.add(t)
    map.get(t)!.add(s)
  }
  return map
}

function linkId(end: string | GraphNode): string {
  return typeof end === 'string' ? end : (end.id as string)
}

// ── 加载数据 ──
const RETRY_DELAYS = [1000, 2000, 4000]

async function loadGraph(retries = 0) {
  if (destroyed || !containerEl.value) return
  // 容器尚未可见（模态隐藏）时跳过，等 ResizeObserver 唤醒
  if (containerEl.value.clientWidth === 0) return

  loading.value = true
  try {
    const data = await getKnowledgeGraph(props.entity, activeDepth.value)
    if (destroyed) return

    const rawNodes: any[] = data.nodes || []
    const rawEdges: any[] = data.edges || []

    // 计算度数
    const degree = new Map<string, number>()
    for (const e of rawEdges) {
      const f = String(e.from), t = String(e.to)
      degree.set(f, (degree.get(f) || 0) + 1)
      degree.set(t, (degree.get(t) || 0) + 1)
    }

    const graphNodes: GraphNode[] = rawNodes.map(n => {
      const id = String(n.name)
      return {
        id,
        name: String(n.name),
        kind: n.kind,
        val: (degree.get(id) || 0) + 1,
        // 入场动画：所有节点从中心起步
        fx: 0, fy: 0, fz: 0,
      }
    })
    const graphLinks: GraphLink[] = rawEdges.map(e => ({
      source: String(e.from),
      target: String(e.to),
      relation: e.relation,
    }))

    nodes.value = graphNodes
    links.value = graphLinks
    nodeCount.value = graphNodes.length
    neighborsCache = buildNeighbors(graphNodes, graphLinks)

    // 性能保护：节点过多则降级（用户已点击"仍要进入 3D"则跳过）
    if (graphNodes.length > 2000 && !bypassDegrade) {
      degraded.value = true
      loading.value = false
      // 释放已建实例
      if (graph.value) {
        graph.value._destructor()
        graph.value = null
        initialized = false
      }
      return
    }
    degraded.value = false

    if (!graph.value) initGraph()

    // graphData 接收内部会原地解析 source/target 为节点对象
    graph.value!.graphData({ nodes: graphNodes as unknown as NodeObject[], links: graphLinks as unknown as GraphLink[] } as any)

    // 力导向参数：增大斥力和连接长度，避免节点挤成一坨
    // 默认 charge.strength=-30, link.distance=30；调大后节点会分散开
    const charge = graph.value!.d3Force('charge')
    if (charge) charge.strength(-120)
    const link = graph.value!.d3Force('link')
    if (link) link.distance(60)

    loading.value = false

    // 释放初始锚定，让力导向把节点从中心炸开
    releaseTimer = setTimeout(() => {
      if (destroyed || !graph.value) return
      graphNodes.forEach(n => { n.fx = undefined; n.fy = undefined; n.fz = undefined })
      graph.value!.d3AlphaDecay(0.05)
      graph.value!.graphData({ nodes: graphNodes as unknown as NodeObject[], links: graphLinks as unknown as GraphLink[] } as any)
    }, 300)

    // 收敛后框选居中
    zoomTimer = setTimeout(() => {
      if (destroyed || !graph.value) return
      graph.value!.zoomToFit(500, 100)
    }, 1500)
  } catch (e: any) {
    if (retries < RETRY_DELAYS.length) {
      retryTimer = setTimeout(() => loadGraph(retries + 1), RETRY_DELAYS[retries])
    } else {
      message.error(e?.message || '加载知识图谱失败')
      loading.value = false
    }
  }
}

// ── 性能监测 + 自适应降级 ──
// 每 500ms 采样帧率；连续低帧率自动降级质量档位
// high → medium: 关 Bloom、星层减半
// medium → low: 关链路粒子、节点降到 6 段
function startPerfMonitor() {
  if (perfRAF) return
  frameCount = 0
  lastPerfTime = performance.now()
  const tick = () => {
    if (destroyed) return
    frameCount++
    const now = performance.now()
    const elapsed = now - lastPerfTime
    if (elapsed >= 500) {
      fps.value = Math.round((frameCount * 1000) / elapsed)
      if (fps.value < 30) {
        lowFpsStreak++
        if (lowFpsStreak >= 2 && qualityTier.value === 'high') {
          qualityTier.value = 'medium'
          applyQualityTier()
        } else if (lowFpsStreak >= 4 && qualityTier.value === 'medium') {
          qualityTier.value = 'low'
          applyQualityTier()
        }
      } else {
        lowFpsStreak = 0
      }
      frameCount = 0
      lastPerfTime = now
    }
    perfRAF = requestAnimationFrame(tick)
  }
  perfRAF = requestAnimationFrame(tick)
}

function applyQualityTier() {
  const g = graph.value
  if (!g) return
  const tier = qualityTier.value
  // Bloom 仅在 high 档启用
  if (bloomPass) bloomPass.enabled = (tier === 'high')
  // 链路粒子：low 档关闭，medium 档 1 个，high 档 2 个
  g.linkDirectionalParticles(tier === 'low' ? 0 : tier === 'medium' ? 1 : 2)
  // 节点分辨率：low 档 6 段，其余 8 段（原 20 段过高）
  g.nodeResolution(tier === 'low' ? 6 : 8)
}

// 开灯/关灯：Bloom 二态开关（high=灯开，medium=灯关）
function toggleLight() {
  qualityTier.value = qualityTier.value === 'high' ? 'medium' : 'high'
  lowFpsStreak = 0
  applyQualityTier()
}

// ── 点击涟漪 ──
// 在节点位置生成一个线框球体，向外扩散并淡出
function spawnRipple(x: number, y: number, z: number, color: string) {
  const g = graph.value
  if (!g) return
  const geo = new THREE.SphereGeometry(1, 16, 12)
  const mat = new THREE.MeshBasicMaterial({
    color: new THREE.Color(color),
    transparent: true,
    opacity: 0.6,
    wireframe: true,
    depthWrite: false,
  })
  const mesh = new THREE.Mesh(geo, mat)
  mesh.position.set(x, y, z)
  g.scene().add(mesh)
  ripples.push({ mesh, startTime: performance.now(), duration: 800 })
}

function updateRipples() {
  if (!ripples.length) return
  const g = graph.value
  const now = performance.now()
  ripples = ripples.filter(r => {
    const t = (now - r.startTime) / r.duration
    if (t >= 1) {
      g?.scene().remove(r.mesh)
      r.mesh.geometry.dispose()
      ;(r.mesh.material as THREE.Material).dispose()
      return false
    }
    // ease-out 扩散
    const scale = 1 + (1 - (1 - t) * (1 - t)) * 25
    r.mesh.scale.setScalar(scale)
    ;(r.mesh.material as THREE.MeshBasicMaterial).opacity = 0.6 * (1 - t)
    return true
  })
}

// ── 初始化 3D 场景 ──
function initGraph() {
  const el = containerEl.value
  if (!el || graph.value) return

  graph.value = new ForceGraph3D(el, {
    controlType: 'orbit',
    // 关闭 MSAA —— 高 DPI 屏上 MSAA 开销极大，改用 Bloom 的模糊自然平滑边缘
    rendererConfig: { antialias: false, alpha: false },
  })
  initialized = true

  const g = graph.value
  // 像素比上限 2.0 —— Windows 3x DPI 屏原本渲染 9 倍像素，限制后降至 4 倍
  try {
    g.renderer().setPixelRatio(Math.min(window.devicePixelRatio, 2))
  } catch { /* renderer 尚未就绪时忽略 */ }
  const w = el.clientWidth || window.innerWidth
  const h = el.clientHeight || window.innerHeight
  g.width(w).height(h)
  g.backgroundColor(BG_DEEP)

  // 节点外观：默认 sphere（保留 nodeColor 切换能力），尺寸按 val 缩放
  // nodeResolution 8 段（原 20 段过高，借鉴 Obsidian 粒子星图的低多边形策略）
  g.nodeRelSize(6)
    .nodeOpacity(1.0)
    .nodeResolution(8)
    .nodeColor((node: NodeObject) => colorForKind((node as GraphNode).kind))
    .nodeLabel((node: NodeObject) => {
      const n = node as GraphNode
      return `<div style="padding:4px 10px;border-radius:8px;background:var(--glass-bg);border:1px solid var(--glass-border);color:var(--moon);font-size:13px;">${escapeHtml(n.name)}${n.kind ? `<span style="margin-left:8px;color:var(--wisdom);font-size:11px;">${escapeHtml(n.kind)}</span>` : ''}</div>`
    })

  // 连线 + 粒子流
  g.linkColor(() => COLOR_LINK)
    .linkWidth(0.6)
    .linkOpacity(0.5)
    .linkDirectionalParticles(2)
    .linkDirectionalParticleSpeed(0.004)
    .linkDirectionalParticleWidth(0.5)
    .linkDirectionalParticleColor(() => COLOR_WISDOM)

  // 星空背景
  addStarLayers(g.scene())

  // Bloom 后处理（该版本 postProcessingComposer 自动含 RenderPass，引擎每帧自动 render）
  // 参数顺序: (resolution, strength, radius, threshold)
  // 降强度 + 提阈值，避免过曝刺眼，让节点本身的颜色更清晰
  if (props.enableBloom) {
    composer = g.postProcessingComposer()
    bloomPass = new UnrealBloomPass(new THREE.Vector2(w, h), 0.35, 0.4, 0.45)
    composer.addPass(bloomPass)
  }
  // 应用初始质量档位（默认 medium → 关 Bloom、减粒子）
  applyQualityTier()

  // OrbitControls 配置：Blender 风格，中键 PAN（默认是 DOLLY）
  const controls = g.controls() as any
  if (controls && controls.mouseButtons) {
    controls.mouseButtons.MIDDLE = THREE.MOUSE.PAN
  }

  // 星空自转循环（独立 RAF，仅更新 Points 旋转，渲染由引擎每帧执行）
  startStarLoop()

  // 性能监测 + 自适应降级（Performance API 帧率采集）
  startPerfMonitor()

  // 交互
  g.onNodeHover((node: NodeObject | null) => {
    hoveredNode.value = (node as GraphNode) || null
    updateHighlight()
    resetIdleTimer()
  })
    .onNodeClick((node: NodeObject) => {
      const n = node as GraphNode
      selectedNode.value = n
      updateHighlight()
      focusOnNode(n)
      spawnRipple(n.x ?? 0, n.y ?? 0, n.z ?? 0, colorForKind(n.kind))
      resetIdleTimer()
    })
    .onBackgroundClick(() => {
      selectedNode.value = null
      updateHighlight()
      resetIdleTimer()
    })
    .onNodeDrag(() => resetIdleTimer())
    .onNodeDragEnd(() => resetIdleTimer())
}

// ── 星空三层（Fibonacci 螺旋分布 + HSL 闪烁，借鉴 Obsidian 粒子星图知识）──
function addStarLayers(scene: THREE.Scene) {
  // 星层数量略减以兼顾性能；Fibonacci 分布比随机分布更均匀优雅
  const layers: Array<{ count: number; rMin: number; rMax: number; size: number; color: string; opacity: number; speed: number }> = [
    { count: 600, rMin: 800, rMax: 1000, size: 1, color: COLOR_DENDRO, opacity: 0.3, speed: 0.00015 },
    { count: 300, rMin: 400, rMax: 600, size: 1.5, color: COLOR_WISDOM, opacity: 0.5, speed: 0.00028 },
    { count: 150, rMin: 200, rMax: 300, size: 2, color: COLOR_MOON, opacity: 0.7, speed: 0.00045 },
  ]
  starLayers = layers.map(cfg => {
    const positions = new Float32Array(cfg.count * 3)
    // 顶点颜色：HSL 随机亮度，模拟星星闪烁
    const colors = new Float32Array(cfg.count * 3)
    const base = new THREE.Color(cfg.color)
    const hsl = { h: 0, s: 0, l: 0 }
    base.getHSL(hsl)
    for (let i = 0; i < cfg.count; i++) {
      // Fibonacci 球面分布：phi 均匀铺纬度，theta 螺旋铺经度 → 星云光带效果
      const phi = Math.acos(-1 + (2 * i) / cfg.count)
      const theta = Math.sqrt(cfg.count * Math.PI) * phi
      const r = cfg.rMin + Math.random() * (cfg.rMax - cfg.rMin)
      positions[i * 3] = r * Math.cos(theta) * Math.sin(phi)
      positions[i * 3 + 1] = r * Math.cos(phi)
      positions[i * 3 + 2] = r * Math.sin(theta) * Math.sin(phi)
      // HSL 随机亮度（0.6~1.0），让星星有明暗差异，模拟闪烁
      const c = new THREE.Color().setHSL(hsl.h, hsl.s, hsl.l * (0.6 + Math.random() * 0.4))
      colors[i * 3] = c.r
      colors[i * 3 + 1] = c.g
      colors[i * 3 + 2] = c.b
    }
    const geo = new THREE.BufferGeometry()
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))
    geo.setAttribute('color', new THREE.BufferAttribute(colors, 3))
    const mat = new THREE.PointsMaterial({
      size: cfg.size,
      vertexColors: true,
      transparent: true,
      opacity: cfg.opacity,
      sizeAttenuation: true,
      depthWrite: false,
    })
    const pts = new THREE.Points(geo, mat)
    scene.add(pts)
    return { points: pts, speed: cfg.speed }
  })
}

function startStarLoop() {
  if (starRAF) return
  const loop = () => {
    if (destroyed) return
    for (const layer of starLayers) {
      layer.points.rotation.y += layer.speed
      layer.points.rotation.x += layer.speed * 0.3
    }
    updateRipples()
    starRAF = requestAnimationFrame(loop)
  }
  starRAF = requestAnimationFrame(loop)
}

// ── hover/selected 高亮 ──
// 优先级：hoveredNode > selectedNode > 无
// hover 离开时若存在 selectedNode，保持其高亮（用户点击后还想查看关系）
function updateHighlight() {
  const g = graph.value
  if (!g) return
  const focus = hoveredNode.value || selectedNode.value
  if (!focus) {
    g.nodeColor((node: NodeObject) => colorForKind((node as GraphNode).kind))
    g.linkColor(() => COLOR_LINK)
    return
  }
  const id = focus.id as string
  const neighbors = neighborsCache.get(id) || new Set<string>()
  g.nodeColor((node: NodeObject) => {
    const nid = node.id as string
    return nid === id || neighbors.has(nid) ? colorForKind((node as GraphNode).kind) : COLOR_NODE_DIM
  })
  g.linkColor((link: any) => {
    const s = linkId(link.source)
    const t = linkId(link.target)
    return s === id || t === id ? COLOR_WISDOM : COLOR_LINK_DIM
  })
}

// ── 相机聚焦节点 ──
// 同步 controls.target 到节点位置，避免 OrbitControls 把相机拉回原 target（"弹回去"问题）
function focusOnNode(node: GraphNode) {
  const g = graph.value
  if (!g) return
  // 取消待执行的 zoomToFit，避免它在聚焦后把相机拉回全局视图（"弹回去"问题）
  if (zoomTimer) { clearTimeout(zoomTimer); zoomTimer = null }
  const tx = node.x ?? 0
  const ty = node.y ?? 0
  const tz = node.z ?? 0
  const controls = getOrbitControls()
  if (controls?.target) {
    controls.target.set(tx, ty, tz)
  }
  g.cameraPosition(
    { x: tx, y: ty, z: tz + 80 },
    { x: tx, y: ty, z: tz },
    600,
  )
}

// ── 实体检索闪烁 ──
function focusOnEntity(name: string) {
  const target = nodes.value.find(n => n.name === name || (n.id as string) === name)
  if (!target) {
    message.info(`未找到实体「${name}」`)
    return
  }
  focusOnNode(target)
  const g = graph.value
  if (!g) return
  const tid = target.id as string
  g.nodeColor((node: NodeObject) => (node.id as string) === tid ? '#ffffff' : colorForKind((node as GraphNode).kind))
  if (flashTimer) clearTimeout(flashTimer)
  flashTimer = setTimeout(() => {
    if (destroyed || !graph.value) return
    updateHighlight()
  }, 1000)
}

// ── 闲置公转 ──
function getOrbitControls(): OrbitLikeControls | null {
  const g = graph.value
  if (!g) return null
  const c = g.controls() as unknown
  return (c && typeof c === 'object') ? (c as OrbitLikeControls) : null
}

function resetIdleTimer() {
  const controls = getOrbitControls()
  if (controls) {
    controls.autoRotate = false
  }
  if (idleTimer) clearTimeout(idleTimer)
  idleTimer = setTimeout(() => {
    if (destroyed || !graph.value) return
    const c = getOrbitControls()
    if (c) {
      c.autoRotate = true
      c.autoRotateSpeed = 0.5
    }
  }, 5000)
}

// ── 深度切换 / 检索 ──
function setActiveDepth(d: 1 | 2) {
  activeDepth.value = d
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

// ── 尺寸同步 ──
function applyResize() {
  const el = containerEl.value
  const g = graph.value
  if (!el || !g) return
  const w = el.clientWidth
  const h = el.clientHeight
  if (w === 0 || h === 0) {
    g.pauseAnimation()
    return
  }
  g.resumeAnimation()
  g.width(w).height(h)
  if (bloomPass) bloomPass.setSize(w, h)
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
const lightLabel = computed(() => qualityTier.value === 'high' ? '关灯' : '开灯')

// ── 详情面板：选中节点的关系 ──
const selectedRelations = computed(() => {
  const node = selectedNode.value
  if (!node) return []
  const id = node.id as string
  return links.value
    .filter(l => linkId(l.source) === id || linkId(l.target) === id)
    .slice(0, 10)
    .map(l => ({
      relation: l.relation || '关联',
      other: linkId(l.source) === id ? linkId(l.target) : linkId(l.source),
    }))
})

// ── 生命周期 ──
onMounted(() => {
  webglUnavailable.value = !detectWebGL()
  ws.on('knowledge_graph_changed', onGraphChanged)

  // ResizeObserver：模态由 display 切换，容器尺寸从 0 变非 0 时再初始化
  if (containerEl.value) {
    resizeObserver = new ResizeObserver(() => {
      applyResize()
      const el = containerEl.value
      if (el && el.clientWidth > 0 && !graph.value && props.autoLoad && !webglUnavailable.value && !degraded.value) {
        loadGraph()
      }
    })
    resizeObserver.observe(containerEl.value)
  }
})

onBeforeUnmount(() => {
  destroyed = true
  ws.off('knowledge_graph_changed', onGraphChanged)
  if (idleTimer) clearTimeout(idleTimer)
  if (retryTimer) clearTimeout(retryTimer)
  if (debounceTimer) clearTimeout(debounceTimer)
  if (flashTimer) clearTimeout(flashTimer)
  if (zoomTimer) clearTimeout(zoomTimer)
  if (releaseTimer) clearTimeout(releaseTimer)
  if (starRAF) cancelAnimationFrame(starRAF)
  starRAF = 0
  if (perfRAF) cancelAnimationFrame(perfRAF)
  perfRAF = 0
  // 清理涟漪
  for (const r of ripples) {
    graph.value?.scene().remove(r.mesh)
    r.mesh.geometry.dispose()
    ;(r.mesh.material as THREE.Material).dispose()
  }
  ripples = []
  resizeObserver?.disconnect()
  resizeObserver = null
  if (graph.value) {
    graph.value._destructor()
    graph.value = null
  }
  composer = null
  bloomPass = null
  starLayers = []
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

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c] as string))
}

function kindLabel(kind?: string): string {
  if (!kind) return '实体'
  const map: Record<string, string> = {
    person: '人物', '人物': '人物',
    place: '地点', location: '地点', '地点': '地点',
    concept: '概念', '概念': '概念',
    event: '事件', '事件': '事件',
  }
  return map[kind.toLowerCase()] || map[kind] || kind
}
</script>

<template>
  <div class="universe-root">
    <!-- 顶部工具栏（可收起） -->
    <div class="universe-toolbar glass-panel" :class="{ collapsed: toolbarCollapsed }">
      <template v-if="!toolbarCollapsed">
        <n-input
          v-model:value="searchText"
          size="small"
          placeholder="搜索实体定位…"
          style="max-width: 200px"
          @keydown.enter="onSearchEnter"
        />
        <n-button
          size="tiny"
          :type="activeDepth === 1 ? 'primary' : 'default'"
          @click="setActiveDepth(1)"
        >深度1</n-button>
        <n-button
          size="tiny"
          :type="activeDepth === 2 ? 'primary' : 'default'"
          @click="setActiveDepth(2)"
        >深度2</n-button>
        <span class="universe-count">节点 {{ nodeCount }}</span>
        <span class="universe-fps" :class="fpsClass">{{ fps }} fps · {{ qualityTier }}</span>
        <n-button size="tiny" quaternary @click="toggleLight">{{ lightLabel }}</n-button>
        <n-button size="tiny" quaternary @click="loadGraph()">刷新</n-button>
        <n-button class="universe-close" size="tiny" type="primary" @click="emit('close')">✕ 关闭</n-button>
        <n-button size="tiny" quaternary @click="toolbarCollapsed = true">▴</n-button>
      </template>
      <n-button v-else size="tiny" quaternary @click="toolbarCollapsed = false">▾ 控制栏</n-button>
    </div>

    <!-- 3D 容器 -->
    <div ref="containerEl" class="universe-canvas" />

    <!-- 加载中 -->
    <div v-if="loading" class="universe-loading">
      <div class="sumeru-spinner" />
      <span>召唤须弥星图…</span>
    </div>

    <!-- 降级提示 -->
    <div v-if="degraded" class="universe-degraded glass-panel">
      <p>节点过多（{{ nodeCount }}），为避免卡顿已启用降级视图。</p>
      <n-button size="small" type="primary" @click="forceEnter3D">仍要进入 3D</n-button>
    </div>

    <!-- WebGL 不可用 -->
    <div v-if="webglUnavailable" class="universe-degraded glass-panel">
      <p>当前环境不支持 WebGL，无法渲染 3D 星图。</p>
      <n-button size="small" type="primary" @click="emit('close')">关闭</n-button>
    </div>

    <!-- 节点详情浮层 -->
    <div v-if="selectedNode" class="universe-detail glass-panel">
      <div class="detail-head">
        <span class="detail-name">{{ selectedNode.name }}</span>
        <n-tag size="tiny" :bordered="false">{{ kindLabel(selectedNode.kind) }}</n-tag>
        <span class="detail-degree">度数 {{ (selectedNode.val ?? 1) - 1 }}</span>
        <n-button size="tiny" quaternary @click="selectedNode = null">✕</n-button>
      </div>
      <div class="detail-relations">
        <div v-for="(r, i) in selectedRelations" :key="i" class="rel-row">
          <span class="rel-arrow">{{ r.other }}</span>
          <n-tag size="tiny" type="info" :bordered="false">{{ r.relation }}</n-tag>
        </div>
        <div v-if="!selectedRelations.length" class="rel-empty">暂无关系</div>
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
