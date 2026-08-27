/**
 * 记忆球落梢分配（自 engine.ts 抽出，2026-08-25 巨型文件门禁专项）
 *
 * 职责：图关系 → 世界树枝尖的分配算法 —— 根记忆球（检索实体优先）坐最居中
 * 的枝尖；同一父节点的孩子们按扇区轮转分杈（像真树各奔一方），扇区内取距
 * 父球最近的空梢，空间哈希格网防挤坨（O(N²)→近似 O(N)）；梢容量耗尽时驱动
 * 世界树 extendBranch 真实生长新梢。
 *
 * 与引擎的边界：本模块持有落梢私有状态（childSeq/occGrid/anchorSectors/
 * anchorById/usedAnchors），经 deps 注入世界树与根提示；动画状态
 * （knownIds/spawnTimes/springState 等）仍归引擎。
 */
import * as THREE from 'three'
import type { GraphNode } from './types'
import type { WorldTree } from './worldTree'

export interface MemoryPlacementDeps {
  /** 当前世界树实例（可能随重建变化，故为 getter） */
  getTree: () => WorldTree | null
  /** 根节点提示 id（检索实体优先坐主枝） */
  getRootHintId: () => string
  /** 兜底螺旋游标：孤儿球收养到树冠外围时的位置游标（所有权在引擎，跨区共享） */
  bumpCanopyCursor: () => number
  /** 邻接索引（引擎维护，落梢时查父球是否已就位） */
  getNeighborsCache: () => Map<string, Set<string>>
}

const SECTOR_COUNT = 12
const TIP_MIN_SEP = 34

function hashStr(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

export interface MemoryPlacement {
  assignAnchors(fresh: GraphNode[]): void
  reset(): void
  /** 枝尖下标是否已被记忆球占用（空枝尖=芽点，可点击新建） */
  isAnchorUsed(i: number): boolean
}

export function createMemoryPlacement(deps: MemoryPlacementDeps): MemoryPlacement {
  let anchorSectors: number[] | null = null
  const childSeq = new Map<string, number>()               // 父节点已用枝槽序号（扇区轮转）
  const anchorById = new Map<string, THREE.Vector3>()      // id → 枝尖锚点
  const usedAnchors = new Set<number>()                    // 已占用枝尖下标

  // 空间哈希格网加速挤坨检测：格边 = 最小间距，只需检查 27 个邻格
  const occGrid = new Map<string, THREE.Vector3[]>()

  function ensureAnchorSectors(): void {
    const tree = deps.getTree()
    if (!tree) return
    const tau = Math.PI * 2
    anchorSectors ??= []
    // extendBranch 会向 anchors 追加自动生长的新梢 —— 这里增量补齐扇区值
    while (anchorSectors.length < tree.anchors.length) {
      const a = tree.anchors[anchorSectors.length]
      let az = Math.atan2(a.z, a.x)
      if (az < 0) az += tau
      anchorSectors.push(Math.min(SECTOR_COUNT - 1, Math.floor((az / tau) * SECTOR_COUNT)))
    }
  }

  function gridKey(x: number, y: number, z: number): string {
    return `${Math.floor(x / TIP_MIN_SEP)},${Math.floor(y / TIP_MIN_SEP)},${Math.floor(z / TIP_MIN_SEP)}`
  }
  function occupyTip(v: THREE.Vector3): void {
    const k = gridKey(v.x, v.y, v.z)
    const cell = occGrid.get(k)
    if (cell) cell.push(v)
    else occGrid.set(k, [v])
  }
  function tooCrowded(v: THREE.Vector3): boolean {
    const cx = Math.floor(v.x / TIP_MIN_SEP)
    const cy = Math.floor(v.y / TIP_MIN_SEP)
    const cz = Math.floor(v.z / TIP_MIN_SEP)
    const min2 = TIP_MIN_SEP * TIP_MIN_SEP
    for (let ix = cx - 1; ix <= cx + 1; ix++) {
      for (let iy = cy - 1; iy <= cy + 1; iy++) {
        for (let iz = cz - 1; iz <= cz + 1; iz++) {
          const cell = occGrid.get(`${ix},${iy},${iz}`)
          if (!cell) continue
          for (const p of cell) {
            const dx = p.x - v.x; const dy = p.y - v.y; const dz = p.z - v.z
            if (dx * dx + dy * dy + dz * dz < min2) return true
          }
        }
      }
    }
    return false
  }

  /**
   * 为父球 parentId 的第 seq 个孩子挑枝尖：
   * 四档降级（同扇区+间距+向外 → 同扇区+间距 → 全局带间距 → 全局最近 → 冠外螺旋）
   */
  function pickTip(parentId: string, parentAnchor: THREE.Vector3, seq: number): THREE.Vector3 {
    const tree = deps.getTree()
    ensureAnchorSectors()
    const list = tree!.anchors
    const sectors = anchorSectors!
    const pref = (hashStr(parentId) + seq * 5) % SECTOR_COUNT
    const refR = Math.hypot(parentAnchor.x, parentAnchor.z)
    const passes: Array<(i: number) => boolean> = [
      i => sectors[i] === pref && Math.hypot(list[i].x, list[i].z) >= refR * 0.7 && !tooCrowded(list[i]),
      i => sectors[i] === pref && !tooCrowded(list[i]),
      i => !tooCrowded(list[i]),
      () => true,
    ]
    for (const ok of passes) {
      let best = -1
      let bestD = Infinity
      for (let i = 0; i < list.length; i++) {
        if (usedAnchors.has(i) || !ok(i)) continue
        const d = list[i].distanceToSquared(parentAnchor)
        if (d < bestD) { bestD = d; best = i }
      }
      if (best >= 0) {
        usedAnchors.add(best)
        return list[best]
      }
    }
    // 梢容量耗尽 → 世界树自动生长：从父球所在枝尖延伸一根真实新梢，
    // 新梢末端就是这颗记忆球的家 —— 树随记忆一起长大，而不是让球漂出树外
    const grown = tree!.extendBranch(parentAnchor)
    usedAnchors.add(tree!.anchors.length - 1)
    return grown
  }

  function firstPlacedNeighbor(id: string): string | null {
    const nbs = deps.getNeighborsCache().get(id)
    if (!nbs) return null
    for (const nb of nbs) if (anchorById.has(nb)) return nb
    return null
  }

  function setAnchor(n: GraphNode, a: THREE.Vector3): void {
    n.__anchor = a.clone()
    n.x = a.x + (Math.random() - 0.5) * 2
    n.y = a.y + (Math.random() - 0.5) * 2
    n.z = a.z + (Math.random() - 0.5) * 2
    anchorById.set(n.id as string, n.__anchor)
    occupyTip(n.__anchor)
  }

  function assignAnchors(fresh: GraphNode[]): void {
    const tree = deps.getTree()
    if (!tree || !fresh.length) return

    // 根记忆球：最居中的枝尖（anchors 已排序，取首个）
    if (anchorById.size === 0) {
      const hint = deps.getRootHintId()
      const rootN = fresh.find(n => n.id === hint)
        || [...fresh].sort((a, b) => (b.val ?? 1) - (a.val ?? 1))[0]
      usedAnchors.add(0)
      setAnchor(rootN, tree.anchors[0])
    }

    // 反复扫描待落梢队列：父球就位的立即落梢，直到无进展
    const pending = fresh.filter(n => !anchorById.has(n.id as string)).map(n => n.id as string)
    let progress = true
    while (progress && pending.length) {
      progress = false
      for (let i = pending.length - 1; i >= 0; i--) {
        const id = pending[i]
        if (anchorById.has(id)) { pending.splice(i, 1); continue }
        const pid = firstPlacedNeighbor(id)
        if (pid != null) {
          const n = fresh.find(x => x.id === id)!
          const seq = childSeq.get(pid) ?? 0
          childSeq.set(pid, seq + 1)
          setAnchor(n, pickTip(pid, anchorById.get(pid)!, seq))
          pending.splice(i, 1)
          progress = true
        }
      }
    }
    // 孤儿（断链组件）：收养到树冠外围兜底位
    for (const id of pending) {
      const n = fresh.find(x => x.id === id)!
      setAnchor(n, tree.canopyAnchor(deps.bumpCanopyCursor()))
    }
  }

  function reset(): void {
    anchorById.clear()
    usedAnchors.clear()
    childSeq.clear()
    anchorSectors = null
    occGrid.clear()
  }

  return { assignAnchors, reset, isAnchorUsed: (i: number) => usedAnchors.has(i) }
}
