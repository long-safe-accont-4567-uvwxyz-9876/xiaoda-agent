/**
 * InsightView 拆解后的共享展示类型（2026-08-23 大文件拆分专项）。
 *
 * 只描述视图层实际消费的字段（props/emits 显式类型化）；
 * API 层类型在 src/api/types.ts，此处允许按后端真实返回做视图侧收窄/扩展。
 */
import type { KnowledgeEntityRow, LearningRow, NoteRow } from '../../api'

export type CrudType = 'memory' | 'note' | 'learning' | 'instinct' | 'entity' | 'relation'

// ── 情绪 ──
export interface EmotionCurrent {
  primary?: string
}

export interface EmotionHistoryRow {
  hour: string
  emotion_label: string
  cnt: number
}

// ── 画像 ──
export interface PortraitHistoryEntry {
  version: number
  change_log?: string
  created_at?: number
}

/** GET /insight/portrait 的 portrait 对象（视图消费 version/created_at/content） */
export interface PortraitData {
  version?: number
  created_at?: number
  content?: string
  [key: string]: unknown
}

// ── 今日事件 ──
export interface TodayStats {
  conversations?: number
  tool_calls?: number
  memories?: number
}

export interface TodayItem {
  ts: number
  kind: string
  text?: string
  event_type?: string
}

// ── 记忆 ──
export interface MemoryItem {
  id: number
  summary: string
  importance?: number
  emotion_label?: string
  timestamp: number
  via?: string
}

// ── 笔记 / 学习 / 本能 ──
export type NoteItem = NoteRow

/** 后端在学习记录上附带出现次数统计（原 any 视图直接消费） */
export type LearningItem = LearningRow & { recurrence_count?: number }

/** 原模板存在 ins.summary 兜底分支 */
export type InstinctItem = import('../../api').InstinctRow & { summary?: string }

// ── 知识图谱 ──
export type EntityItem = KnowledgeEntityRow

export interface RelationItem {
  id: string
  from_entity: string
  to_entity: string
  relation_type: string
}

// ── 2D 力导图渲染形态（useKnowledgeGraphData makeNode/makeEdge 输出）──
export interface KgNode2D {
  name: string
  value?: unknown
  kind?: unknown
}

export interface KgEdge2D {
  from: string
  to: string
  relation?: unknown
}
