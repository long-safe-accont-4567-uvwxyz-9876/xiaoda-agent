<script setup lang="ts">
import { ref } from 'vue'
import { NButton, NEmpty, NPopconfirm, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'
import { localAiApi, type BenchmarkResult } from '../../api/localAi'

const store = useLocalAiStore()
const message = useMessage()
const removing = ref('')
const benchmarking = ref('')
const benchmarkResult = ref<Record<string, BenchmarkResult>>({})

const PURPOSE_TEXT: Record<string, string> = { chat: '对话', embedding: '向量嵌入', reranker: '语义重排' }
const OWNERSHIP_TEXT: Record<string, string> = { bundled: '内置模型', user: '用户安装' }
const VALIDATION_TEXT: Record<string, string> = { valid: '已验证', validated: '已验证', invalid: '未通过校验', pending: '校验中' }
const purposeText = (purpose: string) => PURPOSE_TEXT[purpose] ?? purpose
const ownershipText = (ownership: string) => OWNERSHIP_TEXT[ownership] ?? ownership
const validationMeta = (state: string) => ({ text: VALIDATION_TEXT[state] ?? state, type: state === 'valid' || state === 'validated' ? 'success' : 'warning' } as const)
const revisionText = (revision: string) => /^0+$/.test(revision) ? '内置版本' : revision
/** 是否有正在运行的实例：测速仅对已启动的模型有意义 */
const isRunning = (modelId: string) => store.instances.some(instance => instance.model_id === modelId && instance.state !== 'stopped')

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
    benchmarkResult.value[id] = await localAiApi.benchmarkModel(id)
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
        <n-popconfirm v-if="model.removable" @positive-click="remove(model.id)"><template #trigger><n-button size="small" type="error" :loading="removing === model.id">移除</n-button></template>确认移除该模型？安装登记和模型目录文件将一并删除（不可恢复）。若模型正在使用中或仍有下载任务，将被拒绝。</n-popconfirm>
        <n-tag v-else size="small">内置模型</n-tag>
      </div>
    </article>
    <n-empty v-if="!store.models.length" description="暂无已安装模型" />
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
</style>
