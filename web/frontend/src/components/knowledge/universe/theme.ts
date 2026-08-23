/**
 * 纳西妲宇宙 —— 配色常量与实体类别映射（从 UniverseGraph.vue 拆出）
 */
import { t } from '../../../i18n'

// ── 配色 ──
export const COLOR_DENDRO = '#8fe560'
export const COLOR_WISDOM = '#e8d5a3'
export const COLOR_MOON = '#f2f7ee'
export const COLOR_ALERT = '#d96a5f'
export const COLOR_DIM = 'rgba(143,229,96,0.6)'
export const COLOR_LINK = 'rgba(143,229,96,0.3)'
export const COLOR_LINK_DIM = 'rgba(143,229,96,0.1)'
export const COLOR_NODE_DIM = 'rgba(143,229,96,0.15)'
export const COLOR_EXPANDED = '#fbbf24'  // 已展开邻域的节点：亮金色提示
export const BG_DEEP = '#0f1f17'

export function colorForKind(kind?: string): string {
  if (!kind) return COLOR_DIM
  const k = kind.toLowerCase()
  if (k === 'person' || kind === '人物') return COLOR_DENDRO
  if (k === 'place' || k === 'location' || kind === '地点') return COLOR_WISDOM
  if (k === 'concept' || kind === '概念') return COLOR_MOON
  if (k === 'event' || kind === '事件') return COLOR_ALERT
  return COLOR_DIM
}

export function kindLabel(kind?: string): string {
  if (!kind) return t('universeGraph.kindEntity')
  const map: Record<string, string> = {
    person: t('universeGraph.kindPerson'), '人物': t('universeGraph.kindPerson'),
    place: t('universeGraph.kindPlace'), location: t('universeGraph.kindPlace'), '地点': t('universeGraph.kindPlace'),
    concept: t('universeGraph.kindConcept'), '概念': t('universeGraph.kindConcept'),
    event: t('universeGraph.kindEvent'), '事件': t('universeGraph.kindEvent'),
  }
  return map[kind.toLowerCase()] || map[kind] || kind
}
