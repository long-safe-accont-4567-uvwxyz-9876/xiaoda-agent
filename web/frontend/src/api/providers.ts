import { del, get, post, put } from './index'

export type ProviderProtocolInput = 'openai' | 'anthropic' | 'ollama' | 'custom-map'
export type ProviderProtocol = 'openai_compatible' | 'anthropic' | 'ollama' | 'custom_mapping'

export interface ProviderCapabilities {
  tools: boolean
  vision: boolean
  streaming: boolean
  model_discovery: boolean
  json_mode: boolean
}

export interface ProviderAuth {
  required: boolean
  header: string
  scheme: string
}

export interface ProviderMapping {
  request: Record<string, string>
  response: Record<string, string>
  stream: Record<string, string>
  models: string
}

export interface ProviderDraft {
  id: string
  label: string
  protocol: ProviderProtocolInput
  base_url: string
  chat_path: string
  models_path: string
  default_model: string
  enabled: boolean
  auth: ProviderAuth
  capabilities: ProviderCapabilities
  headers: Record<string, string>
  mapping: ProviderMapping
}

export interface ProviderDefinition extends Omit<ProviderDraft, 'protocol'> {
  protocol: ProviderProtocol
  builtin: boolean
}

export interface CapabilityReport {
  available: boolean
  capabilities: ProviderCapabilities
  models: string[]
  error: string | null
}

export interface ProviderCredentials {
  api_key?: string
}

export const providerApi = {
  list: () => get<ProviderDefinition[]>("/providers"),
  test: (draft: ProviderDraft, credentials?: ProviderCredentials) => post<CapabilityReport>("/providers/test", { draft, credentials: credentials ?? {} }),
  create: (draft: ProviderDraft, credentials?: ProviderCredentials) => post<ProviderDefinition>("/providers", { draft, credentials: credentials ?? {} }),
  update: (id: string, draft: ProviderDraft, credentials?: ProviderCredentials) => put<ProviderDefinition>(`/providers/${id}`, { draft, credentials: credentials ?? {} }),
  delete: (id: string) => del<{ deleted: string }>(`/providers/${id}`, true),
  capabilities: (id: string) => get<CapabilityReport>(`/providers/${id}` + "/capabilities"),
  models: (id: string) => get<{ provider: string; models: string[] }>(`/providers/${id}` + "/models"),
  setKey: (pid: string, apiKey: string) => post<{ id: string; key_masked: string }>(`/models/providers/${pid}/key`, { api_key: apiKey }),
  reorder: (order: string[]) => post<{ ok: boolean }>('/models/providers/reorder', { order }),
}

function ordered(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(ordered)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, ordered(item)]))
  }
  return value
}

export function normalizeProviderDraft(draft: ProviderDraft): ProviderDraft {
  return ordered(draft) as ProviderDraft
}

export function fingerprintProviderDraft(draft: ProviderDraft): string {
  return JSON.stringify(normalizeProviderDraft(draft))
}