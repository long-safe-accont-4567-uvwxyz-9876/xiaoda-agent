import { del, get, post, put } from './index'

export type ModelPurpose = 'chat' | 'embedding' | 'reranker'
export type RuntimeKind = 'ort' | 'ort_genai' | 'vip'
export type ModelNodeBackend = 'auto' | 'local' | 'api' | 'off'

export interface ModelNode {
  id: string
  name: string
  kind: 'encoder' | 'generative' | 'other'
  desc: string
  api_model: string
  local_desc: string
  /** 本地实际使用的模型名（如 bge-small-zh-v1.5；生成型节点为本地对话小模型） */
  local_model: string
  default: ModelNodeBackend
  backend: ModelNodeBackend
  api_configured: boolean
  local_available: boolean
  /** 可选的本地模型候选（按节点用途分类，只来自已安装模型） */
  local_models: Array<{
    id: string
    catalog_id: string
    purpose: string
    source: string
    installed: boolean
    ownership?: string
  }>
}

/** 功能节点清单与状态（主 LLM 之外依赖硅基流动免费模型的系统服务） */
export async function fetchModelNodes(): Promise<ModelNode[]> {
  return get<ModelNode[]>('/local-deploy/model-nodes')
}

/** 设置功能节点后端（auto/local/api/off）；选择 local 时可指定具体本地模型 */
export async function setModelNodeBackend(
  node_id: string,
  backend: ModelNodeBackend,
  local_model?: string,
): Promise<void> {
  await put<{ node_id: string; backend: ModelNodeBackend }>(
    '/local-deploy/model-nodes',
    local_model ? { node_id, backend, local_model } : { node_id, backend },
  )
}

/** embedding 引擎实时状态（模式 / 本地引擎是否已启动 / 维度） */
export interface LocalDeployStatus {
  mode: 'local' | 'remote'
  source?: string | null
  engine_running: boolean
  backend?: string
  api_configured: boolean
  model_dir?: string
  dimensions?: number
}

export async function fetchLocalDeployStatus(): Promise<LocalDeployStatus> {
  return get<LocalDeployStatus>('/local-deploy/status')
}

export interface ExecutionBackend {
  runtime: RuntimeKind
  provider: string
  healthy: boolean
  options: Record<string, unknown>
  purposes: ModelPurpose[]
  precisions: string[]
  evidence: Record<string, unknown>
}

export interface ComputeDeviceStats {
  cores?: number | null
  freq_mhz?: number | null
  usage_pct?: number | null
  memory_total?: number | null
  memory_available?: number | null
  utilization_pct?: number | null
  memory_used?: number | null
  memory_free?: number | null
  temperature_c?: number | null
  source?: string
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
  /** 实时负载（CPU 利用率 / GPU 占用等），由 /local-ai/devices 轮询附加 */
  stats?: ComputeDeviceStats
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
  /** 可跑性标注（由后端按已探测算力设备评估） */
  runnable?: { cpu: boolean; gpu: boolean; npu: boolean; gpu_provider: string | null; reason: string }
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

export interface RemoteInspection {
  repository: string
  revision: string
  files: Array<{ path: string; size: number; sha256: string }>
  purpose: ModelPurpose | null
  runnable: boolean
  state: string
  evidence: Record<string, unknown>
  missing: string[]
}

export type HubSource = 'all' | 'hf-mirror' | 'modelscope'

export interface HubSearchResult {
  id: string
  /** 首选来源（优先带不可变 hash 的源，供检视/下载） */
  source: 'hf-mirror' | 'modelscope'
  /** 该仓库出现过的全部来源（跨源同 id 已合并为一行） */
  sources: Array<'hf-mirror' | 'modelscope'>
  name?: string
  downloads: number
  likes: number
  /** HF 镜像默认分支 commit hash */
  sha?: string | null
  /** ModelScope 默认分支 commit hash */
  revision?: string | null
  pipeline_tag?: string | null
  modified_at?: string | null
  tags: string[]
  category?: string
}

export interface HubSearchResponse {
  results: HubSearchResult[]
  /** 失败来源的说明（单源失败不阻断另一源） */
  errors: string[]
}

export interface HubCategory {
  key: string
  label: string
  desc: string
  pipelines: string[]
}

export interface StartInstanceRequest {
  model_id: string
  device_id: string
  request_id: string
}

export interface BenchmarkResult {
  ok: boolean
  model_id: string
  purpose: ModelPurpose | null
  device_id?: string
  error?: string | null
  iterations?: number
  samples?: number
  documents?: number
  tokens?: number
  latency_ms?: number
  samples_per_second?: number
  tokens_per_second?: number
  dimensions?: number
}

export const localAiApi = {
  loadDevices: () => get<ComputeDevice[]>('/local-ai/devices'),
  loadCatalog: (advanced = false) => get<CatalogModel[]>(`/local-ai/catalog${advanced ? '?advanced=true' : ''}`),
  loadModels: () => get<InstalledModel[]>('/local-ai/models'),
  loadDownloads: () => get<DownloadTask[]>('/local-ai/downloads'),
  loadInstances: () => get<ModelInstance[]>('/local-ai/instances'),
  rescanDevices: () => post<ComputeDevice[]>('/local-ai/devices/rescan'),
  inspectRemote: (repository: string, revision: string, source: string = 'modelscope') =>
    get<RemoteInspection>(`/local-ai/remote/inspect?repository=${encodeURIComponent(repository)}&revision=${encodeURIComponent(revision)}&source=${encodeURIComponent(source)}`),
  searchHub: (q: string, source: HubSource = 'all', limit = 20, category = 'all') =>
    get<HubSearchResponse>(`/local-ai/hub/search?q=${encodeURIComponent(q)}&source=${encodeURIComponent(source)}&limit=${limit}&category=${encodeURIComponent(category)}`),
  hubCategories: () => get<HubCategory[]>('/local-ai/hub/categories'),
  downloadHubRepository: (repository: string, revision: string, destination: string, requestId: string, source: string = 'modelscope') =>
    post<{ task: DownloadTask }>('/local-ai/hub/download', { repository, revision, destination, request_id: requestId, source }),
  createDownload: (request: DownloadRequest) => post<{ task: DownloadTask }>('/local-ai/downloads', request),
  pauseDownload: (id: string) => post<DownloadTask>(`/local-ai/downloads/${encodeURIComponent(id)}/pause`),
  resumeDownload: (id: string) => post<DownloadTask>(`/local-ai/downloads/${encodeURIComponent(id)}/resume`),
  cancelDownload: (id: string, discardPartials = false) => post<DownloadTask>(`/local-ai/downloads/${encodeURIComponent(id)}/cancel`, { discard_partials: discardPartials }),
  deleteDownload: (id: string) => del<void>(`/local-ai/downloads/${encodeURIComponent(id)}`, true),
  startInstance: (request: StartInstanceRequest) => post<{ task_id: string; instance?: ModelInstance }>('/local-ai/instances', request),
  getInstanceTask: (taskId: string) => get<{ task_id: string; status: 'pending' | 'completed' | 'failed'; instance?: ModelInstance; error?: { code?: string; message: string; retryable?: boolean } }>(`/local-ai/instances/tasks/${encodeURIComponent(taskId)}`),
  stopInstance: (id: string) => post<ModelInstance>(`/local-ai/instances/${encodeURIComponent(id)}/stop`),
  benchmarkModel: (modelId: string, iterations = 3) =>
    post<BenchmarkResult>(`/local-ai/models/${encodeURIComponent(modelId)}/benchmark`, { iterations }),
  removeModel: (id: string) => del<void>(`/local-ai/models/${encodeURIComponent(id)}`, true),
  browseStorage: (path = '') => get<DirectoryListing>(`/local-ai/storage${path ? `?path=${encodeURIComponent(path)}` : ''}`),
  validateStorage: (path: string, requiredBytes = 0) => post<StorageValidation>('/local-ai/storage/validate', { path, required_bytes: requiredBytes }),
  loadDefaultStorage: () => get<{ default_model_root: string }>('/local-ai/storage/default'),
  saveDefaultStorage: (path: string) => put<{ default_model_root: string }>('/local-ai/storage/default', { path }),
}
