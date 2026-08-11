import { del, get, post, put } from './index'

export type ModelPurpose = 'chat' | 'embedding' | 'reranker'
export type RuntimeKind = 'ort' | 'ort_genai' | 'vip'

export interface ExecutionBackend {
  runtime: RuntimeKind
  provider: string
  healthy: boolean
  options: Record<string, unknown>
  purposes: ModelPurpose[]
  precisions: string[]
  evidence: Record<string, unknown>
}

export interface ComputeDevice {
  id: string
  name: string
  kind: string
  architecture: string
  state: 'available' | 'unavailable' | 'degraded'
  memory_total: number
  memory_available: number
  backends: ExecutionBackend[]
  system: Record<string, unknown>
  evidence: Record<string, unknown>
}

export interface CatalogFile {
  path: string
  size: number
  sha256: string
}

export interface CatalogModel {
  id: string
  source: string
  repository: string
  revision: string
  purpose: ModelPurpose
  files: CatalogFile[]
  parameter_count: number | null
  quantization: string | null
  download_size: number
  license: string | null
  compatibility: Record<string, unknown>
  runtime_requirements: Record<string, unknown>
}

export interface InstalledModel {
  id: string
  catalog_id: string
  revision: string
  purpose: ModelPurpose
  directory: string
  manifest_checksum: string
  validation_state: string
  ownership: string
  removable: boolean
  installed_at: string
  metadata: Record<string, unknown>
}

export interface DownloadTask {
  id: string
  model_id: string
  state: 'pending' | 'downloading' | 'paused' | 'completed' | 'failed' | 'cancelled' | 'quarantined'
  bytes_downloaded: number
  total_bytes: number
  destination: string
  created_at: string
  updated_at: string
  speed_bps: number | null
  eta_seconds: number | null
  resumable: boolean
  error: string | null
}

export interface ModelInstance {
  id: string
  model_id: string
  runtime: RuntimeKind
  device_id: string
  state: string
  health: string
  started_at: string
  updated_at: string
  active_routes: string[]
  resource_usage: Record<string, unknown>
}

export interface DirectoryListing {
  path: string
  entries: string[]
  error: string | null
}

export interface StorageValidation {
  path: string
  writable: boolean
  free_bytes: number
  error: string | null
  reason: string | null
}

export interface DownloadRequest {
  model_id: string
  destination: string
  request_id: string
}

export interface StartInstanceRequest {
  model_id: string
  device_id: string
  request_id: string
}

export const localAiApi = {
  loadDevices: () => get<ComputeDevice[]>('/local-ai/devices'),
  loadCatalog: () => get<CatalogModel[]>('/local-ai/catalog'),
  loadModels: () => get<InstalledModel[]>('/local-ai/models'),
  loadDownloads: () => get<DownloadTask[]>('/local-ai/downloads'),
  loadInstances: () => get<ModelInstance[]>('/local-ai/instances'),
  rescanDevices: () => post<ComputeDevice[]>('/local-ai/devices/rescan'),
  createDownload: (request: DownloadRequest) => post<{ task: DownloadTask }>('/local-ai/downloads', request),
  pauseDownload: (id: string) => post<DownloadTask>(`/local-ai/downloads/${encodeURIComponent(id)}/pause`),
  resumeDownload: (id: string) => post<DownloadTask>(`/local-ai/downloads/${encodeURIComponent(id)}/resume`),
  cancelDownload: (id: string, discardPartials = false) => post<DownloadTask>(`/local-ai/downloads/${encodeURIComponent(id)}/cancel`, { discard_partials: discardPartials }),
  startInstance: (request: StartInstanceRequest) => post<{ task_id: string; instance?: ModelInstance }>('/local-ai/instances', request),
  stopInstance: (id: string) => post<ModelInstance>(`/local-ai/instances/${encodeURIComponent(id)}/stop`),
  removeModel: (id: string) => del<void>(`/local-ai/models/${encodeURIComponent(id)}`, true),
  browseStorage: (path = '') => get<DirectoryListing>(`/local-ai/storage${path ? `?path=${encodeURIComponent(path)}` : ''}`),
  validateStorage: (path: string, requiredBytes = 0) => post<StorageValidation>('/local-ai/storage/validate', { path, required_bytes: requiredBytes }),
  loadDefaultStorage: () => get<{ default_model_root: string }>('/local-ai/storage/default'),
  saveDefaultStorage: (path: string) => put<{ default_model_root: string }>('/local-ai/storage/default', { path }),
}
