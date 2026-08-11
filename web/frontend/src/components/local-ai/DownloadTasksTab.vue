<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NEmpty, NModal, NProgress, NSelect, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'

const store = useLocalAiStore()
const message = useMessage()
const completedTask = ref('')
const selectedDevice = ref<string | null>(null)
const deviceOptions = computed(() => store.devices.filter(device => device.state === 'available').map(device => ({ label: device.name, value: device.id })))
const progress = (done: number, total: number) => total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0
const knownStates = new Map<string, string>()

watch(() => store.downloads, downloads => {
  for (const task of downloads) {
    const previous = knownStates.get(task.id)
    knownStates.set(task.id, task.state)
    if (task.state === 'completed' && previous && previous !== 'completed') {
      const completed = task
      completedTask.value = completed.id
    }
  }
}, { deep: true, immediate: true })

async function action(kind: 'pause' | 'resume' | 'cancel', id: string) {
  try { await store[kind](id) } catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
}

async function confirmStart() {
  const task = store.downloadsById[completedTask.value]
  const deviceId = selectedDevice.value || deviceOptions.value[0]?.value
  if (!task || !deviceId) return message.warning('请选择可用算力设备')
  try {
    await store.start({ model_id: task.model_id, device_id: deviceId, request_id: store.createRequestId() })
    message.success('启动任务已创建')
    completedTask.value = ''
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}
</script>

<template>
  <div class="download-list">
    <article v-for="task in store.downloads" :key="task.id" class="glass-panel download-card">
      <div class="download-head"><div><strong>{{ task.model_id }}</strong><span>{{ task.destination }}</span></div><n-tag :type="task.state === 'completed' ? 'success' : task.state === 'failed' ? 'error' : 'default'">{{ task.state }}</n-tag></div>
      <n-progress type="line" :percentage="progress(task.bytes_downloaded, task.total_bytes)" />
      <div class="download-meta"><span>{{ task.speed_bps ? `${(task.speed_bps / 1024 / 1024).toFixed(1)} MB/s` : '等待速度数据' }}</span><span>{{ task.eta_seconds != null ? `剩余 ${task.eta_seconds}s` : '' }}</span></div>
      <div v-if="task.error" class="download-error">{{ task.error }}</div>
      <div class="download-actions"><n-button v-if="task.state === 'downloading'" size="small" @click="action('pause', task.id)">暂停</n-button><n-button v-if="task.state === 'paused'" size="small" type="primary" @click="action('resume', task.id)">继续</n-button><n-button v-if="['pending', 'downloading', 'paused'].includes(task.state)" size="small" type="error" @click="action('cancel', task.id)">取消</n-button><n-button v-if="task.state === 'completed'" size="small" type="primary" @click="completedTask = task.id">安装完成，启动</n-button></div>
    </article>
    <n-empty v-if="!store.downloads.length" description="暂无下载任务" />
    <n-modal :show="Boolean(completedTask)" preset="dialog" title="确认启动" positive-text="启动" negative-text="稍后" @positive-click="confirmStart" @negative-click="completedTask = ''">
      <p>安装完成。是否立即启动这个模型？</p>
      <n-select v-model:value="selectedDevice" :options="deviceOptions" placeholder="选择算力设备" />
    </n-modal>
  </div>
</template>

<style scoped>
.download-list { display: grid; gap: 12px; }
.download-card { padding: 16px; border-radius: 14px; }
.download-head, .download-meta, .download-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.download-head { margin-bottom: 12px; }
.download-head strong, .download-head span { display: block; }
.download-head span, .download-meta { margin-top: 4px; color: var(--moon-dim); font-size: 12px; }
.download-actions { margin-top: 14px; justify-content: flex-end; }
.download-error { margin-top: 8px; color: #ef8080; font-size: 12px; }
</style>
