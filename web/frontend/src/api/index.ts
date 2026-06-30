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
  const res = await fetch(`${BASE}${path}`, { ...options, headers })
  if (res.status === 401) {
    localStorage.removeItem('token')
    if (!location.hash.includes('login')) location.hash = '#/login'
    throw new Error('登录已过期，请重新登录')
  }
  // 滑动续期：后端在响应头返回新 token 时自动替换本地存储
  const newToken = res.headers.get('X-New-Token')
  if (newToken) {
    localStorage.setItem('token', newToken)
  }
  let body: ApiEnvelope<T>
  try {
    body = await res.json()
  } catch {
    throw new Error(`HTTP ${res.status}`)
  }
  if (!res.ok || !body.ok) {
    const msg = body?.error?.message || (body as any)?.detail || `HTTP ${res.status}`
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

  getStatus: () => get('/system/status'),
  getSessions: () => get<any[]>('/sessions'),
  createSession: () => post<{ session_id: string }>('/sessions'),
  deleteSession: (id: string) => del(`/sessions/${id}`),
  getMessages: (sessionId: string, before = 0, limit = 50) =>
    get<any[]>(`/sessions/${sessionId}/messages?before=${before}&limit=${limit}`),
  getCommands: () => get<Array<{ name: string; description: string; owner_only: boolean }>>('/commands'),

  getAgents: () => get<any[]>('/agents'),
  getPermissions: (name: string) => get(`/agents/${name}/permissions`),
  setAgentModel: (name: string, provider: string, model_id: string) =>
    post<any>(`/agents/${name}/model`, { provider, model_id }),

  tts: (text: string, voice?: string, style?: string) =>
    post<{ audio_url: string; cached: boolean }>('/media/tts', { text, voice, style }),

  // Setup wizard APIs (no token required — first-run before login)
  getSetupFirstRun: () => {
    return fetch(`${BASE}/setup/first-run`).then(r => r.json()).then(b => b.data)
  },

  getSetupKeys: async () => {
    try {
      const token = localStorage.getItem('token')
      const r = await fetch(`${BASE}/setup/keys`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
      const b = await r.json()
      if (!r.ok || !b.ok) {
        throw new Error(b?.error?.message || `HTTP ${r.status}`)
      }
      return b.data as { keys: any[] }
    } catch (e: any) {
      throw new Error(e.message || 'Failed to load setup keys')
    }
  },

  testSetupKey: (keyName: string, keyValue: string, extra?: Record<string, string>) => {
    const token = localStorage.getItem('token')
    return fetch(`${BASE}/setup/test-key`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ key_name: keyName, key_value: keyValue, ...(extra ? { extra } : {}) }),
    }).then(r => r.json()).then(b => {
      if (!b.ok) throw new Error(b.error?.message || 'Test failed')
      return b.data as { success: boolean; message: string }
    })
  },

  saveSetupKeys: (keys: Record<string, string>, testRequired = false) => {
    const token = localStorage.getItem('token')
    return fetch(`${BASE}/setup/keys`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ keys, test_required: testRequired }),
    }).then(r => r.json()).then(b => {
      if (!b.ok) throw new Error(b.error?.message || 'Save failed')
      return b.data
    })
  },

  getSetupUserProfile: async () => {
    const token = localStorage.getItem('token')
    const r = await fetch(`${BASE}/setup/user-profile`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    const b = await r.json()
    if (!r.ok || !b.ok) throw new Error(b?.error?.message || `HTTP ${r.status}`)
    return b.data as Record<string, string>
  },

  saveSetupUserProfile: (fields: Record<string, string>) => {
    const token = localStorage.getItem('token')
    return fetch(`${BASE}/setup/user-profile`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(fields),
    }).then(r => r.json()).then(b => {
      if (!b.ok) throw new Error(b.error?.message || 'Save failed')
      return b.data
    })
  },

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
  const token = localStorage.getItem('token') || ''
  return `${BASE}/sessions/${sessionId}/export?token=${encodeURIComponent(token)}`
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
