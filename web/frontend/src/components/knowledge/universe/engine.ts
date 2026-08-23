/**
 * 纳西妲宇宙 —— three.js 场景引擎层（从 UniverseGraph.vue 拆出）
 *
 * 职责：ForceGraph3D 实例创建与外观配置、Bloom 后处理、星空背景、点击涟漪、
 * 交互事件绑定（hover/click/drag/dblclick）、hover 高亮、相机聚焦与复位、
 * 闲置公转、性能监测自适应降档、尺寸同步。
 *
 * 生命周期红线（与拆分前语义逐条对应）：
 * - WebGL 上下文只经 init() 里的 new ForceGraph3D 创建；init() 可重复调用
 *   （>2000 节点降级后"仍要进入 3D"会重建实例），星空/性能循环经 RAF 句柄
 *   守卫不重复启动（与原实现一致）；
 * - destroyInstance(): 对应降级分支，仅析构 WebGL 实例，刻意保留循环运行，
 *   待重建后由启动守卫复用（行为保真关键点）;
 * - shutdown(): 组件卸载的完整销毁链，顺序：定时器 → 星空/性能 RAF →
 *   dblclick 解绑 → 涟漪 geometry/material → 渲染器 _destructor → 引用置空。
 *
 * 适配说明（installed v1.80）：
 *  - 该版本无 graph.onEngineRender / graph.cameraAutoOrbit。
 *  - Bloom 通过官方 graph.postProcessingComposer()（自动创建 EffectComposer +
 *    RenderPass，引擎每帧自动调用 composer.render()），仅追加 UnrealBloomPass 即可。
 *  - 闲置公转通过 controlType:'orbit' 的 OrbitControls.autoRotate 实现（引擎每帧调用 controls.update）。
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
}

export interface UniverseEngine {
  readonly hasInstance: boolean
  /** 创建 ForceGraph3D 实例并完成全部场景配置（降级后可再次调用以重建） */
  init(): void
  /** ">2000 节点降级"路径专用：仅析构实例，其余循环/定时器保持原状 */
  destroyInstance(): void
  setData(nodes: GraphNode[], links: GraphLink[]): void
  configureForce(bigGraph: boolean): void
  /** 延迟释放初始锚定并挂全量边；getter 保证触发时读到最新累积数据 */
  scheduleRelease(getNodes: () => GraphNode[], getLinks: () => GraphLink[]): void
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
const RELEASE_DELAY_MS = 300
const ZOOMTOFIT_DELAY_MS = 1500
const FLASH_DURATION_MS = 1000

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

  const ripples = createRippleManager()
  const perf = createPerfMonitor({
    fps: ctx.fps,
    qualityTier: ctx.qualityTier,
    isAlive: () => alive,
    onTierApplied: applyQualityTier,
  })

  function getOrbitControls(): OrbitLikeControls | null {
    if (!instance) return null
    const c = instance.controls() as unknown
    return (c && typeof c === 'object') ? (c as OrbitLikeControls) : null
  }

  function applyQualityTier(): void {
    const g = instance
    if (!g) return
    // 边数过载（>600）时强制 medium 起步：Bloom 关、粒子 1
    if (ctx.heavyEdges.value && ctx.qualityTier.value === 'high') {
      ctx.qualityTier.value = 'medium'
    }
    const tier = ctx.qualityTier.value
    // Bloom 仅在 high 档启用
    if (bloomPass) bloomPass.enabled = (tier === 'high')
    // 链路粒子：low 档关闭，medium 档 1 个，high 档 2 个
    g.linkDirectionalParticles(tier === 'low' ? 0 : tier === 'medium' ? 1 : 2)
    // 节点分辨率：low 档 6 段，其余 8 段（原 20 段过高）
    g.nodeResolution(tier === 'low' ? 6 : 8)
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
      return
    }
    const id = focus.id as string
    const neighbors = neighborsCache.get(id) || new Set<string>()
    g.nodeColor((node: NodeObject) => {
      const nid = node.id as string
      if (nid === id) return '#ffffff'
      if (neighbors.has(nid)) return colorForKind((node as GraphNode).kind)
      return ctx.expandedIds.value.has(nid) ? COLOR_EXPANDED : COLOR_NODE_DIM
    })
    g.linkColor((link: any) => {
      const s = linkId(link.source)
      const t = linkId(link.target)
      return s === id || t === id ? COLOR_WISDOM : COLOR_LINK_DIM
    })
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
      { x: tx, y: ty, z: tz + 80 },
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
    const loop = () => {
      if (!alive) return
      // 窗口隐藏/最小化：浏览器通常自动暂停 rAF，这里显式兜底（保持原实现语义）
      if (document.hidden) return
      spinStarLayers(starLayers)
      ripples.update(instance ? instance.scene() : null)
      starRAF = requestAnimationFrame(loop)
    }
    starRAF = requestAnimationFrame(loop)
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
      .nodeColor((node: NodeObject) => ctx.expandedIds.value.has(node.id as string)
        ? COLOR_EXPANDED : colorForKind((node as GraphNode).kind))
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
    starScene = g.scene()
    starLayers = createStarLayers(starScene)

    // Bloom 后处理（该版本 postProcessingComposer 自动含 RenderPass，引擎每帧自动 render）
    // 参数顺序: (resolution, strength, radius, threshold)
    // 降强度 + 提阈值，避免过曝刺眼，让节点本身的颜色更清晰
    if (ctx.enableBloom) {
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
      .onNodeDrag(() => resetIdleTimer())
      .onNodeDragEnd(() => resetIdleTimer())

    // 双击空白处复位全局视角（全屏模式快速回到总览）
    el.addEventListener('dblclick', onDblClickReset)
  }

  function destroyInstance(): void {
    if (!instance) return
    instance._destructor()
    instance = null
  }

  function setData(nodes: GraphNode[], links: GraphLink[]): void {
    if (!instance) return
    instance.graphData({
      nodes: nodes as unknown as NodeObject[],
      links: links as unknown as GraphLink[],
    } as any)
  }

  // 力导向参数：增大斥力和连接长度，避免节点挤成一坨
  function configureForce(bigGraph: boolean): void {
    if (!instance) return
    const charge = instance.d3Force('charge')
    if (charge) charge.strength(bigGraph ? -200 : -120)
    const link = instance.d3Force('link')
    if (link) link.distance(60)
  }

  // 释放初始锚定，让力导向把节点从中心炸开（getter 在触发时才读数，与拆分前一致）
  function scheduleRelease(getNodes: () => GraphNode[], getLinks: () => GraphLink[]): void {
    releaseTimer = setTimeout(() => {
      if (!alive || !instance) return
      getNodes().forEach(n => { n.fx = undefined; n.fy = undefined; n.fz = undefined })
      instance.d3AlphaDecay(0.05)
      instance.graphData({
        nodes: getNodes() as unknown as NodeObject[],
        links: getLinks() as unknown as GraphLink[],
      } as any)
    }, RELEASE_DELAY_MS)
  }

  // 收敛后框选居中
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
    if (bloomPass) bloomPass.setSize(w, h)
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
  }

  return {
    get hasInstance() { return !!instance },
    init,
    destroyInstance,
    setData,
    configureForce,
    scheduleRelease,
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
