import { t } from '../i18n'
import type {
  AgentInfo, AgentPermissions, ChatMessageItem,
  IntentFactorResult, JSpaceConfig, JSpaceDirection, JSpaceInterventions,
  JSpaceSignalEntry, JSpaceStatus,
  KnowledgeGraphData, KnowledgeEntityRow, KnowledgeRelationRow,
  NoteRow, SessionInfo, SetupKeyInfo, SystemConfig, SystemStatus,
  Workflow, WorkflowNode, WorkflowReview, WorkflowRevisionInfo,
  WorkflowRun, WorkflowRunSnapshot, WorkflowSummary,
  XpLevelConfig, XpState,
} from './types'

// 类型统一在 types.ts 建档；此处转发导出保持既有 `from '../api'` 导入兼容
export type {
  AgentInfo, AgentPermissions, ChatMessageItem, IntentFactorResult,
  JSpaceConfig, JSpaceDirection, JSpaceInterventions, JSpaceSignalEntry,
  JSpaceStatus, KnowledgeGraphData, KnowledgeEntityRow, KnowledgeRelationRow,
  LearningRow, InstinctRow,
  NoteRow, SessionInfo, SetupKeyInfo, SystemConfig, SystemStatus,
  Workflow, WorkflowNode, WorkflowNodeType, WorkflowReview, WorkflowRevisionInfo,
  WorkflowRun, WorkflowRunSnapshot, WorkflowSummary,
  XpHistoryEntry, XpLevelConfig, XpState,
} from './types'

const BASE = '/api/v1'

interface ApiEnvelope<T> {
  ok: boolean
  data: T | null
  error?: { code: string; message: string }
}

/** FastAPI 校验失败时返回的 {detail: ...} 错误体（非标准信封） */
interface ApiDetailError {
  detail?: unknown
}

// 泛型无默认值：调用点必须显式声明响应类型，杜绝 any 隐式扩散
async function request<T>(path: string, options?: RequestInit, confirm = false,
                          extraHeaders?: Record<string, string>): Promise<T> {
  const token = localStorage.getItem('token')
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(confirm ? { 'X-Confirm': 'yes' } : {}),
    ...(extraHeaders || {}),
  }
  const res = await fetch(`${BASE}${path}`, { ...options, headers }).catch(e => {
    // 路由切换时浏览器中止 fetch 产生 AbortError，重新抛出以便调用方静默处理
    if (e?.name === 'AbortError') throw e
    throw new Error(e?.message || 'Network error')
  })
  if (res.status === 401) {
    localStorage.removeItem('token')
    // token 失效/未登录时一律引导到登录页（无密码环境同样需要点击"进入"，
    // 不做静默空密码重登——那样会绕过登录页）。设置页保存场景的 401 已由
    // 后端 profile 端点免认证（_profile_endpoint_access）根治，无需前端兜底。
    if (!location.hash.includes('login')) location.hash = '#/login'
    throw new Error(t('login.tokenExpired'))
  }
  // 滑动续期：后端在响应头返回新 token 时自动替换本地存储
  const newToken = res.headers.get('X-New-Token')
  if (newToken) {
    const newExpiry = res.headers.get('X-New-Token-Expiry')
    localStorage.setItem('token', newToken)
    if (newExpiry) localStorage.setItem('expires_at', newExpiry)
    window.dispatchEvent(new CustomEvent('xiaoda-auth-renewed', {
      detail: { token: newToken, expiresAt: Number(newExpiry) || 0 },
    }))
  }
  let body: ApiEnvelope<T>
  // 204/205 无响应体（如模型删除），跳过 JSON 解析避免 SyntaxError
  if (res.status === 204 || res.status === 205) {
    return undefined as T
  }
  try {
    body = await res.json()
  } catch {
    throw new Error(`HTTP ${res.status}`)
  }
  if (!res.ok || !body.ok) {
    let msg: string | undefined = body?.error?.message
    if (!msg && (body as ApiDetailError)?.detail !== undefined) {
      const d = (body as ApiDetailError).detail
      // 兼容结构化 detail（如 {code, message}）与纯字符串 detail
      if (typeof d === 'string') {
        msg = d
      } else if (d && typeof d === 'object') {
        const rec = d as Record<string, unknown>
        msg = rec.message !== undefined ? String(rec.message)
          : rec.code !== undefined ? String(rec.code)
            : JSON.stringify(d)
      } else {
        msg = String(d)
      }
    }
    if (!msg) msg = `HTTP ${res.status}`
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return body.data as T
}

export const get = <T>(path: string) => request<T>(path)
export const post = <T = void>(path: string, body?: unknown, confirm = false) =>
  request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }, confirm)
export const put = <T = void>(path: string, body?: unknown, confirm = false) =>
  request<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}) }, confirm)
export const patch = <T = void>(path: string, body?: unknown, extraHeaders?: Record<string, string>) =>
  request<T>(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) }, false, extraHeaders)
export const del = <T = void>(path: string, confirm = false) =>
  request<T>(path, { method: 'DELETE' }, confirm)

/** 通用文件上传（FormData POST），统一 token 注入与错误处理 */
async function uploadFile<T>(url: string, formData: FormData, errorMsg = 'Upload failed'): Promise<T> {
  const token = localStorage.getItem('token')
  const res = await fetch(`${BASE}${url}`, {
    method: 'POST',
    headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: formData,
  })
  interface UploadEnvelope {
    ok: boolean
    data?: T | null
    error?: { message?: string }
  }
  const body: UploadEnvelope = await res.json().catch(() => ({ ok: false }) as UploadEnvelope)
  if (!res.ok || !body.ok) throw new Error(body?.error?.message || errorMsg)
  return body.data as T
}

// ── 工作流类型见 types.ts（Workflow/WorkflowNode/WorkflowSummary）──

export const api = {
  login: (password: string) =>
    post<{ token: string; expires_at: number }>('/auth/login', { password }),

  getRecoverQuestion: () =>
    get<{ question: string; has_question: boolean }>('/auth/recover-question'),

  recoverPassword: (answer: string, newPassword: string) =>
    post<{ ok: boolean }>('/auth/recover', { answer, new_password: newPassword }),

  changePassword: (body: {
    old_password: string
    new_password: string
    answer: string
    new_question?: string
    new_answer?: string
  }) => post<{ token: string; expires_at: number }>('/auth/change-password', body),

  getStatus: () => get<SystemStatus>('/system/status'),
  getSessions: () => get<SessionInfo[]>('/sessions'),
  createSession: () => post<{ session_id: string }>('/sessions'),
  deleteSession: (id: string) => del(`/sessions/${id}`, true),
  getMessages: (sessionId: string, before = 0, limit = 50) =>
    get<ChatMessageItem[]>(`/sessions/${sessionId}/messages?before=${before}&limit=${limit}`),
  getCommands: () => get<Array<{ name: string; description: string; owner_only: boolean }>>('/commands'),
  testModelRoute: (route: string) => post<{ ok: boolean; error?: string }>('/health/test/llm', { route }),

  getAgents: () => get<AgentInfo[]>('/agents'),
  getPermissions: (name: string) => get<AgentPermissions>(`/agents/${name}/permissions`),
  setAgentModel: (name: string, provider: string, model_id: string) =>
    post<{ name: string; model: string }>(`/agents/${name}/model`, { provider, model_id }),

  tts: (text: string, voice?: string, style?: string) =>
    post<{ audio_url: string; cached: boolean }>('/media/tts', { text, voice, style }),

  uploadVoiceRef: (agent: string, formData: FormData) =>
    uploadFile<{ voice_ref: string }>(`/media/tts/voices/${agent}`, formData),

  // Setup wizard APIs（首次运行时后端免认证；非首次需 token，统一走 request()
  // 以自动处理 X-New-Token 滑动续期，token 失效时引导重新登录而非裸 401 报错）
  getSetupFirstRun: async (): Promise<{ first_run: boolean; profile_done: boolean }> => {
    interface FirstRunEnvelope { ok: boolean; data?: { first_run?: boolean; profile_done?: boolean }; error?: { message?: string } }
    const res = await fetch(`${BASE}/setup/first-run`)
    const body: FirstRunEnvelope = await res.json().catch(() => ({ ok: false }) as FirstRunEnvelope)
    if (!res.ok || !body.ok) throw new Error(body?.error?.message || `HTTP ${res.status}`)
    return { first_run: body.data?.first_run !== false, profile_done: body.data?.profile_done !== false }
  },

  getSetupKeys: () => get<{ keys: SetupKeyInfo[] }>('/setup/keys'),

  testSetupKey: (keyName: string, keyValue: string, extra?: Record<string, string>) =>
    post<{ success: boolean; message: string }>('/setup/test-key', { key_name: keyName, key_value: keyValue, ...(extra ? { extra } : {}) }),

  saveSetupKeys: (keys: Record<string, string>, testRequired = false, extra: Record<string, unknown> = {}) =>
    post<void>('/setup/keys', { keys, test_required: testRequired, ...extra }),

  getSetupUserProfile: () => get<Record<string, string>>('/setup/user-profile'),

  saveSetupUserProfile: (fields: Record<string, string>) =>
    post<void>('/setup/user-profile', fields),

  // Custom provider (needs auth)
  createProvider: (data: { id: string; label: string; format: string; base_url: string; default_model: string; api_key: string }) =>
    post<void>('/models/providers', data),

  // ── 表情包管理 ──
  listStickers: (agentName: string) =>
    get<{ stickers: Array<{ name: string; description: string; emotion: string; url: string }>; emotions: string[] }>(`/agents/${agentName}/stickers`),

  uploadSticker: async (agentName: string, file: File, description: string, emotion: string) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('description', description)
    formData.append('emotion', emotion)
    return uploadFile<{ name: string; description: string; emotion: string; url: string }>(
      `/agents/${agentName}/stickers`, formData)
  },

  deleteSticker: (agentName: string, filename: string) =>
    del<{ deleted: string }>(`/agents/${agentName}/stickers/${encodeURIComponent(filename)}`, true),

  uploadImage: async (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return uploadFile<{ url: string; name: string }>('/chat/upload-image', formData)
  },

  // P0 新增（Task 1.9）：文档上传 — 与图片上传分离
  // 文档（PDF/DOCX 等）走 document_reader 工具，而非 vision API
  uploadDoc: async (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return uploadFile<{ url: string; name: string; path: string; ext: string }>('/chat/upload-doc', formData)
  },

  speechToText: async (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    return uploadFile<{ text: string }>('/chat/speech-to-text', formData, 'STT failed')
  },

  // ── 资源列表（工作流编辑器用） ──
  getTools: () => get<Array<{ name: string; description: string; category: string; enabled: boolean }>>('/tools'),
  getSkills: () => get<Array<{ name: string; size: number; preview: string }>>('/skills'),
  getMcpServers: () => get<Array<{ name: string; status: string; tool_names: string[] }>>('/mcp/servers'),
  getProviders: () => get<Array<{ id: string; label: string; enabled: boolean; default_model?: string }>>('/models/providers'),
  discoverModels: () => get<Array<{ provider: string; label: string; models: Array<{ id: string; display_name: string; free?: boolean; model_id?: string; name?: string }> }>>('/models/discover'),

  // ── 工作流管理 ──
  listWorkflows: () => get<WorkflowSummary[]>('/workflows'),
  getWorkflow: (id: string) => get<Workflow>('/workflows/' + id),
  createWorkflow: (data: Workflow) => post<Workflow>('/workflows', data),
  updateWorkflow: (id: string, data: Workflow) => put<Workflow>('/workflows/' + id, data),
  deleteWorkflow: (id: string) => del<void>('/workflows/' + id),
  previewWorkflow: (id: string) => get<{prompt: string}>('/workflows/' + id + '/preview'),

  listWorkflowRuns: (wfId: string) => get<WorkflowRun[]>(`/workflows/${wfId}/runs`),
  runWorkflow: (wfId: string, input?: Record<string, unknown>) => post<WorkflowRun>(`/workflows/${wfId}/runs`, { input: input || {} }),
  getWorkflowRun: (runId: string) => get<WorkflowRunSnapshot>(`/workflow-runs/${runId}`),
  listWorkflowRevisions: (wfId: string) => get<WorkflowRevisionInfo[]>(`/workflows/${wfId}/revisions`),
  createWorkflowRevision: (wfId: string) => post<WorkflowRevisionInfo>(`/workflows/${wfId}/revisions`, {}),
  publishWorkflow: (wfId: string) => post<WorkflowRevisionInfo>(`/workflows/${wfId}/publish`),
  rollbackWorkflowRevision: (wfId: string, revisionId: string, etag: string) =>
    patch<{ revision_id: string; current_revision_id: string; etag: string }>(`/workflows/${wfId}/current`, { revision_id: revisionId }, { 'If-Match': etag }),
  cancelWorkflowRun: (runId: string) => post<{ run_id: string; status: string }>(`/workflow-runs/${runId}/cancel`),
  getWorkflowV2Status: (wfId: string) =>
    get<{ enabled: boolean; global_enabled: boolean; whitelisted: boolean }>(`/workflows/${wfId}/v2-status`),

  // ── 工作流 REVIEW 审批（M4 服务端 + M5 前端卡片） ──
  listWorkflowReviews: (runId: string) => get<WorkflowReview[]>(`/workflow-runs/${runId}/reviews`),
  decideWorkflowReview: (runId: string, reviewId: string,
                         decision: 'approve' | 'reject', note?: string) => {
    const body: Record<string, unknown> = { decision }
    if (note) body.note = note
    return post<WorkflowReview>(`/workflow-runs/${runId}/reviews/${reviewId}/decide`, body)
  },

  // 品牌署名与免责协议
  getBrandSignature: () => get<{ signature: string; author: string; version: string }>('/brand/signature'),
  getDisclaimerStatus: () => get<{ agreed: boolean; agreed_at: string; text: string }>('/setup/disclaimer-status'),
  agreeDisclaimer: (agreed: boolean) => post<{ success: boolean }>('/setup/agree-disclaimer', { agreed }),
}

export async function getSetupVersion(): Promise<{ version: string }> {
  return get('/setup/version')
}

/** 通过 POST + Authorization header 安全下载会话导出 */
export async function exportSessionDownload(sessionId: string): Promise<void> {
  const token = localStorage.getItem('token')
  const res = await fetch(`${BASE}/sessions/${sessionId}/export`, {
    method: 'POST',
    headers: {
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body?.error?.message || `导出失败 (HTTP ${res.status})`)
  }
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `session-${sessionId}.json`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

// ── 记忆管理 ──
export const createMemory = (data: { summary: string; importance?: number; emotion_label?: string }) =>
  post<{ id: number }>('/insight/memories', data)

export const updateMemory = (id: number, data: { summary?: string; importance?: number; emotion_label?: string }) =>
  put<{ id: number; updated: boolean }>(`/insight/memories/${id}`, data)

export const deleteMemory = (id: number) =>
  del<{ deleted: number }>(`/insight/memories/${id}`, true)

// ── 笔记管理 ──
export const getNotes = (params?: Record<string, string | number | boolean>) =>
  get<NoteRow[]>('/insight/notebook' + (params ? '?' + new URLSearchParams(
    Object.entries(params).map(([k, v]) => [k, String(v)])
  ).toString() : ''))

export const createNote = (data: { content: string; kind?: string; tags?: string; importance?: number }) =>
  post<{ id: number }>('/insight/notebook', data)

export const updateNote = (noteId: number, data: { content?: string; tags?: string; kind?: string; status?: string; importance?: number }) =>
  put<{ id: number; updated: boolean }>(`/insight/notebook/${noteId}`, data)

export const deleteNote = (noteId: number) =>
  del<{ deleted: number }>(`/insight/notebook/${noteId}`, true)

// ── 学习记录管理 ──
export const createLearning = (data: { summary: string; pattern?: string; priority?: string }) =>
  post<{ id: number }>('/insight/learnings', data)

export const updateLearning = (id: number, data: { summary?: string; pattern?: string; priority?: string }) =>
  put<{ id: number; updated: boolean }>(`/insight/learnings/${id}`, data)

export const deleteLearning = (id: number) =>
  del<{ deleted: number }>(`/insight/learnings/${id}`, true)

// ── 本能管理 ──
export const createInstinct = (data: { content: string; trigger_pattern?: string; confidence?: number }) =>
  post<{ id: number }>('/insight/instincts', data)

export const updateInstinct = (id: number, data: { content?: string; trigger_pattern?: string; confidence?: number }) =>
  put<{ id: number; updated: boolean }>(`/insight/instincts/${id}`, data)

export const deleteInstinct = (id: number) =>
  del<{ deleted: number }>(`/insight/instincts/${id}`, true)

// ── 知识图谱管理 ──
export const createKnowledgeEntity = (data: { name: string; kind?: string; observations?: string }) =>
  post<{ name: string }>('/insight/knowledge/entities', data)

export const updateKnowledgeEntity = (name: string, data: { kind?: string; observations?: string }) =>
  put<{ name: string; updated: boolean }>(`/insight/knowledge/entities/${encodeURIComponent(name)}`, data)

export const deleteKnowledgeEntity = (name: string) =>
  del<{ deleted: string }>(`/insight/knowledge/entities/${encodeURIComponent(name)}`, true)

export const createKnowledgeRelation = (data: { from: string; to: string; relation: string }) =>
  post<{ from: string; to: string; relation: string }>('/insight/knowledge/relations', data)

export const deleteKnowledgeRelation = (id: string) =>
  del<{ deleted: string }>(`/insight/knowledge/relations/${encodeURIComponent(id)}`, true)

export const listKnowledgeEntities = (limit = 200) =>
  get<KnowledgeEntityRow[]>(`/insight/knowledge/entities?limit=${limit}`)

export const listKnowledgeRelations = (limit = 200) =>
  get<KnowledgeRelationRow[]>(`/insight/knowledge/relations?limit=${limit}`)

export async function getXpState(): Promise<XpState> {
  return get<XpState>('/insight/xp')
}

export async function getXpLevels(): Promise<{ levels: XpLevelConfig[] }> {
  return get<{ levels: XpLevelConfig[] }>('/insight/xp/levels')
}

export const updateKnowledgeRelation = (id: string, data: { relation: string }) =>
  put<{ id: string; updated: boolean }>(`/insight/knowledge/relations/${id}`, data)

export const getKnowledgeGraph = (entity = '', depth: number | null | undefined = 1) => {
  // n-input-number 清空时 depth 为 null；后端要求整数，这里归位到默认 6
  const d = Number.isFinite(Number(depth)) && Number(depth) >= 1 ? Math.round(Number(depth)) : 1
  return get<KnowledgeGraphData>(`/insight/knowledge/graph?entity=${encodeURIComponent(entity)}&depth=${d}`)
}

// J-Space 类型见 types.ts

export const jspaceGetStatus = () => get<JSpaceStatus>('/jspace/status')
export const jspaceGetSignals = (signal_type = '', last_n = 50) =>
  get<{ entries: JSpaceSignalEntry[]; total: number }>(`/jspace/signals?signal_type=${encodeURIComponent(signal_type)}&last_n=${last_n}`)
export const jspaceGetSignalAggregate = (signal_type: string, strategy = 'mean_of_means') =>
  get<{ value: number; signal_type: string; strategy: string }>(`/jspace/signals/aggregate?signal_type=${encodeURIComponent(signal_type)}&strategy=${strategy}`)
export const jspaceGetDirections = () => get<{ directions: Record<string, JSpaceDirection> }>('/jspace/directions')
export const jspaceGetInterventions = () =>
  get<JSpaceInterventions>('/jspace/interventions')
export const jspaceDecompose = (text: string, use_llm = true) =>
  post<{ factors: IntentFactorResult[]; residual: number; dominant: string | null; sparsity: number }>('/jspace/decompose', { text, use_llm })
export const jspaceGetConfig = () => get<JSpaceConfig>('/jspace/config')
export const jspaceSetConfig = (config: Partial<JSpaceConfig>) => put<{ updated: string[] }>('/jspace/config', config)