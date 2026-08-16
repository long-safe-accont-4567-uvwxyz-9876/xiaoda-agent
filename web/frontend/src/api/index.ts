import { t } from '../i18n'

const BASE = '/api/v1'

interface ApiEnvelope<T> {
  ok: boolean
  data: T | null
  error?: { code: string; message: string }
}

async function request<T>(path: string, options?: RequestInit, confirm = false): Promise<T> {
  const token = localStorage.getItem('token')
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(confirm ? { 'X-Confirm': 'yes' } : {}),
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
    let msg: any = body?.error?.message
    if (!msg && (body as any)?.detail) {
      const d = (body as any).detail
      // 兼容结构化 detail（如 {code, message}）与纯字符串 detail
      msg = typeof d === 'string' ? d : (d?.message || d?.code || JSON.stringify(d))
    }
    if (!msg) msg = `HTTP ${res.status}`
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg))
  }
  return body.data as T
}

export const get = <T = any>(path: string) => request<T>(path)
export const post = <T = any>(path: string, body?: unknown, confirm = false) =>
  request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined }, confirm)
export const put = <T = any>(path: string, body?: unknown, confirm = false) =>
  request<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}) }, confirm)
export const del = <T = any>(path: string, confirm = false) =>
  request<T>(path, { method: 'DELETE' }, confirm)

// ── 工作流类型 ──
export interface WorkflowNode {
  id: string
  type: 'tool' | 'skill' | 'mcp' | 'agent' | 'model' | 'step'
  ref?: string
  label: string
  params?: Record<string, any>
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

  getStatus: () => get('/system/status'),
  getSessions: () => get<any[]>('/sessions'),
  createSession: () => post<{ session_id: string }>('/sessions'),
  deleteSession: (id: string) => del(`/sessions/${id}`),
  getMessages: (sessionId: string, before = 0, limit = 50) =>
    get<any[]>(`/sessions/${sessionId}/messages?before=${before}&limit=${limit}`),
  getCommands: () => get<Array<{ name: string; description: string; owner_only: boolean }>>('/commands'),
  testModelRoute: (route: string) => post<{ ok: boolean; error?: string }>('/health/test/llm', { route }),

  getAgents: () => get<any[]>('/agents'),
  getPermissions: (name: string) => get(`/agents/${name}/permissions`),
  setAgentModel: (name: string, provider: string, model_id: string) =>
    post<any>(`/agents/${name}/model`, { provider, model_id }),

  tts: (text: string, voice?: string, style?: string) =>
    post<{ audio_url: string; cached: boolean }>('/media/tts', { text, voice, style }),

  uploadVoiceRef: async (agent: string, formData: FormData) => {
    const token = localStorage.getItem('token')
    const res = await fetch(`${BASE}/media/tts/voices/${agent}`, {
      method: 'POST',
      headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: formData,
    })
    const body = await res.json()
    if (!res.ok || !body.ok) throw new Error(body?.error?.message || 'Upload failed')
    return body.data
  },

  // Setup wizard APIs（首次运行时后端免认证；非首次需 token，统一走 request()
  // 以自动处理 X-New-Token 滑动续期，token 失效时引导重新登录而非裸 401 报错）
  getSetupFirstRun: () => {
    return fetch(`${BASE}/setup/first-run`).then(r => r.json()).then(b => b.data)
  },

  getSetupKeys: () => get<{ keys: any[] }>('/setup/keys'),

  testSetupKey: (keyName: string, keyValue: string, extra?: Record<string, string>) =>
    post<{ success: boolean; message: string }>('/setup/test-key', { key_name: keyName, key_value: keyValue, ...(extra ? { extra } : {}) }),

  saveSetupKeys: (keys: Record<string, string>, testRequired = false, extra: Record<string, unknown> = {}) =>
    post<unknown>('/setup/keys', { keys, test_required: testRequired, ...extra }),

  getSetupUserProfile: () => get<Record<string, string>>('/setup/user-profile'),

  saveSetupUserProfile: (fields: Record<string, string>) =>
    post<unknown>('/setup/user-profile', fields),

  // Custom provider (needs auth)
  createProvider: (data: { id: string; label: string; format: string; base_url: string; default_model: string; api_key: string }) =>
    post('/models/providers', data),

  // ── 表情包管理 ──
  listStickers: (agentName: string) =>
    get<{ stickers: Array<{ name: string; description: string; emotion: string; url: string }>; emotions: string[] }>(`/agents/${agentName}/stickers`),

  uploadSticker: async (agentName: string, file: File, description: string, emotion: string) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('description', description)
    formData.append('emotion', emotion)
    const token = localStorage.getItem('token')
    const res = await fetch(`${BASE}/agents/${agentName}/stickers`, {
      method: 'POST',
      headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: formData,
    })
    const body = await res.json()
    if (!res.ok || !body.ok) throw new Error(body?.error?.message || 'Upload failed')
    return body.data as { name: string; description: string; emotion: string; url: string }
  },

  deleteSticker: (agentName: string, filename: string) =>
    del<{ deleted: string }>(`/agents/${agentName}/stickers/${encodeURIComponent(filename)}`, true),

  uploadImage: async (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    const token = localStorage.getItem('token')
    const res = await fetch(`${BASE}/chat/upload-image`, {
      method: 'POST',
      headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: formData,
    })
    const body = await res.json()
    if (!res.ok || !body.ok) throw new Error(body?.error?.message || 'Upload failed')
    return body.data as { url: string; name: string }
  },

  // P0 新增（Task 1.9）：文档上传 — 与图片上传分离
  // 文档（PDF/DOCX 等）走 document_reader 工具，而非 vision API
  uploadDoc: async (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    const token = localStorage.getItem('token')
    const res = await fetch(`${BASE}/chat/upload-doc`, {
      method: 'POST',
      headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: formData,
    })
    const body = await res.json()
    if (!res.ok || !body.ok) throw new Error(body?.error?.message || 'Upload failed')
    return body.data as { url: string; name: string; path: string; ext: string }
  },

  speechToText: async (file: File) => {
    const formData = new FormData()
    formData.append('file', file)
    const token = localStorage.getItem('token')
    const res = await fetch(`${BASE}/chat/speech-to-text`, {
      method: 'POST',
      headers: { ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: formData,
    })
    const body = await res.json()
    if (!res.ok || !body.ok) throw new Error(body?.error?.message || 'STT failed')
    return body.data as { text: string }
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

  // 品牌署名与免责协议
  getBrandSignature: () => get<{ signature: string; author: string; version: string }>('/brand/signature'),
  getDisclaimerStatus: () => get<{ agreed: boolean; agreed_at: string; text: string }>('/setup/disclaimer-status'),
  agreeDisclaimer: (agreed: boolean) => post<{ success: boolean }>('/setup/agree-disclaimer', { agreed }),
}

export async function getSetupVersion(): Promise<{ version: string }> {
  return get('/setup/version')
}

export function exportSessionUrl(sessionId: string): string {
  // Token 不再通过 URL 查询参数暴露，改为 POST 下载
  // 此函数保留兼容性但返回空字符串，实际下载由 exportSessionDownload 完成
  return ''
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
export const getNotes = (params?: Record<string, any>) =>
  get<any[]>('/insight/notebook' + (params ? '?' + new URLSearchParams(params as any).toString() : ''))

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
  get<any[]>(`/insight/knowledge/entities?limit=${limit}`)

export const listKnowledgeRelations = (limit = 200) =>
  get<any[]>(`/insight/knowledge/relations?limit=${limit}`)

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

export async function getXpState(): Promise<XpState> {
  return get('/insight/xp')
}

export async function getXpLevels(): Promise<{ levels: XpLevelConfig[] }> {
  return get('/insight/xp/levels')
}

export const updateKnowledgeRelation = (id: string, data: { relation: string }) =>
  put<{ id: string; updated: boolean }>(`/insight/knowledge/relations/${id}`, data)

export const getKnowledgeGraph = (entity = '', depth = 1) =>
  get<{ nodes: any[]; edges: any[] }>(`/insight/knowledge/graph?entity=${encodeURIComponent(entity)}&depth=${depth}`)

// ── 品牌署名与免责协议 ──
export const getBrandSignature = () =>
  get<{ signature: string; author: string; version: string }>('/brand/signature')
export const getDisclaimerStatus = () =>
  get<{ agreed: boolean; agreed_at: string; text: string }>('/setup/disclaimer-status')
export const agreeDisclaimer = (agreed: boolean) =>
  post<{ success: boolean }>('/setup/agree-disclaimer', { agreed })
