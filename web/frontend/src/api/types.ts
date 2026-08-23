/**
 * API 响应类型定义（2026-08-23 技术债：api 层类型化）。
 *
 * 目标：止住 `get<any>` 向视图层传染 any——已知字段在此建档，
 * 行级实体带 `[key: string]: any` 索引签名保证存量视图零破坏；
 * 后续按域收紧时删除对应索引签名即可（vue-tsc 会指出所有依赖点）。
 * 字段来源 = 后端路由/模型定义（web/schemas.py、db/schema.sql、
 * workflow_v2/repository.py 等），改动后端响应结构时同步此处。
 */

// ── 系统 ──
export interface SystemStatus {
  uptime: number
  qq_connected: boolean
  wechat_connected: boolean
  active_sessions: number
  version: string
  permission_mode: string
  [key: string]: any
}

// ── 会话 / 消息 ──
export interface SessionInfo {
  session_id: string
  summary: string
  last_modified: number
  custom_title?: string | null
  first_prompt?: string | null
  tag?: string | null
  created_at?: number | null
  [key: string]: any
}

export interface ChatMessageItem {
  id: number
  role: 'user' | 'assistant' | (string & {})
  content: string
  emotion?: string | null
  timestamp: number
  tool_calls?: unknown[] | null
  request_context?: Record<string, unknown> | null
  [key: string]: any
}

// ── Agent（原 stores/agents.ts 定义，收口至此；store 转发导出保持兼容）──
export interface AgentInfo {
  name: string
  display_name: string
  display_name_en?: string
  builtin: boolean
  is_main: boolean
  enabled: boolean
  provider: string
  model: string
  tool_count: number
  mcp_servers: string[]
  wallpaper?: string
  voice_ref?: string | null
  route_description?: string
  [key: string]: any
}

export interface AgentPermissions {
  name: string
  permissions: Record<string, unknown>
  [key: string]: any
}

// ── Setup 向导 ──
export interface SetupKeyInfo {
  key_name?: string
  configured?: boolean
  source?: string
  [key: string]: any
}

// ── 工作流 V2 运行时 ──
export type WorkflowRunStatus =
  | 'queued' | 'running' | 'waiting_input'
  | 'succeeded' | 'failed' | 'cancelled'

export interface WorkflowRun {
  run_id: string
  workflow_id: string
  revision_id: string
  status: WorkflowRunStatus | (string & {})
  lock_version: number
  parent_run_id?: string | null
  idempotency_key?: string | null
  input: Record<string, unknown>
  output: Record<string, unknown>
  cancel_requested_at?: number | null
  created_at: number
  updated_at: number
  [key: string]: any
}

export interface WorkflowStepRun {
  run_id: string
  node_id: string
  attempt: number
  status: string
  error_code?: string | null
  error_message?: string | null
  lease_owner?: string | null
  lease_expires_at?: number | null
  [key: string]: any
}

/** GET /workflow-runs/{id} —— service.snapshot() */
export interface WorkflowRunSnapshot {
  run: WorkflowRun
  steps: WorkflowStepRun[]
  last_seq: number
  [key: string]: any
}

export interface WorkflowRevisionInfo {
  revision_id: string
  content_hash: string
  created_at: number
  /** service.list_revisions 富化字段 */
  current?: boolean
  /** 定义级 etag（回滚 PUT If-Match 用） */
  etag?: string
  [key: string]: any
}

/** wf_review 表行（GET /workflow-runs/{id}/reviews） */
export interface WorkflowReview {
  review_id: string
  run_id: string
  node_id: string
  attempt: number
  title: string
  note: string
  status: 'pending' | 'approved' | 'rejected' | (string & {})
  decided_by?: string | null
  decision_note?: string
  created_at?: number
  decided_at?: number | null
  [key: string]: any
}

// ── Insight（记忆/笔记/学习/本能/知识图谱）──
export interface NoteRow {
  id: number
  content: string
  kind?: string
  tags?: string
  status?: string
  importance?: number
  created_at?: number
  [key: string]: any
}

export interface LearningRow {
  id: number
  summary: string
  pattern?: string
  priority?: string
  last_seen?: number
  [key: string]: any
}

export interface InstinctRow {
  id: number
  content: string
  trigger_pattern?: string
  confidence?: number
  [key: string]: any
}

export interface KnowledgeEntityRow {
  name: string
  kind?: string
  observations?: string
  [key: string]: any
}

export interface KnowledgeGraphData {
  nodes: Array<{ name: string; kind?: string; [key: string]: any }>
  edges: Array<{ from?: string; to?: string; relation?: string; [key: string]: any }>
}

// ── J-Space ──
export interface JSpaceInterventions {
  rules: Array<Record<string, any>>
  convergence: Record<string, any> | null
  history: Array<Record<string, any>>
}
