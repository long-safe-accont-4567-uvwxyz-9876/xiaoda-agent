<script setup lang="ts">
import { computed, ref } from 'vue'
import { NButton, NEmpty, NSelect, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'

const store = useLocalAiStore()
const message = useMessage()
const starting = ref('')
const deviceOptions = computed(() => store.devices.filter(device => device.state === 'available').map(device => ({ label: device.name, value: device.id })))
const activeInstances = computed(() => store.instances.filter(instance => instance.state !== 'stopped'))
const selectedDevices = ref<Record<string, string>>({})

async function start(modelId: string) {
  const deviceId = selectedDevices.value[modelId] || deviceOptions.value[0]?.value
  if (!deviceId) return message.warning('没有可用算力设备')
  starting.value = modelId
  try {
    await store.start({ model_id: modelId, device_id: deviceId, request_id: store.createRequestId() })
    message.success('启动任务已创建')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    starting.value = ''
  }
}

async function stop(id: string) {
  try { await store.stop(id) } catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
}
</script>

<template>
  <div class="local-ai-grid">
    <article v-for="instance in activeInstances" :key="instance.id" class="glass-panel resource-card">
      <div class="resource-head"><div><strong>{{ instance.model_id }}</strong><span>{{ instance.runtime }} · {{ instance.device_id }}</span></div><n-tag :type="instance.health === 'healthy' ? 'success' : 'warning'">{{ instance.state }}</n-tag></div>
      <div class="resource-meta">用途路由：{{ instance.active_routes.join('、') || '未设置' }}</div>
      <div class="resource-actions"><n-button size="small" type="warning" @click="stop(instance.id)">停止</n-button></div>
    </article>
    <article v-for="model in store.models.filter(item => !activeInstances.some(instance => instance.model_id === item.id))" :key="model.id" class="glass-panel resource-card">
      <div class="resource-head"><div><strong>{{ model.id }}</strong><span>{{ model.purpose }} · {{ model.validation_state }}</span></div><n-tag>未运行</n-tag></div>
      <div class="resource-actions"><n-select v-model:value="selectedDevices[model.id]" :options="deviceOptions" placeholder="选择设备" /><n-button type="primary" :loading="starting === model.id" @click="start(model.id)">启动</n-button></div>
    </article>
    <n-empty v-if="!activeInstances.length && !store.models.length" description="暂无可部署模型" />
  </div>
</template>

<style scoped>
.local-ai-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.resource-card { padding: 16px; border-radius: 14px; }
.resource-head, .resource-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.resource-head strong, .resource-head span { display: block; }
.resource-head span, .resource-meta { margin-top: 5px; color: var(--moon-dim); font-size: 12px; }
.resource-actions { margin-top: 16px; justify-content: flex-end; }
.resource-actions .n-select { min-width: 150px; }
</style>
