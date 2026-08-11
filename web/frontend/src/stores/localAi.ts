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
  const loading = ref(false)
  const error = ref<string | null>(null)
  let loadGeneration = 0
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
    if (!loading.value) {
      loadingUpdates = { devices: new Set(), downloads: new Set(), instances: new Set() }
    }
    loading.value = true
    error.value = null
    try {
      const [nextDevices, nextCatalog, nextModels, nextDownloads, nextInstances, storage] = await Promise.all([
        localAiApi.loadDevices(),
        localAiApi.loadCatalog(),
        localAiApi.loadModels(),
        localAiApi.loadDownloads(),
        localAiApi.loadInstances(),
        localAiApi.loadDefaultStorage(),
      ])
      if (generation !== loadGeneration) return
      devicesById.value = reconcileSnapshot(nextDevices, devicesById.value, loadingUpdates.devices)
      catalogById.value = indexById(nextCatalog)
      modelsById.value = indexById(nextModels)
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
    devicesById.value = indexById(await localAiApi.rescanDevices())
  }

  async function download(request: DownloadRequest) {
    const response = await localAiApi.createDownload(request)
    upsertDownload(response.task)
    return response.task
  }

  async function pause(id: string) { upsertDownload(await localAiApi.pauseDownload(id)) }
  async function resume(id: string) { upsertDownload(await localAiApi.resumeDownload(id)) }
  async function cancel(id: string, discardPartials = false) { upsertDownload(await localAiApi.cancelDownload(id, discardPartials)) }

  async function start(request: StartInstanceRequest) {
    const response = await localAiApi.startInstance(request)
    if (response.instance) upsertInstance(response.instance)
    return response
  }

  async function stop(id: string) { upsertInstance(await localAiApi.stopInstance(id)) }

  async function remove(id: string) {
    await localAiApi.removeModel(id)
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
    devices, catalog, models, downloads, instances, defaultStorage, loading, error,
    load, rescan, download, pause, resume, cancel, start, stop, remove,
    browseStorage, validateStorage, saveDefaultStorage,
    createRequestId, upsertDevice, upsertDownload, upsertInstance, connectWebSocket, disconnectWebSocket,
  }
})
