/**
 * API 响应类型定义（2026-08-23 技术债：api 层类型化）。
 *
 * 目标：止住 `get<any>` 向视图层传染 any——已知字段在此建档，
 * 不设 `[key: string]: any` 兜底索引签名；动态键场景用
 * Record<string, 具体值类型> 精确表达。
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
}

export interface ChatMessageItem {
  id: number
  role: 'user' | 'assistant' | (string & {})
  content: string
  emotion?: string | null
  timestamp: number
  tool_calls?: unknown[] | null
  request_context?: Record<string, unknown> | null
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
  /** 视频壁纸的 ffmpeg 首帧海报（{stem}_poster.jpg），仅真实存在时返回 */
  wallpaper_poster?: string
  voice_ref?: string | null
  route_description?: string
  /** 子代理缺 API Key 等降级标志（agent_registry 序列化字段） */
  degraded?: boolean
}

/** 权限矩阵单条目（web/agent_registry.py::get_permissions） */
export interface AgentPermissionEntry {
  enabled: boolean
  locked: boolean
  reason?: string
}

/** GET/PUT /agents/{name}/permissions 响应体 */
export interface AgentPermissions {
  tools: Record<string, AgentPermissionEntry>
  mcp_servers: Record<string, AgentPermissionEntry>
  is_main: boolean
}

// ── 运行时配置（GET /system/config，web/config_service.py 合并后的 webui_overrides）──
export interface SystemConfig {
  ui?: {
    particles?: string
    tilt3d?: boolean
    sound_fx?: boolean
    sound_volume?: number
    dendro_cursor?: boolean
    dendro_cursor_trail?: boolean
    main_wallpaper?: string
  }
  tts?: { auto_speak?: boolean; default_voice?: string }
  dashboard?: { system_monitor_enabled?: boolean }
  context?: { shared_platforms?: string[]; shared_key?: string }
}

/** GET /insight/today */
export interface TodaySummary {
  items: Array<{
    ts?: number
    text?: string
    kind: 'memory' | 'event' | 'note' | 'greeting' | (string & {})
    note_kind?: string
    event_type?: string
    reason?: string
  }>
  stats: { conversations: number; tool_calls: number; memories: number }
}

/** GET /models/usage —— api_usage 按日聚合行 */
export interface UsageSeriesRow {
  day: string | null
  model?: string
  prompt_tokens?: number
  completion_tokens?: number
  cost_usd?: number | null
  calls: number
}

export interface UsageSummary {
  days: number
  series: UsageSeriesRow[]
  total: { cost?: number | null; tokens?: number | null; calls?: number }
}

/** GET /models/routes 单条路由配置 */
export interface ModelRouteInfo {
  model: string
  provider: string
  max_tokens: number
  thinking: boolean
  timeout?: number | null
}

/** GET /models/credentials/status 单条凭证 */
export interface CredentialStatus {
  provider: string
  index: number
  key_masked: string
  state: string
  last_error?: string | null
  use_count: number
  error_count: number
  last_used_at?: number | null
}

/** audit_logs 表行（GET /system/audit） */
export interface AuditLogRow {
  id: number
  timestamp: number
  event_type: string
  user_id?: string
  detail?: string
}

/** GET /system/permission-mode */
export interface PermissionModeInfo {
  mode: string
  options: string[]
}

/** GET /system/metrics（utils/metrics.get_snapshot） */
export interface MetricsSnapshot {
  timestamp: number
  counters: Record<string, number>
  gauges: Record<string, number>
}

/** 探针结果（web/probes.py probe_* / run_probe 返回的 dict） */
export interface HealthProbeResult {
  ok: boolean
  latency_ms?: number
  error?: string
  reply_excerpt?: string
  note?: string
  audio_url?: string | null
}

/** health_reports 表行（GET /health/report） */
export interface HealthReportRow {
  run_at: number
  passed: number
  total: number
  detail?: unknown
}

// ── Setup 向导 ──
/** GET /setup/keys 单条（_build_key_list 序列化；raw_value 后端已不返回，留作旧视图兜底） */
export interface SetupKeyInfo {
  key: string
  label?: string
  desc?: string
  url?: string
  url_desc?: string
  required?: boolean
  configured?: boolean
  masked_value?: string
  raw_value?: string
  key_name?: string
  source?: string
}

// ── 工作流 V1 定义（编辑器）──
export type WorkflowNodeType = 'tool' | 'skill' | 'mcp' | 'agent' | 'model' | 'step'

export interface WorkflowNode {
  id: string
  type: WorkflowNodeType
  ref?: string
  label: string
  params?: Record<string, unknown>
  note?: string
  expect?: string
}

export interface Workflow {
  id: string
  name: string
  description: string
  version: string
  enabled: boolean
  nodes: WorkflowNode[]
  edges: [string, string][]
  trigger: string
}

export interface WorkflowSummary {
  id: string
  name: string
  description: string
  enabled: boolean
  node_count: number
  version: string
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
}

/** GET /workflow-runs/{id} —— service.snapshot() */
export interface WorkflowRunSnapshot {
  run: WorkflowRun
  steps: WorkflowStepRun[]
  last_seq: number
}

export interface WorkflowRevisionInfo {
  revision_id: string
  content_hash: string
  created_at: number
  /** service.list_revisions 富化字段 */
  current?: boolean
  /** 定义级 etag（回滚 PUT If-Match 用） */
  etag?: string
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
}

export interface LearningRow {
  id: number
  summary: string
  pattern?: string
  priority?: string
  last_seen?: number
}

export interface InstinctRow {
  id: number
  content: string
  trigger_pattern?: string
  confidence?: number
}

export interface KnowledgeEntityRow {
  name: string
  kind?: string
  observations?: string
}

export interface KnowledgeGraphData {
  nodes: Array<{ name: string; kind?: string }>
  edges: Array<{ from?: string; to?: string; relation?: string }>
}

/** GET /insight/knowledge/relations 行（knowledge_relations 表全列，SELECT * 直出） */
export interface KnowledgeRelationRow {
  id: string
  from_entity: string
  relation_type: string
  to_entity: string
  created_at?: number
  updated_at?: number
}

// ── XP 亲密度 ──
export interface XpHistoryEntry {
  timestamp: string
  amount: number
  source: string
  description: string
}

export interface XpLevelConfig {
  level: number
  threshold: number
  label: string
  tone: string
  proactivity: number
  emotional_richness: number
  guidance: string
}

export interface XpState {
  user_id: string
  xp: number
  level: number
  level_label: string
  next_level_xp: number
  progress: number
  history: XpHistoryEntry[]
  milestones: Record<string, string>
  first_seen_at: string
  last_chat_at: string
  level_config: XpLevelConfig
}

// ── J-Space ──
/** 干预规则行（core/intervention_loop.py::list_rules 序列化） */
export interface JSpaceRule {
  signal_type: string
  threshold: number
  direction_name: string
  alpha: number
  mode: string
  trigger_above: boolean
}

/** 收敛指标（core/intervention_loop.py::get_convergence_metrics） */
export interface JSpaceConvergence {
  converging: boolean
  trend?: number
  intervention_count?: number
}

export interface JSpaceInterventions {
  rules: JSpaceRule[]
  convergence: JSpaceConvergence | null
  history: Array<Record<string, unknown>>
}

export interface JSpaceStatus {
  enabled: boolean
  signal_stream: { active: boolean; buffer_size: number }
  direction_registry: { active: boolean; directions: string[] }
  intervention_loop: { active: boolean; rules_count: number }
  structured_blackboard: { active: boolean }
  enhanced_router: { active: boolean }
  intent_decomposer: { active: boolean; use_llm: boolean }
}

export interface JSpaceSignalEntry {
  signal_type: string
  value: number
  source: string
  timestamp: number
  meta: Record<string, unknown>
}

export interface JSpaceDirection {
  dimensions: Record<string, number>
  source: string
  magnitude: number
}

export interface JSpaceConfig {
  enabled: boolean
  signal_max_history: number
  intent_use_llm: boolean
  intent_llm_timeout: number
}

export interface IntentFactorResult {
  name: string
  activation: number
  evidence: string
  confidence: number
}
