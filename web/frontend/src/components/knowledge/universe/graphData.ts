/**
 * 纳西妲宇宙 —— 数据→图结构的纯函数转换（从 UniverseGraph.vue 拆出）
 *
 * 无副作用、不依赖组件状态，便于后续补最小单测。
 */
import type { GraphLink, GraphNode } from './types'

export function linkId(end: string | GraphNode): string {
  return typeof end === 'string' ? end : (end.id as string)
}

// ── 邻居索引（hover 高亮用）：节点 id -> 邻居 id 集合 ──
export function buildNeighbors(ns: GraphNode[], ls: GraphLink[]): Map<string, Set<string>> {
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

export interface NodeRelation {
  relation: string
  other: string
  /** 关系主键（删/改连接凭此定位） */
  id?: string
}

/** 选中节点的关系列表（详情面板，最多 limit 条） */
export function findRelations(
  links: GraphLink[],
  nodeId: string,
  limit = 10,
  fallbackRelation = '',
): NodeRelation[] {
  return links
    .filter(l => linkId(l.source) === nodeId || linkId(l.target) === nodeId)
    .slice(0, limit)
    .map(l => ({
      relation: l.relation || fallbackRelation,
      other: linkId(l.source) === nodeId ? linkId(l.target) : linkId(l.source),
      id: l.id,
    }))
}

/** 节点标签 tooltip 的 HTML 转义 */
export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c] as string))
}
