/**
 * 知识图谱累积器 —— 2D（InsightView/echarts）与 3D（UniverseGraph/3d-force-graph）
 * 共享的数据层组合式函数（2026-08-22 三可视化库合并专项第一阶段）。
 *
 * 统一语义：
 * - 节点按 name 去重；边按 `${from}||${relation}||${to}` 去重
 * - merge 时基于本批边计算 degree，供渲染整形使用（3D 的 val、2D 忽略）
 * - expandedIds 追踪已展开邻域的节点（点击高亮）
 * - 渲染形态经 makeNode/makeEdge 钩子注入：两侧节点字段不同
 *   （3D: id/val/fx/fy/fz；2D: value/kind），算法只此一份
 *
 * 渲染层仍各自独立（echarts 力导 vs three WebGL），合并渲染引擎不在本阶段范围。
 */
import { ref, type Ref } from 'vue'

export const kgEdgeKey = (e: { from?: unknown; source?: unknown; relation: unknown; to?: unknown; target?: unknown }) =>
  `${String(e.from ?? e.source)}||${String(e.relation)}||${String(e.to ?? e.target)}`

export interface KnowledgeGraphAccumulator<TNode, TEdge> {
  /** 累积节点（去重后，渲染形态） */
  nodes: Ref<TNode[]>
  /** 累积边（去重后，渲染形态） */
  edges: Ref<TEdge[]>
  /** 已展开邻域的节点 id 集合 */
  expandedIds: Ref<Set<string>>
  /** 清空全部累积状态（搜索/深度变化时调用） */
  reset(): void
  /** 去重合并一批原始 API 数据 */
  merge(rawNodes: any[], rawEdges: any[]): void
  markExpanded(id: string): void
}

export function useKnowledgeGraphData<TNode, TEdge>(
  makeNode: (raw: any, degree: number) => TNode,
  makeEdge: (raw: any) => TEdge,
): KnowledgeGraphAccumulator<TNode, TEdge> {
  const nodes = ref<TNode[]>([]) as Ref<TNode[]>
  const edges = ref<TEdge[]>([]) as Ref<TEdge[]>
  const expandedIds = ref<Set<string>>(new Set())
  let nodeIdx = new Map<string, TNode>()
  let edgeKeys = new Set<string>()

  function reset() {
    nodes.value = []
    edges.value = []
    expandedIds.value = new Set()
    nodeIdx = new Map()
    edgeKeys = new Set()
  }

  function merge(rawNodes: any[], rawEdges: any[]) {
    const degree = new Map<string, number>()
    for (const e of rawEdges) {
      const f = String(e.from), t = String(e.to)
      degree.set(f, (degree.get(f) || 0) + 1)
      degree.set(t, (degree.get(t) || 0) + 1)
    }
    for (const n of rawNodes) {
      const name = String(n.name)
      if (nodeIdx.has(name)) continue
      const node = makeNode(n, degree.get(name) || 0)
      nodeIdx.set(name, node)
      nodes.value.push(node)
    }
    for (const e of rawEdges) {
      const k = kgEdgeKey(e)
      if (edgeKeys.has(k)) continue
      edgeKeys.add(k)
      edges.value.push(makeEdge(e))
    }
  }

  function markExpanded(id: string) {
    expandedIds.value.add(id)
  }

  return { nodes, edges, expandedIds, reset, merge, markExpanded }
}
