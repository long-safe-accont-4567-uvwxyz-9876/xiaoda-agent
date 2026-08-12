import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  localAiApi,
  type CatalogModel,
  type ComputeDevice,
  type DirectoryListing,
  type DownloadRequest,
  type DownloadTask,
  type InstalledModel,
  type ModelInstance,
  type StartInstanceRequest,
  type StorageValidation,
} from '../api/localAi'
import { getWsClient, type LocalAiWsEvent, type WsEvent } from '../api/ws'

function indexById<T extends { id: string }>(items: T[]): Record<string, T> {
  return Object.fromEntries(items.map(item => [item.id, item]))
}

function reconcileSnapshot<T extends { id: string }>(snapshot: T[], current: Record<string, T>, protectedIds: Set<string>) {
  const next = indexById(snapshot)
  protectedIds.forEach(id => {
    if (current[id]) next[id] = current[id]
  })
  return next
}

let requestSequence = 0

function createRequestId() {
  const randomUUID = globalThis.crypto?.randomUUID
  if (typeof randomUUID === 'function') return randomUUID.call(globalThis.crypto)
  requestSequence += 1
  return `local-ai-${Date.now().toString(36)}-${requestSequence.toString(36)}-${Math.random().toString(36).slice(2)}`
}

export const useLocalAiStore = defineStore('localAi', () => {
  const devicesById = ref<Record<string, ComputeDevice>>({})
  const catalogById = ref<Record<string, CatalogModel>>({})
  const modelsById = ref<Record<string, InstalledModel>>({})
  const downloadsById = ref<Record<string, DownloadTask>>({})
  const instancesById = ref<Record<string, ModelInstance>>({})
  const defaultStorage = ref('')
  const catalogAdvanced = ref(false)
  const loading = ref(false)
  const rescanning = ref(false)
  const error = ref<string | null>(null)
  let loadGeneration = 0
  let deviceGeneration = 0
  let modelGeneration = 0
  const modelDeletionTombstones = new Map<string, number>()
  let listening = false
  let loadingUpdates = { devices: new Set<string>(), downloads: new Set<string>(), instances: new Set<string>() }

  const devices = computed(() => Object.values(devicesById.value))
  const catalog = computed(() => Object.values(catalogById.value))
  const models = computed(() => Object.values(modelsById.value))
  const downloads = computed(() => Object.values(downloadsById.value))
  const instances = computed(() => Object.values(instancesById.value))

  function upsertDevice(device: ComputeDevice) {
    devicesById.value = { ...devicesById.value, [device.id]: device }
  }

  function upsertDownload(download: DownloadTask) {
    downloadsById.value = { ...downloadsById.value, [download.id]: download }
  }

  function upsertInstance(instance: ModelInstance) {
    instancesById.value = { ...instancesById.value, [instance.id]: instance }
  }

  function commitModelSnapshot(snapshot: InstalledModel[], generation: number) {
    if (generation !== modelGeneration) return
    modelsById.value = indexById(snapshot.filter(model => {
      const deletionGeneration = modelDeletionTombstones.get(model.id)
      return deletionGeneration === undefined || generation > deletionGeneration
    }))
    modelDeletionTombstones.forEach((deletionGeneration, id) => {
      if (generation > deletionGeneration || !snapshot.some(model => model.id === id)) {
        modelDeletionTombstones.delete(id)
      }
    })
  }

  async function refreshModels() {
    const generation = ++modelGeneration
    error.value = null
    try {
      const nextModels = await localAiApi.loadModels()
      commitModelSnapshot(nextModels, generation)
    } catch (cause) {
      if (generation === modelGeneration) error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    }
  }

  function eventResource<T>(event: WsEvent, key: string): T | null {
    const direct = event[key]
    if (direct && typeof direct === 'object') return direct as T
    const data = event.data
    if (data && typeof data === 'object' && key in data) return (data as Record<string, T>)[key]
    return null
  }

  const onDeviceUpdated = (event: LocalAiWsEvent) => {
    const device = eventResource<ComputeDevice>(event, 'device')
    if (device) {
      if (loading.value) loadingUpdates.devices.add(device.id)
      upsertDevice(device)
    }
  }
  const onDownloadUpdated = (event: LocalAiWsEvent) => {
    const download = eventResource<DownloadTask>(event, 'download')
      ?? eventResource<DownloadTask>(event, 'task')
    if (download) {
      if (loading.value) loadingUpdates.downloads.add(download.id)
      upsertDownload(download)
      if (download.state === 'completed') void refreshModels().catch(() => undefined)
    }
  }
  const onInstanceUpdated = (event: LocalAiWsEvent) => {
    const instance = eventResource<ModelInstance>(event, 'instance')
    if (instance) {
      if (loading.value) loadingUpdates.instances.add(instance.id)
      upsertInstance(instance)
    }
  }

  async function load() {
    const generation = ++loadGeneration
    const currentDeviceGeneration = ++deviceGeneration
    const currentModelGeneration = ++modelGeneration
    if (!loading.value) {
      loadingUpdates = { devices: new Set(), downloads: new Set(), instances: new Set() }
    }
    loading.value = true
    error.value = null
    try {
      const [nextDevices, nextCatalog, nextModels, nextDownloads, nextInstances, storage] = await Promise.all([
        localAiApi.loadDevices(),
        localAiApi.loadCatalog(catalogAdvanced.value),
        localAiApi.loadModels(),
        localAiApi.loadDownloads(),
        localAiApi.loadInstances(),
        localAiApi.loadDefaultStorage(),
      ])
      if (generation !== loadGeneration) return
      if (currentDeviceGeneration === deviceGeneration) {
        devicesById.value = reconcileSnapshot(nextDevices, devicesById.value, loadingUpdates.devices)
      }
      catalogById.value = indexById(nextCatalog)
      commitModelSnapshot(nextModels, currentModelGeneration)
      downloadsById.value = reconcileSnapshot(nextDownloads, downloadsById.value, loadingUpdates.downloads)
      instancesById.value = reconcileSnapshot(nextInstances, instancesById.value, loadingUpdates.instances)
      defaultStorage.value = storage.default_model_root
    } catch (cause) {
      if (generation !== loadGeneration) return
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    } finally {
      if (generation === loadGeneration) loading.value = false
    }
  }

  async function rescan() {
    const generation = ++deviceGeneration
    rescanning.value = true
    try {
      const nextDevices = await localAiApi.rescanDevices()
      if (generation === deviceGeneration) devicesById.value = indexById(nextDevices)
    } finally {
      rescanning.value = false
    }
  }

  /** 切换"显示大模型"开关时仅刷新目录，避免重复触发设备扫描 */
  async function refreshCatalog(advanced: boolean) {
    catalogAdvanced.value = advanced
    catalogById.value = indexById(await localAiApi.loadCatalog(advanced))
  }

  async function download(request: DownloadRequest) {
    const response = await localAiApi.createDownload(request)
    upsertDownload(response.task)
    return response.task
  }

  async function pause(id: string) { upsertDownload(await localAiApi.pauseDownload(id)) }
  async function resume(id: string) { upsertDownload(await localAiApi.resumeDownload(id)) }
  async function cancel(id: string, discardPartials = false) { upsertDownload(await localAiApi.cancelDownload(id, discardPartials)) }

  async function removeDownload(id: string) {
    await localAiApi.deleteDownload(id)
    const next = { ...downloadsById.value }
    delete next[id]
    downloadsById.value = next
  }

  async function start(request: StartInstanceRequest) {
    const response = await localAiApi.startInstance(request)
    if (response.instance) {
      upsertInstance(response.instance)
      return response
    }
    // 202 首启仅返回 task_id：轮询任务状态直至完成/失败（约 60s 超时），
    // 避免启动失败对用户完全静默（WS 未连接或事件丢失时仍有兜底）
    const taskId = response.task_id
    const deadline = Date.now() + 60_000
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 1000))
      const task = await localAiApi.getInstanceTask(taskId)
      if (task.status === 'completed' && task.instance) {
        upsertInstance(task.instance)
        return { task_id: taskId, instance: task.instance }
      }
      if (task.status === 'failed') {
        throw new Error(task.error?.message || `实例启动失败（${taskId}）`)
      }
    }
    throw new Error(`实例启动超时（${taskId}）`)
  }

  async function stop(id: string) { upsertInstance(await localAiApi.stopInstance(id)) }

  async function remove(id: string) {
    await localAiApi.removeModel(id)
    modelDeletionTombstones.set(id, modelGeneration)
    const next = { ...modelsById.value }
    delete next[id]
    modelsById.value = next
  }

  function browseStorage(path = ''): Promise<DirectoryListing> {
    return localAiApi.browseStorage(path)
  }

  function validateStorage(path: string, requiredBytes = 0): Promise<StorageValidation> {
    return localAiApi.validateStorage(path, requiredBytes)
  }

  async function saveDefaultStorage(path: string) {
    const response = await localAiApi.saveDefaultStorage(path)
    defaultStorage.value = response.default_model_root
  }

  function connectWebSocket() {
    if (listening) return
    const ws = getWsClient()
    ws.on('local_ai_device_updated', onDeviceUpdated)
    ws.on('local_ai_download_updated', onDownloadUpdated)
    ws.on('local_ai_instance_updated', onInstanceUpdated)
    listening = true
  }

  function disconnectWebSocket() {
    if (!listening) return
    const ws = getWsClient()
    ws.off('local_ai_device_updated', onDeviceUpdated)
    ws.off('local_ai_download_updated', onDownloadUpdated)
    ws.off('local_ai_instance_updated', onInstanceUpdated)
    listening = false
  }

  return {
    devicesById, catalogById, modelsById, downloadsById, instancesById,
    devices, catalog, models, downloads, instances, defaultStorage, catalogAdvanced, loading, rescanning, error,
    load, rescan, download, pause, resume, cancel, removeDownload, start, stop, remove,
    refreshModels, refreshCatalog, browseStorage, validateStorage, saveDefaultStorage,
    createRequestId, upsertDevice, upsertDownload, upsertInstance, connectWebSocket, disconnectWebSocket,
  }
})
