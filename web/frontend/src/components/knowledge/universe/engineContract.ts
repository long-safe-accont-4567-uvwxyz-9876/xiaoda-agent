/** universe engine contract: context/types, capability probe, tuning constants */
import type { Ref } from 'vue'
import type { GraphLink, GraphNode, QualityTier } from './types'
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
  /** 弹窗隐藏：停 rAF + 暂停渲染循环，保留场景与 GPU 资源（P0-3） */
  suspend(): void
  /** 弹窗重新可见：恢复渲染循环（suspend 的逆操作） */
  resume(): void
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

export const IDLE_AUTOROTATE_DELAY_MS = 5000
export const IDLE_AUTOROTATE_SPEED = 0.5
export const DEFER_EDGES_DELAY_MS = 300
export const DEFER_EDGES_NODE_THRESHOLD = 400
export const ZOOMTOFIT_DELAY_MS = 1500
export const FLASH_DURATION_MS = 1000
export const GROWTH_DURATION_MS = 900     // 新节点破土生长动画时长
export const RECENT_SPAWN_MS = 2600       // 新生记忆的"实体色宽限期"（防聚焦调暗成虚影）

// ── 记忆球弹簧（位置所有权在引擎，不在 d3）──
// 每帧把节点拉向"锚点经风摆+树冠倾斜变换后的目标点"，带速度惯性：
// 松手回弹有过冲 → 枝条弹性手感；d3 力模拟全部移除（O(n²) CPU 清零）
export const SPRING_STRENGTH = 0.085
export const SPRING_DAMPING = 0.85
export const TETHER_SHOW_DIST = 10        // 锚点距超过该值显示橡皮筋
export const LEAN_MAX = 0.06              // 树冠最大倾斜（弧度）
export const LEAN_FACTOR = 0.0007         // 拖拽位移 → 倾斜角系数

/** easeOutBack：先回缩再弹出的"抽芽"曲线 */
export function easeOutBack(p: number): number {
  const c1 = 1.70158
  const c3 = c1 + 1
  return 1 + c3 * Math.pow(p - 1, 3) + c1 * Math.pow(p - 1, 2)
}
