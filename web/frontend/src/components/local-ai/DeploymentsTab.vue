<script setup lang="ts">
import { computed, ref } from 'vue'
import { NButton, NEmpty, NSelect, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'

const store = useLocalAiStore()
const message = useMessage()
const starting = ref('')
const stopping = ref('')
const deviceOptions = computed(() => store.devices.filter(device => device.state === 'available').map(device => ({ label: device.name, value: device.id })))
const selectedDevices = ref<Record<string, string>>({})

const activeInstances = computed(() => store.instances.filter(instance => instance.state !== 'stopped' && instance.state !== 'failed'))
const deployable = computed(() => store.models.filter(item => !activeInstances.value.some(instance => instance.model_id === item.id)))

// 全中文状态映射（此前直接显示英文 state / validated）
const INSTANCE_STATE: Record<string, { text: string; type: 'default' | 'info' | 'success' | 'warning' | 'error' }> = {
  pending: { text: '排队中', type: 'default' },
  starting: { text: '启动中', type: 'info' },
  running: { text: '运行中', type: 'success' },
  stopping: { text: '停止中', type: 'warning' },
  stopped: { text: '已停止', type: 'default' },
  failed: { text: '失败', type: 'error' },
}
const HEALTH_TEXT: Record<string, string> = { healthy: '健康', degraded: '降级', unhealthy: '异常' }
const MODEL_STATE: Record<string, string> = {
  validated: '已验证',
  ready: '就绪',
  pending: '校验中',
  failed: '失败',
}
const purposeText = (purpose?: string | null) => {
  const map: Record<string, string> = { embedding: '向量嵌入', rerank: '语义重排', reranker: '语义重排', chat: '对话', other: '其他' }
  return purpose ? (map[purpose] ?? purpose) : '未标注'
}
const instanceState = (state: string) => INSTANCE_STATE[state] ?? { text: state, type: 'default' as const }
const modelState = (state: string) => MODEL_STATE[state] ?? state

async function start(modelId: string) {
  const deviceId = selectedDevices.value[modelId] || deviceOptions.value[0]?.value
  if (!deviceId) return message.warning('没有可用算力设备')
  starting.value = modelId
  try {
    await store.start({ model_id: modelId, device_id: deviceId, request_id: store.createRequestId() })
    message.success('启动任务已创建，稍后自动出现在「运行中」')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    starting.value = ''
  }
}

async function stop(id: string) {
  stopping.value = id
  try {
    await store.stop(id)
    message.success('已发送停止指令')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    stopping.value = ''
  }
}
</script>

<template>
  <div class="deploy-wrap">
    <!-- 运行中的实例 -->
    <section v-if="activeInstances.length" class="deploy-section">
      <div class="section-head">
        <h4>运行中的实例</h4>
        <span class="section-sub">{{ activeInstances.length }} 个正在提供推理服务</span>
      </div>
      <div class="deploy-grid">
        <article v-for="instance in activeInstances" :key="instance.id" class="glass-panel resource-card running">
          <div class="resource-head">
            <div class="head-name">
              <strong>{{ instance.model_id }}</strong>
              <span>{{ instance.runtime }} · {{ instance.device_id }}</span>
            </div>
            <div class="head-tags">
              <n-tag :type="instanceState(instance.state).type" round>{{ instanceState(instance.state).text }}</n-tag>
              <n-tag v-if="instance.health" size="small" :type="instance.health === 'healthy' ? 'success' : 'warning'">{{ HEALTH_TEXT[instance.health] ?? instance.health }}</n-tag>
            </div>
          </div>
          <div class="resource-meta">用途路由：{{ instance.active_routes.length ? instance.active_routes.join('、') : '未设置' }}</div>
          <div class="resource-actions">
            <n-button size="small" type="warning" :loading="stopping === instance.id" @click="stop(instance.id)">停止</n-button>
          </div>
        </article>
      </div>
    </section>

    <!-- 可部署的本地模型 -->
    <section class="deploy-section">
      <div class="section-head">
        <h4>可部署的本地模型</h4>
        <span class="section-sub">选择算力设备启动后即可提供推理 · 未运行前不可测速</span>
      </div>
      <div v-if="deployable.length" class="deploy-grid">
        <article v-for="model in deployable" :key="model.id" class="glass-panel resource-card">
          <div class="resource-head">
            <div class="head-name">
              <strong>{{ model.id }}</strong>
              <span>{{ purposeText(model.purpose) }} · {{ modelState(model.validation_state) }}</span>
            </div>
            <n-tag>未运行</n-tag>
          </div>
          <div class="resource-actions">
            <n-select
              v-model:value="selectedDevices[model.id]"
              :options="deviceOptions"
              placeholder="选择算力设备"
              :disabled="!deviceOptions.length"
            />
            <n-button type="primary" :loading="starting === model.id" :disabled="!deviceOptions.length" @click="start(model.id)">启动</n-button>
          </div>
        </article>
      </div>
      <n-empty v-else-if="!activeInstances.length && !store.models.length" description="暂无可部署模型，请先在「模型获取」中下载" />
      <div v-else-if="!deployable.length" class="all-running">全部本地模型都在运行中</div>
    </section>
  </div>
</template>

<style scoped>
.deploy-wrap { display: flex; flex-direction: column; gap: 22px; }
.deploy-section { display: flex; flex-direction: column; gap: 10px; }
.section-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.section-head h4 { margin: 0; font-size: 14px; font-weight: 600; }
.section-sub { color: var(--moon-dim); font-size: 12px; }
.deploy-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.resource-card { padding: 16px; border-radius: 14px; }
.resource-card.running { border-color: rgba(143, 229, 96, 0.35); }
.resource-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.head-name strong, .head-name span { display: block; }
.head-name span { margin-top: 3px; color: var(--moon-dim); font-size: 12px; }
.head-tags { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.resource-meta { margin-top: 8px; color: var(--moon-dim); font-size: 12px; }
.resource-actions { margin-top: 14px; display: flex; align-items: center; justify-content: flex-end; gap: 10px; }
.resource-actions .n-select { min-width: 150px; }
.all-running { color: var(--moon-dim); font-size: 13px; padding: 12px 0; }
</style>
