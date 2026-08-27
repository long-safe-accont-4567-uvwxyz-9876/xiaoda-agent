/**
 * 纳西妲宇宙 —— three.js 场景引擎层（从 UniverseGraph.vue 拆出）
 *
 * 职责：ForceGraph3D 实例创建与外观配置、Bloom 后处理、世界树（worldTree）、
 * 星空背景、点击涟漪、交互事件绑定（hover/click/drag/dblclick）、hover 高亮、
 * 相机聚焦与复位、闲置公转、性能监测自适应降档、尺寸同步。
 *
 * 世界树生长模型（2026-08-25 专项）：
 *  - 程序化古树承担记忆容器：每个末梢枝尖是一个锚点，一球一梢；
 *  - 根记忆球（检索实体优先）坐上最居中的枝尖，BFS 把相关记忆安放到
 *    父节点枝尖附近的空梢 —— 关系近的记忆聚在同一根大枝上；
 *  - 弹簧跟随带惯性：拖拽松手有过冲回弹 → 枝条弹性手感；
 *  - 风摆公式与树共享同一时钟，球摇枝也摇。
 *
 * 帧率策略：
 *  - 链路粒子只挂在聚焦节点邻接边（曾为最大 GPU 开销的全量粒子流已移除，
 *    仅小图高档保留稀疏氛围流）；
 *  - Bloom 半分辨率渲染（辉光本身是模糊，减半无损观感）；
 *  - low 档像素比降至 1.25；d3 力模拟全部移除（O(n²) CPU 清零）。
 *
 * 生命周期红线（与拆分前语义逐条对应）：
 * - WebGL 上下文只经 init() 里的 new ForceGraph3D 创建；init() 可重复调用
 *   （>2000 节点降级后"仍要进入 3D"会重建实例），星空/性能循环经 RAF 句柄
 *   守卫不重复启动；重建时先析构旧的星空/树再重建（场景归属新实例）；
 * - destroyInstance(): 对应降级分支，仅析构 WebGL 实例，刻意保留循环运行，
 *   待重建后由启动守卫复用（行为保真关键点）;
 * - shutdown(): 组件卸载的完整销毁链，顺序：定时器 → 星空/性能 RAF →
 *   dblclick 解绑 → 涟漪 geometry/material → 树/星空 dispose → 渲染器 _destructor。
 *
 * 适配说明（installed v1.80）：
 *  - 该版本无 graph.onEngineRender / graph.cameraAutoOrbit。
 *  - Bloom 通过官方 graph.postProcessingComposer()（自动创建 EffectComposer +
 *    RenderPass，引擎每帧自动调用 composer.render()），仅追加 UnrealBloomPass 即可。
 *  - 闲置公转通过 controlType:'orbit' 的 OrbitControls.autoRotate 实现（引擎每帧调用 controls.update）。
 *  - 默认节点 mesh 带 __graphObjType='node' / __data 标记，生长动画据此定位缩放。
 */
import ForceGraph3D, { type NodeObject, type ForceGraph3DInstance } from '3d-force-graph'
import * as THREE from 'three'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'
import type { Ref } from 'vue'
import {
  BG_DEEP,
  COLOR_EXPANDED,
  COLOR_LINK,
  COLOR_LINK_DIM,
  COLOR_NODE_DIM,
  COLOR_WISDOM,
  colorForKind,
} from './theme'
import { buildNeighbors, escapeHtml, linkId } from './graphData'
import { createRippleManager } from './ripples'
import { createStarLayers, disposeStarLayers, spinStarLayers } from './starfield'
import { createPerfMonitor } from './perfMonitor'
import { buildWorldTree, type WorldTree } from './worldTree'
import { createMemoryPlacement } from './memoryPlacement'
import type { GraphLink, GraphNode, OrbitLikeControls, QualityTier, StarLayer } from './types'

export interface UniverseEngineContext {
  enableBloom: boolean
  expandedIds: Ref<Set<string>>
  hoveredNode: Ref<GraphNode | null>
  selectedNode: Ref<GraphNode | null>
  qualityTier: Ref<QualityTier>
  heavyEdges: Ref<boolean>
  fps: Ref<number>
  /** 单击节点的业务追加动作（增量拉邻域），在选中→高亮→聚焦→涟漪之后同步触发 */
  onExpandRequest: (node: GraphNode) => void
  /** 空枝尖芽点被点击（2026-08-27 记忆树编辑专项）：打开"新记忆"面板 */
  onBudClick?: () => void
}

export interface UniverseEngine {
  readonly hasInstance: boolean
  /** 创建 ForceGraph3D 实例并完成全部场景配置（降级后可再次调用以重建） */
  init(): void
  /** ">2000 节点降级"路径专用：仅析构实例，其余循环/定时器保持原状 */
  destroyInstance(): void
  /** 全量重载前调用：清空落梢/生长状态（WS 同步、刷新、深度切换）；rootId 为检索起步实体 */
  resetWorld(rootId?: string): void
  /**
   * 应用累积数据。新节点会落到父节点枝尖附近的空梢上（一球一梢）；
   * 大图（>阈值节点）先只挂节点、延后再挂全量边，getLatest 保证触发时读到最新累积数据
   */
  setData(nodes: GraphNode[], links: GraphLink[], getLatest?: () => { nodes: GraphNode[]; links: GraphLink[] }): void
  scheduleZoomToFit(delay?: number): void
  updateNeighborIndex(nodes: GraphNode[], links: GraphLink[]): void
  focusOnNode(node: GraphNode): void
  /** 实体检索闪烁：白亮目标节点 1s 后恢复高亮态 */
  flashNode(id: string): void
  resetView(): void
  toggleLight(): void
  applyResize(): void
  pauseAnimation(): void
  resumeAnimation(): void
  shutdown(): void
}

// ── WebGL 可用性检测 ──
export function detectWebGL(): boolean {
  try {
    const canvas = document.createElement('canvas')
    const ctx = canvas.getContext('webgl') || canvas.getContext('experimental-webgl')
    return !!ctx
  } catch {
    return false
  }
}

const IDLE_AUTOROTATE_DELAY_MS = 5000
const IDLE_AUTOROTATE_SPEED = 0.5
const DEFER_EDGES_DELAY_MS = 300
const DEFER_EDGES_NODE_THRESHOLD = 400
const ZOOMTOFIT_DELAY_MS = 1500
const FLASH_DURATION_MS = 1000
const GROWTH_DURATION_MS = 900     // 新节点破土生长动画时长
const RECENT_SPAWN_MS = 2600       // 新生记忆的"实体色宽限期"（防聚焦调暗成虚影）

// ── 记忆球弹簧（位置所有权在引擎，不在 d3）──
// 每帧把节点拉向"锚点经风摆+树冠倾斜变换后的目标点"，带速度惯性：
// 松手回弹有过冲 → 枝条弹性手感；d3 力模拟全部移除（O(n²) CPU 清零）
const SPRING_STRENGTH = 0.085
const SPRING_DAMPING = 0.85
const TETHER_SHOW_DIST = 10        // 锚点距超过该值显示橡皮筋
const LEAN_MAX = 0.06              // 树冠最大倾斜（弧度）
const LEAN_FACTOR = 0.0007         // 拖拽位移 → 倾斜角系数

/** easeOutBack：先回缩再弹出的"抽芽"曲线 */
function easeOutBack(p: number): number {
  const c1 = 1.70158
  const c3 = c1 + 1
  return 1 + c3 * Math.pow(p - 1, 3) + c1 * Math.pow(p - 1, 2)
}

export function createUniverseEngine(
  containerEl: Ref<HTMLDivElement | null>,
  ctx: UniverseEngineContext,
): UniverseEngine {
  let instance: ForceGraph3DInstance | null = null
  let composer: ReturnType<ForceGraph3DInstance['postProcessingComposer']> | null = null
  let bloomPass: UnrealBloomPass | null = null
  let starLayers: StarLayer[] = []
  let starScene: THREE.Scene | null = null
  let starRAF = 0
  let neighborsCache = new Map<string, Set<string>>()
  let idleTimer: ReturnType<typeof setTimeout> | null = null
  let zoomTimer: ReturnType<typeof setTimeout> | null = null
  let releaseTimer: ReturnType<typeof setTimeout> | null = null
  let flashTimer: ReturnType<typeof setTimeout> | null = null
  let alive = true

  // ── 世界树状态 ──
  let tree: WorldTree | null = null
  let treeScene: THREE.Scene | null = null
  const knownIds = new Set<string>()                       // 已落梢的节点 id

  // ── 记忆球落梢（算法在 memoryPlacement.ts，2026-08-25 门禁专项抽出）──
  const raycaster = new THREE.Raycaster()
  const placement = createMemoryPlacement({
    getTree: () => tree,
    getRootHintId: () => rootHintId,
    bumpCanopyCursor: () => canopyCursor++,
    getNeighborsCache: () => neighborsCache,
  })
  let budSyncCounter = 0
  /** 空枝尖 → 芽点集合；几何按需重建（点击拾取在 onCanvasClick） */
  function syncBudOccupancy(): void {
    if (!tree) return
    let changed = false
    const n = Math.min(tree.anchors.length, 1 << 12)
    for (let i = 0; i < n; i++) {
      const used = placement.isAnchorUsed(i)
      if (used && tree.budIndices.has(i)) { tree.budIndices.delete(i); changed = true }
      else if (!used && !tree.budIndices.has(i)) { tree.budIndices.add(i); changed = true }
    }
    if (changed) tree.rebuildBudGeometry()
  }
  const spawnTimes = new Map<string, number>()             // 生长动画：id → 出生时刻
  const recentSpawn = new Map<string, number>()            // 新生宽限期：宽限内不被聚焦调暗
  const springState = new Map<string, { sx: number; sy: number; sz: number }>()
  let rootHintId = ''                                      // 检索起步实体（优先作为根球）
  let canopyCursor = 0                                     // 树冠兜底螺旋点游标
  let tierNodeCount = 0                                    // 当前图规模（起始画质档下压用）
  let totalLinks = 0
  let liveNodes: GraphNode[] | null = null                 // 当前图节点（弹簧循环用，避免每帧 graphData()）
  // 树冠倾斜（拖拽拨动树枝）
  let leanCurX = 0
  let leanCurZ = 0
  let leanTargetX = 0
  let leanTargetZ = 0
  let draggedId: string | null = null
  // 橡皮筋（锚点↔被拖离的节点）
  const tethers = new Map<string, { line: THREE.Line; mat: THREE.LineBasicMaterial; geo: THREE.BufferGeometry }>()
  const windV = new THREE.Vector3()

  const ripples = createRippleManager()
  // 手动开灯优先（2026-08-27 真机反馈"开灯一直自动关"）：用户点开灯后，
  // 帧率监测不再自动降档关灯——低帧率只让 fps 徽标变红提示，是否关灯
  // 由用户决定。重载/降档重建后标记复位
  let manualLightPinned = false
  const perf = createPerfMonitor({
    fps: ctx.fps,
    qualityTier: ctx.qualityTier,
    isAlive: () => alive,
    onTierApplied: applyQualityTier,
    /** 手动开灯期间禁止自动降档（灯是用户点名要的） */
    autoDowngradeAllowed: () => !manualLightPinned,
  })

  function getOrbitControls(): OrbitLikeControls | null {
    if (!instance) return null
    const c = instance.controls() as unknown
    return (c && typeof c === 'object') ? (c as OrbitLikeControls) : null
  }

  function applyQualityTier(): void {
    const g = instance
    if (!g) return
    // 边数过载时强制 medium 起步：Bloom 关、粒子走聚焦模式
    if (ctx.heavyEdges.value && ctx.qualityTier.value === 'high') {
      ctx.qualityTier.value = 'medium'
    }
    const tier = ctx.qualityTier.value
    // Bloom 仅在 high 档启用
    if (bloomPass) bloomPass.enabled = (tier === 'high')
    // 像素比分档：medium 1.5 / low 1.25（ARM/集显设备的主要帧率救星之一）
    try {
      const prCap = tier === 'low' ? 1.25 : tier === 'medium' ? 1.5 : 2
      g.renderer().setPixelRatio(Math.min(window.devicePixelRatio, prCap))
    } catch { /* renderer 尚未就绪时忽略 */ }
    applyLinkParticles()
    // 节点分辨率：low 档 6 段，其余 8 段（原 20 段过高）
    g.nodeResolution(tier === 'low' ? 6 : 8)
  }

  // 节点数越多，每帧 draw call 越多 —— 起始画质档随规模自动下压（只降不升）：
  // >650 节点或 >900 边起步 medium；>1400 节点直接 low
  function applyTierFloor(): void {
    const order: Record<QualityTier, number> = { low: 0, medium: 1, high: 2 }
    let floor: QualityTier = 'high'
    if (tierNodeCount > 650 || ctx.heavyEdges.value) floor = 'medium'
    if (tierNodeCount > 1400) floor = 'low'
    if (order[ctx.qualityTier.value] > order[floor]) {
      ctx.qualityTier.value = floor
      perf.resetStreak()
    }
  }

  // ── 链路粒子：只挂在聚焦节点邻接边 ──
  // 全量粒子流曾是最大 GPU 开销；现在空闲态仅小图高档保留稀疏氛围流，
  // hover/选中时邻接边亮起粒子流，聚焦感反而更强。
  // 记忆化：accessor 按引用比对，重复 setter 会触发 lib 全量 link 对象重建
  // （大图 hover 即卡顿）——焦点/档位/规模未变化时跳过。
  let lastParticleKey = ''
  function applyLinkParticles(): void {
    const g = instance
    if (!g) return
    const tier = ctx.qualityTier.value
    const focusId = ctx.hoveredNode.value?.id ?? ctx.selectedNode.value?.id ?? null
    const key = `${focusId ?? ''}|${tier}|${totalLinks < 250 ? 's' : 'b'}`
    if (key === lastParticleKey) return
    lastParticleKey = key
    if (focusId != null) {
      const mult = tier === 'low' ? 1 : tier === 'medium' ? 1 : 2
      g.linkDirectionalParticles((link: any) => {
        const s = linkId(link.source)
        const t = linkId(link.target)
        return (s === focusId || t === focusId) ? mult : 0
      })
      return
    }
    const ambient = tier === 'high' && totalLinks < 250 ? 1 : 0
    g.linkDirectionalParticles(ambient)
  }

  function resetIdleTimer(): void {
    const controls = getOrbitControls()
    if (controls) {
      controls.autoRotate = false
    }
    if (idleTimer) clearTimeout(idleTimer)
    idleTimer = setTimeout(() => {
      if (!alive || !instance) return
      const c = getOrbitControls()
      if (c) {
        c.autoRotate = true
        c.autoRotateSpeed = IDLE_AUTOROTATE_SPEED
      }
    }, IDLE_AUTOROTATE_DELAY_MS)
  }

  // ── hover/selected 高亮 ──
  // 优先级：hoveredNode > selectedNode > 无
  // hover 离开时若存在 selectedNode，保持其高亮（用户点击后还想查看关系）
  function refreshHighlight(): void {
    const g = instance
    if (!g) return
    const focus = ctx.hoveredNode.value || ctx.selectedNode.value
    // 未聚焦时：已展开节点用亮金色，提示"该邻域已加载"；其余按类别配色
    if (!focus) {
      g.nodeColor((node: NodeObject) => {
        const nid = node.id as string
        return ctx.expandedIds.value.has(nid) ? COLOR_EXPANDED : colorForKind((node as GraphNode).kind)
      })
      g.linkColor(() => COLOR_LINK)
      applyLinkParticles()
      return
    }
    const id = focus.id as string
    const neighbors = neighborsCache.get(id) || new Set<string>()
    g.nodeColor((node: NodeObject) => {
      const nid = node.id as string
      if (nid === id) return '#ffffff'
      if (neighbors.has(nid)) return colorForKind((node as GraphNode).kind)
      // 新生记忆宽限期内保持实体色，避免刚长出的球被聚焦调暗成"虚影"
      if (recentSpawn.has(nid)) {
        return ctx.expandedIds.value.has(nid) ? COLOR_EXPANDED : colorForKind((node as GraphNode).kind)
      }
      return ctx.expandedIds.value.has(nid) ? COLOR_EXPANDED : COLOR_NODE_DIM
    })
    g.linkColor((link: any) => {
      const s = linkId(link.source)
      const t = linkId(link.target)
      return s === id || t === id ? COLOR_WISDOM : COLOR_LINK_DIM
    })
    applyLinkParticles()
  }

  // ── 相机聚焦节点 ──
  // 同步 controls.target 到节点位置，避免 OrbitControls 把相机拉回原 target（"弹回去"问题）
  function focusOnNode(node: GraphNode): void {
    const g = instance
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
      { x: tx, y: ty, z: tz + 110 },
      { x: tx, y: ty, z: tz },
      600,
    )
  }

  function flashNode(id: string): void {
    const g = instance
    if (!g) return
    g.nodeColor((node: NodeObject) => (node.id as string) === id ? '#ffffff' : colorForKind((node as GraphNode).kind))
    if (flashTimer) clearTimeout(flashTimer)
    flashTimer = setTimeout(() => {
      if (!alive || !instance) return
      refreshHighlight()
    }, FLASH_DURATION_MS)
  }

  function resetView(): void {
    const g = instance
    if (!g) return
    ctx.selectedNode.value = null
    refreshHighlight()
    const controls = getOrbitControls()
    if (controls?.target) controls.target.set(0, 0, 0)
    g.zoomToFit(600, 100)
  }

  // 双击背景复位（init 时绑定；dblclick 不与单击选中冲突）
  const onDblClickReset = () => {
    // 双击落在节点上时不复位（保留节点聚焦行为）
    if (ctx.hoveredNode.value) return
    resetView()
  }

  function startStarLoop(): void {
    if (starRAF) return
    let lastT = performance.now()
    const loop = () => {
      if (!alive) return
      // dt 驱动树动画（rAF 在页面隐藏时被浏览器自动节流，无需显式中断循环）
      const now = performance.now()
      const dt = Math.min(0.05, (now - lastT) / 1000)
      lastT = now
      spinStarLayers(starLayers)
      tree?.update(dt)
      // 芽点占用状态低频同步（枝尖占用仅随落梢/删除变化，30 帧一次足够）
      if (tree && (++budSyncCounter % 30 === 0)) syncBudOccupancy()
      applyTreeSprings(dt)
      ripples.update(instance ? instance.scene() : null)
      growthStep(now)
      starRAF = requestAnimationFrame(loop)
    }
    starRAF = requestAnimationFrame(loop)
  }

  // ── 橡皮筋：锚点 ↔ 被拖离节点 的弹性枝条 ──
  const TETHER_COLOR_REST = new THREE.Color(COLOR_WISDOM)
  const TETHER_COLOR_TENSE = new THREE.Color('#fbbf24')

  function tetherUpdate(scene: THREE.Scene, id: string, ax: number, ay: number, az: number, nx: number, ny: number, nz: number): void {
    let t = tethers.get(id)
    if (!t) {
      const geo = new THREE.BufferGeometry()
      geo.setAttribute('position', new THREE.Float32BufferAttribute(new Float32Array(6), 3))
      const mat = new THREE.LineBasicMaterial({
        color: COLOR_WISDOM,
        transparent: true,
        opacity: 0,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      })
      const line = new THREE.Line(geo, mat)
      line.frustumCulled = false
      scene.add(line)
      t = { line, mat, geo }
      tethers.set(id, t)
    }
    const dist = Math.hypot(nx - ax, ny - ay, nz - az)
    const stretch = Math.min(1, dist / 140)
    const arr = t.geo.attributes.position.array as Float32Array
    arr[0] = ax; arr[1] = ay; arr[2] = az
    arr[3] = nx; arr[4] = ny; arr[5] = nz
    t.geo.attributes.position.needsUpdate = true
    t.mat.color.copy(TETHER_COLOR_REST).lerp(TETHER_COLOR_TENSE, stretch)
    t.mat.opacity = Math.min(0.9, 0.25 + stretch * 0.65)
  }

  function tetherFade(id: string): void {
    const t = tethers.get(id)
    if (!t) return
    t.mat.opacity *= 0.85
    if (t.mat.opacity < 0.02) {
      t.line.removeFromParent()
      t.geo.dispose()
      t.mat.dispose()
      tethers.delete(id)
    }
  }

  function tethersDispose(): void {
    for (const t of tethers.values()) {
      t.line.removeFromParent()
      t.geo.dispose()
      t.mat.dispose()
    }
    tethers.clear()
  }

  // ── 记忆球弹簧：位置所有权在引擎 ──
  // 目标点 = 锚点 + 风摆位移，再经树冠倾斜旋转（与 tree.group.rotation 同步），
  // 弹簧带速度惯性 → 拖拽松手有过冲回弹，呈现"枝条弹性"。
  // 拖拽中的节点（lib 设置 fx/fy/fz）跳过积分，改显示橡皮筋并驱动树冠倾斜目标。
  const leanClamp = (v: number) => Math.max(-LEAN_MAX, Math.min(LEAN_MAX, v))

  function applyTreeSprings(dt: number): void {
    if (!tree || !liveNodes || !instance) return
    const f = Math.min(2, dt * 60)          // 帧率归一化
    const damp = Math.pow(SPRING_DAMPING, f)

    // 树冠倾斜弹簧 → 应用到树群组（绕基座原点旋转，树像被拨动的枝头）
    leanCurX += (leanTargetX - leanCurX) * 0.07 * f
    leanCurZ += (leanTargetZ - leanCurZ) * 0.07 * f
    if (!draggedId) { leanTargetX *= 0.92; leanTargetZ *= 0.92 }
    tree.group.rotation.set(leanCurX, 0, leanCurZ)

    const gy = tree.group.position.y
    const cx = Math.cos(leanCurX); const sx = Math.sin(leanCurX)
    const cz = Math.cos(leanCurZ); const sz = Math.sin(leanCurZ)
    const scene = instance.scene()

    for (let i = 0; i < liveNodes.length; i++) {
      const n = liveNodes[i]
      const a = n.__anchor
      if (!a) continue
      const id = n.id as string

      if (n.fx !== undefined || n.fy !== undefined || n.fz !== undefined) {
        // 拖拽中：橡皮筋 + 拨动树冠
        tetherUpdate(scene, id, a.x, a.y, a.z, n.x ?? 0, n.y ?? 0, n.z ?? 0)
        leanTargetX = leanClamp(((n.z ?? 0) - a.z) * LEAN_FACTOR)
        leanTargetZ = leanClamp(-((n.x ?? 0) - a.x) * LEAN_FACTOR)
        continue
      }

      // 目标点：锚点（局部）+ 风摆，绕基座旋转后回到场景坐标
      let lx = a.x
      let ly = a.y - gy
      let lz = a.z
      tree.windOffset(ly, tree.time, windV)
      lx += windV.x; lz += windV.z
      // rotation order 'XYZ'：v' = Rx · Rz · v
      const rx1 = lx * cz - ly * sz
      const ry1 = lx * sz + ly * cz
      const ry2 = ry1 * cx - lz * sx
      const rz2 = ry1 * sx + lz * cx
      const tx = rx1
      const ty = ry2 + gy
      const tz = rz2

      let st = springState.get(id)
      if (!st) { st = { sx: 0, sy: 0, sz: 0 }; springState.set(id, st) }
      st.sx = st.sx * damp + (tx - (n.x ?? 0)) * SPRING_STRENGTH * f
      st.sy = st.sy * damp + (ty - (n.y ?? 0)) * SPRING_STRENGTH * f
      st.sz = st.sz * damp + (tz - (n.z ?? 0)) * SPRING_STRENGTH * f
      n.x = (n.x ?? 0) + st.sx * f
      n.y = (n.y ?? 0) + st.sy * f
      n.z = (n.z ?? 0) + st.sz * f

      // 松手回弹期间保持橡皮筋可见，靠近后淡出
      // 距离按风摆+倾斜后的稳定目标点计算，树冠倾斜时不产生幻影橡皮筋
      const dist = Math.hypot((n.x ?? 0) - tx, (n.y ?? 0) - ty, (n.z ?? 0) - tz)
      if (dist > TETHER_SHOW_DIST && (tethers.has(id) || dist > TETHER_SHOW_DIST * 3)) {
        tetherUpdate(scene, id, a.x, a.y, a.z, n.x ?? 0, n.y ?? 0, n.z ?? 0)
      } else {
        tetherFade(id)
      }
    }
  }

  // ── 新节点破土生长：scale 从 0 以 easeOutBack 弹到 1 ──
  // 定位方式：three-forcegraph v1.80 在节点数据上回写 __threeObj（lib objBindAttr），
  // 直接按节点取 mesh，避免每帧全场景 traverse；超龄孤儿条目兜底清理防空转。
  function growthStep(now: number): void {
    // 宽限期到期的新生记忆移出保护集
    if (recentSpawn.size) {
      const cutoff = now - RECENT_SPAWN_MS
      for (const [k, v] of recentSpawn) if (v < cutoff) recentSpawn.delete(k)
    }
    if (spawnTimes.size) {
      // 超过动画时长 4 倍仍未完成的条目视为孤儿（对象已被重建等），直接丢弃
      const stale = now - GROWTH_DURATION_MS * 4
      for (const [k, v] of spawnTimes) if (v < stale) spawnTimes.delete(k)
    }
    if (!spawnTimes.size || !liveNodes) return
    for (let i = 0; i < liveNodes.length; i++) {
      const n = liveNodes[i]
      const id = n.id as string
      const t0 = spawnTimes.get(id)
      if (t0 == null) continue
      const obj = (n as any).__threeObj as THREE.Object3D | undefined
      if (!obj) continue
      const p = Math.min(1, (now - t0) / GROWTH_DURATION_MS)
      obj.scale.setScalar(Math.max(0.001, easeOutBack(p)))
      if (p >= 1) { obj.scale.setScalar(1); spawnTimes.delete(id) }
    }
  }

  // ── 初始化 3D 场景 ──
  function init(): void {
    const el = containerEl.value
    if (!el || instance) return

    instance = new ForceGraph3D(el, {
      controlType: 'orbit',
      // 关闭 MSAA —— 高 DPI 屏上 MSAA 开销极大，改用 Bloom 的模糊自然平滑边缘
      rendererConfig: { antialias: false, alpha: false },
    })

    const g = instance
    // 像素比上限 2.0（low 档降到 1.25，见 applyQualityTier）
    try {
      g.renderer().setPixelRatio(Math.min(window.devicePixelRatio, 2))
    } catch { /* renderer 尚未就绪时忽略 */ }
    const w = el.clientWidth || window.innerWidth
    const h = el.clientHeight || window.innerHeight
    g.width(w).height(h)
    g.backgroundColor(BG_DEEP)

    // 节点外观：默认 sphere（保留 nodeColor 切换能力）
    // 尺寸压平：val 只作度数参考，实际半径按温和曲线封顶 —— 记忆球大小相近，
    // 枢纽节点不再巨大化挤压枝尖布局
    g.nodeRelSize(6)
      .nodeVal((n: NodeObject) => 1 + Math.min(2.4, (((n as GraphNode).val ?? 1) - 1) * 0.32))
      .nodeOpacity(1.0)
      .nodeResolution(8)
      .nodeColor((node: NodeObject) => ctx.expandedIds.value.has(node.id as string)
        ? COLOR_EXPANDED : colorForKind((node as GraphNode).kind))
      .nodeLabel((node: NodeObject) => {
        const n = node as GraphNode
        return `<div style="padding:4px 10px;border-radius:8px;background:var(--glass-bg);border:1px solid var(--glass-border);color:var(--moon);font-size:13px;">${escapeHtml(n.name)}${n.kind ? `<span style="margin-left:8px;color:var(--wisdom);font-size:11px;">${escapeHtml(n.kind)}</span>` : ''}</div>`
      })

    // 连线 + 聚焦粒子流（全量粒子已移除，见 applyLinkParticles）
    g.linkColor(() => COLOR_LINK)
      .linkWidth(0.75)
      .linkOpacity(0.55)
      .linkDirectionalParticleSpeed(0.004)
      .linkDirectionalParticleWidth(0.5)
      .linkDirectionalParticleColor(() => COLOR_WISDOM)

    // ── 世界树 ──
    // 重建路径（降级后"仍要进入 3D"）会创建新 scene：先析构旧星空/树再重建，
    // 避免几何体泄漏到已销毁的上下文
    if (starScene) { disposeStarLayers(starLayers, starScene); starLayers = []; starScene = null }
    if (tree && treeScene) { tree.dispose(treeScene); tree = null }

    starScene = g.scene()
    starLayers = createStarLayers(starScene)

    tree = buildWorldTree(starScene)
    g.cameraPosition(
      { x: tree.viewDistance * 0.12, y: tree.viewDistance * 0.06, z: tree.viewDistance },
      { x: 0, y: 0, z: 0 },
      0,
    )

    // ── 力模拟全部移除 ──
    // 节点位置完全由引擎的"锚点弹簧 + 风摆 + 树冠倾斜"接管：
    // 斥力(center/charge)是 O(n²) CPU 大头，也是把节点推离树枝的元凶；
    // link 力不再需要 —— 连线只是锚点间关系的可视化。
    g.d3Force('charge', null)
    g.d3Force('center', null)
    g.d3Force('link', null)

    // Bloom 后处理（该版本 postProcessingComposer 自动含 RenderPass，引擎每帧自动 render）
    // 半分辨率渲染：辉光本身是模糊效果，减半几乎无损观感、显著省 GPU
    // 参数顺序: (resolution, strength, radius, threshold)
    if (ctx.enableBloom) {
      composer = g.postProcessingComposer()
      bloomPass = new UnrealBloomPass(
        new THREE.Vector2(Math.max(320, Math.floor(w / 2)), Math.max(240, Math.floor(h / 2))),
        0.4, 0.5, 0.4,
      )
      composer.addPass(bloomPass)
    }
    // 应用初始质量档位（默认 medium → 关 Bloom；粒子走聚焦模式）
    applyQualityTier()

    // OrbitControls 配置：Blender 风格，中键 PAN（默认是 DOLLY）；开阻尼让相机带惯性
    const controls = g.controls() as any
    if (controls && controls.mouseButtons) {
      controls.mouseButtons.MIDDLE = THREE.MOUSE.PAN
    }
    try {
      if (controls) {
        controls.enableDamping = true
        controls.dampingFactor = 0.08
      }
    } catch { /* 控件能力缺失时忽略 */ }

    // 星空自转 + 世界树动画 + 生长动画（独立 RAF，仅更新对象属性，渲染由引擎每帧执行）
    startStarLoop()

    // 性能监测 + 自适应降级（Performance API 帧率采集）
    perf.start()

    // 交互
    g.onNodeHover((node: NodeObject | null) => {
      ctx.hoveredNode.value = (node as GraphNode) || null
      refreshHighlight()
      resetIdleTimer()
    })
      .onNodeClick((node: NodeObject) => {
        const n = node as GraphNode
        ctx.selectedNode.value = n
        refreshHighlight()
        focusOnNode(n)
        if (instance) {
          ripples.spawn(instance.scene(), n.x ?? 0, n.y ?? 0, n.z ?? 0, colorForKind(n.kind))
        }
        // 按需展开：单击节点增量拉取其邻域（已展开过则跳过）
        ctx.onExpandRequest(n)
        resetIdleTimer()
      })
      .onBackgroundClick(() => {
        ctx.selectedNode.value = null
        refreshHighlight()
        resetIdleTimer()
      })
      .onNodeDrag((node: NodeObject) => {
        draggedId = (node.id as string) ?? null
        resetIdleTimer()
      })
      .onNodeDragEnd(() => {
        draggedId = null
        resetIdleTimer()
      })

    // 双击空白处复位全局视角（全屏模式快速回到总览）
    el.addEventListener('dblclick', onDblClickReset)

    // 空枝尖芽点拾取（2026-08-27 记忆树编辑专项）：pointerdown/up 位移 <6px
    // 视为点击（区别于轨道旋转拖拽），Raycaster 对芽点 Points 阈值拾取。
    // 竞争修复（真机反馈"点记忆球也弹新建"）：hoveredNode 非空 = 指针在
    // 记忆球上，本轮放行给 onNodeClick，芽点拾取不参与——球与芽点同挂枝尖
    // 附近，几何上必然重叠，必须按"谁先命中"仲裁而非各自独立触发。
    // threshold 按屏幕像素反算（世界单位会随相机距离失真）：
    //   world ≈ 2·dist·tan(fov/2)·px / viewportH
    let downX = 0, downY = 0
    el.addEventListener('pointerdown', (e: PointerEvent) => { downX = e.clientX; downY = e.clientY })
    el.addEventListener('pointerup', (e: PointerEvent) => {
      if (!ctx.onBudClick || !tree || !tree.budIndices.size) return
      if (Math.hypot(e.clientX - downX, e.clientY - downY) > 6) return
      if (ctx.hoveredNode.value) return  // 指针在记忆球上：交给节点点击链路
      const rect = el.getBoundingClientRect()
      const ndc = new THREE.Vector2(
        ((e.clientX - rect.left) / rect.width) * 2 - 1,
        -((e.clientY - rect.top) / rect.height) * 2 + 1)
      const cam = g.camera() as THREE.PerspectiveCamera
      const dist = cam.position.length()
      const pxToWorld = 2 * dist * Math.tan((cam.fov * Math.PI / 180) / 2) / rect.height
      raycaster.setFromCamera(ndc, cam)
      raycaster.params.Points = { threshold: Math.max(1.5, pxToWorld * 14) }  // ~14px 半径
      const hits = raycaster.intersectObject(tree.budPoints, false)
      if (hits.length) { ctx.selectedNode.value = null; ctx.onBudClick(); resetIdleTimer() }
    })
  }

  function destroyInstance(): void {
    if (!instance) return
    instance._destructor()
    instance = null
    liveNodes = null
    draggedId = null
    tethersDispose() // 橡皮筋挂在已销毁的 scene 上，直接弃置
  }


  function resetWorld(rootId?: string): void {
    knownIds.clear()
    manualLightPinned = false  // 全量重载：灯回到自动调节
    placement.reset()
    spawnTimes.clear()
    recentSpawn.clear()
    tethersDispose() // 重载时清空橡皮筋，防旧线悬空残影
    springState.clear()
    rootHintId = rootId ?? ''
    canopyCursor = 0
    draggedId = null
    leanCurX = leanCurZ = leanTargetX = leanTargetZ = 0
  }

  function setData(
    nodes: GraphNode[],
    links: GraphLink[],
    getLatest?: () => { nodes: GraphNode[]; links: GraphLink[] },
  ): void {
    const g = instance
    if (!g) return

    // 新增节点 → 落梢到父节点枝尖附近的空梢（一球一梢）
    const fresh = nodes.filter(n => !knownIds.has(n.id as string))
    placement.assignAnchors(fresh)
    syncBudOccupancy()
    const now = performance.now()
    for (const n of fresh) {
      knownIds.add(n.id as string)
      if (!spawnTimes.has(n.id as string)) spawnTimes.set(n.id as string, now)
      recentSpawn.set(n.id as string, now)
    }
    if (fresh.length && tree) tree.pulseGrowth()
    // 极端大图兜底：截断最旧的生长记录防泄漏
    if (spawnTimes.size > 800) {
      const stale: string[] = []
      for (const k of spawnTimes.keys()) {
        stale.push(k)
        if (stale.length > 400) break
      }
      for (const k of stale) spawnTimes.delete(k)
    }

    totalLinks = links.length
    liveNodes = nodes
    tierNodeCount = nodes.length
    applyTierFloor()
    applyQualityTier()

    // 大图先只挂节点、延后再挂边：力导向先把树冠上的节点理顺，避免初始帧卡死
    const deferEdges = nodes.length > DEFER_EDGES_NODE_THRESHOLD
    g.graphData({
      nodes: nodes as unknown as NodeObject[],
      links: (deferEdges ? [] : links) as unknown as GraphLink[],
    } as any)
    if (deferEdges && getLatest) {
      if (releaseTimer) clearTimeout(releaseTimer)
      releaseTimer = setTimeout(() => {
        if (!alive || !instance || !getLatest) return
        const latest = getLatest()
        instance.graphData({
          nodes: latest.nodes as unknown as NodeObject[],
          links: latest.links as unknown as GraphLink[],
        } as any)
      }, DEFER_EDGES_DELAY_MS)
    }
  }

  function scheduleZoomToFit(delay: number = ZOOMTOFIT_DELAY_MS): void {
    zoomTimer = setTimeout(() => {
      if (!alive || !instance) return
      instance.zoomToFit(500, 100)
    }, delay)
  }

  function updateNeighborIndex(nodes: GraphNode[], links: GraphLink[]): void {
    neighborsCache = buildNeighbors(nodes, links)
  }

  // ── 尺寸同步 ──
  function applyResize(): void {
    const el = containerEl.value
    if (!el || !instance) return
    const w = el.clientWidth
    const h = el.clientHeight
    if (w === 0 || h === 0) {
      instance.pauseAnimation()
      return
    }
    instance.resumeAnimation()
    instance.width(w).height(h)
    // Bloom 与主画布同降半分辨率（见 init 中说明）
    if (bloomPass) bloomPass.setSize(Math.max(320, Math.floor(w / 2)), Math.max(240, Math.floor(h / 2)))
  }

  function pauseAnimation(): void {
    if (!instance) return
    instance.pauseAnimation()
  }

  function resumeAnimation(): void {
    if (!instance) return
    instance.resumeAnimation()
  }

  // 开灯/关灯：Bloom 二态开关（high=灯开，medium=灯关）
  function toggleLight(): void {
    ctx.qualityTier.value = ctx.qualityTier.value === 'high' ? 'medium' : 'high'
    // 开灯 = 用户手动要求 high：此后帧率监测不得再自动关灯
    manualLightPinned = ctx.qualityTier.value === 'high'
    perf.resetStreak()
    applyQualityTier()
  }

  function shutdown(): void {
    alive = false
    if (idleTimer) { clearTimeout(idleTimer); idleTimer = null }
    if (flashTimer) { clearTimeout(flashTimer); flashTimer = null }
    if (zoomTimer) { clearTimeout(zoomTimer); zoomTimer = null }
    if (releaseTimer) { clearTimeout(releaseTimer); releaseTimer = null }
    if (starRAF) cancelAnimationFrame(starRAF)
    starRAF = 0
    perf.stop()
    containerEl.value?.removeEventListener('dblclick', onDblClickReset)
    // 清理涟漪（geometry/material 先于渲染器销毁，时序与拆分前一致）
    ripples.disposeAll(instance ? instance.scene() : null)
    tethersDispose()
    if (tree && treeScene) {
      tree.dispose(treeScene)
      tree = null
    }
    treeScene = null
    if (starScene) {
      disposeStarLayers(starLayers, starScene)
      starScene = null
    }
    starLayers = []
    if (instance) {
      instance._destructor()
      instance = null
    }
    composer = null
    bloomPass = null
    neighborsCache = new Map()
    liveNodes = null
    draggedId = null
    knownIds.clear()
    placement.reset()
    rootHintId = ''
    canopyCursor = 0
    tierNodeCount = 0
    springState.clear()
    spawnTimes.clear()
    recentSpawn.clear()
  }

  return {
    get hasInstance() { return !!instance },
    init,
    destroyInstance,
    resetWorld,
    setData,
    scheduleZoomToFit,
    updateNeighborIndex,
    focusOnNode,
    flashNode,
    resetView,
    toggleLight,
    applyResize,
    pauseAnimation,
    resumeAnimation,
    shutdown,
  }
}
