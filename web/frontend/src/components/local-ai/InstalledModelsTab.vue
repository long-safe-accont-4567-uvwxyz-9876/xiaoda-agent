<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NEmpty, NPopconfirm, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore, type BenchmarkResult } from '../../stores/localAi'
import StoragePickerDialog from './StoragePickerDialog.vue'

const store = useLocalAiStore()
const message = useMessage()
const removing = ref('')
const benchmarking = ref('')
const benchmarkResult = ref<Record<string, BenchmarkResult>>({})
const downloading = ref('')
const showStorage = ref(false)
const pendingModelId = ref('')

// 目录中「已收录但未下载」的候选（排除已安装），供灰色展示 + 一键下载
const pendingCatalog = computed(() => {
  const installedIds = new Set(store.models.map(m => m.catalog_id).filter(Boolean))
  return store.catalog.filter(c => !installedIds.has(c.id))
})

const PURPOSE_TEXT: Record<string, string> = { chat: '对话', embedding: '向量嵌入', reranker: '语义重排' }
const OWNERSHIP_TEXT: Record<string, string> = { bundled: '内置模型', user: '用户安装' }
const VALIDATION_TEXT: Record<string, string> = { valid: '已验证', validated: '已验证', invalid: '未通过校验', pending: '校验中' }
const purposeText = (purpose: string) => PURPOSE_TEXT[purpose] ?? purpose
const ownershipText = (ownership: string) => OWNERSHIP_TEXT[ownership] ?? ownership
const validationMeta = (state: string) => ({ text: VALIDATION_TEXT[state] ?? state, type: state === 'valid' || state === 'validated' ? 'success' : 'warning' } as const)
const revisionText = (revision: string) => /^0+$/.test(revision) ? '内置版本' : revision
/** 是否有正在运行的实例：测速仅对已启动的模型有意义 */
const isRunning = (modelId: string) => store.instances.some(instance => instance.model_id === modelId && instance.state !== 'stopped' && instance.state !== 'failed')

async function remove(id: string) {
  removing.value = id
  try {
    await store.remove(id)
    message.success('模型已移除')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    removing.value = ''
  }
}

async function benchmark(id: string) {
  benchmarking.value = id
  try {
    benchmarkResult.value[id] = await store.benchmarkModel(id)
  } catch (error) {
    benchmarkResult.value[id] = { ok: false, model_id: id, purpose: null, error: error instanceof Error ? error.message : String(error) }
  } finally {
    benchmarking.value = ''
  }
}

function resultText(result: BenchmarkResult | undefined): string | null {
  if (!result) return null
  if (!result.ok) return `测速失败：${result.error ?? '未知错误'}`
  const parts: string[] = []
  if (result.latency_ms != null) parts.push(`单次延迟 ${result.latency_ms} ms`)
  if (result.tokens_per_second != null) parts.push(`吞吐 ${result.tokens_per_second} token/s`)
  if (result.samples_per_second != null) parts.push(`吞吐 ${result.samples_per_second} 条/秒`)
  if (result.dimensions != null) parts.push(`向量维度 ${result.dimensions}`)
  if (result.iterations != null) parts.push(`采样 ${result.iterations} 次`)
  return parts.join(' · ')
}

// ── 目录候选一键下载（灰色未下载 → 下载 → 已安装白） ──
function createRequestId() { return store.createRequestId() }

async function doDownload(modelId: string, destination: string) {
  downloading.value = modelId
  try {
    await store.download({ model_id: modelId, destination, request_id: createRequestId() })
    pendingModelId.value = ''
    message.success(`已开始下载 ${modelId}`)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    downloading.value = ''
  }
}

async function downloadModel(modelId: string) {
  if (downloading.value) return
  pendingModelId.value = modelId
  if (store.defaultStorage) {
    try {
      const validation = await store.validateStorage(store.defaultStorage, 0)
      if (validation.writable && !validation.error) {
        await doDownload(modelId, validation.path)
        return
      }
      message.warning(validation.error || validation.reason || '默认目录不可用，请重新选择')
    } catch {
      message.warning('默认目录不可用，请重新选择')
    }
  }
  showStorage.value = true
}

function selectStorage(path: string) {
  showStorage.value = false
  if (pendingModelId.value) void doDownload(pendingModelId.value, path)
}

function formatCatalogSize(size: number) {
  if (size >= 1024 * 1024 * 1024) return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}

// 下载完成 → 刷新已安装列表（WS 兜底，避免未连接时候选不更新）
watch(
  () => store.downloads.filter(d => d.state === 'completed').length,
  (count, prev) => { if (count > (prev ?? 0)) void store.refreshModels().catch(() => undefined) },
)
</script>

<template>
  <div class="installed-list">
    <article v-for="model in store.models" :key="model.id" class="glass-panel installed-card">
      <div class="installed-main"><div><strong>{{ model.id }}</strong><span>{{ model.directory }}</span></div><n-tag :type="validationMeta(model.validation_state).type">{{ validationMeta(model.validation_state).text }}</n-tag></div>
      <div class="installed-meta"><span>用途：{{ purposeText(model.purpose) }}</span><span>版本：{{ revisionText(model.revision) }}</span><span>{{ ownershipText(model.ownership) }}</span></div>
      <div v-if="resultText(benchmarkResult[model.id])" class="benchmark-line" :class="{ failed: benchmarkResult[model.id] && !benchmarkResult[model.id].ok }">{{ resultText(benchmarkResult[model.id]) }}</div>
      <div class="installed-actions">
        <span v-if="!isRunning(model.id)" class="not-running">未启动，启动后才能测速</span>
        <n-button size="small" :disabled="!isRunning(model.id)" :loading="benchmarking === model.id" @click="benchmark(model.id)">测速</n-button>
        <n-popconfirm v-if="model.removable" @positive-click="remove(model.id)"><template #trigger><n-button size="small" type="error" :loading="removing === model.id">移除</n-button></template>确认移除该模型？仅移除安装登记，不会删除模型目录或文件。若模型正在使用中或仍有下载任务，将被拒绝。</n-popconfirm>
        <n-tag v-else size="small">内置模型</n-tag>
      </div>
    </article>

    <!-- 目录中已收录但未下载的候选（灰色，一键下载） -->
    <article v-for="catalog in pendingCatalog" :key="catalog.id" class="glass-panel installed-card pending-card">
      <div class="installed-main"><div><strong>{{ catalog.id }}</strong><span>{{ catalog.repository }}</span></div><n-tag size="small" type="warning">暂未下载</n-tag></div>
      <div class="installed-meta"><span>用途：{{ purposeText(catalog.purpose) }}</span><span>大小：{{ formatCatalogSize(catalog.download_size) }}</span></div>
      <div class="installed-actions">
        <n-button size="small" type="primary" :loading="downloading === catalog.id" @click="downloadModel(catalog.id)">{{ downloading === catalog.id ? '下载中…' : '下载' }}</n-button>
      </div>
    </article>

    <n-empty v-if="!store.models.length && !pendingCatalog.length" description="暂无模型" />

    <StoragePickerDialog :show="showStorage" :required-bytes="0" @select="selectStorage" @cancel="showStorage = false" />
  </div>
</template>

<style scoped>
.installed-list { display: grid; gap: 12px; }
.installed-card { padding: 16px; border-radius: 14px; }
.installed-main, .installed-meta, .installed-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.installed-main strong, .installed-main span { display: block; }
.installed-main span { max-width: 620px; margin-top: 4px; overflow: hidden; color: var(--moon-dim); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.installed-meta { justify-content: flex-start; margin-top: 12px; color: var(--moon-dim); font-size: 12px; }
.installed-actions { margin-top: 14px; justify-content: flex-end; }
.benchmark-line { margin-top: 12px; padding: 8px 12px; border-radius: 10px; background: var(--moon-soft); color: var(--text-2); font-size: 12px; }
.benchmark-line.failed { color: #d03050; }
.not-running { color: var(--moon-dim); font-size: 12px; }
.pending-card { opacity: 0.72; border: 1px dashed rgba(128, 128, 128, 0.35); }
</style>
