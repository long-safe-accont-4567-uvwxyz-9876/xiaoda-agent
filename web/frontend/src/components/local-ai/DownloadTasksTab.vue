<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { NButton, NEmpty, NModal, NPopconfirm, NProgress, NSelect, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'
import { t } from '../../i18n'

const store = useLocalAiStore()
const message = useMessage()
const completedTask = ref('')
const selectedDevice = ref<string | null>(null)
const deviceOptions = computed(() => store.devices.filter(device => device.state === 'available').map(device => ({ label: device.name, value: device.id })))
const progress = (done: number, total: number) => total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0

// 审查 G1：原先七态名称、按钮、确认文案全部是硬编码中文，切换英文界面后整块
// 面板仍是中文。文案统一进 i18n 的 downloadTasks 命名空间。
const STATE_META: Record<string, { textKey: string; type: 'default' | 'info' | 'success' | 'warning' | 'error' }> = {
  pending: { textKey: 'downloadTasks.statePending', type: 'default' },
  downloading: { textKey: 'downloadTasks.stateDownloading', type: 'info' },
  paused: { textKey: 'downloadTasks.statePaused', type: 'warning' },
  completed: { textKey: 'downloadTasks.stateCompleted', type: 'success' },
  failed: { textKey: 'downloadTasks.stateFailed', type: 'error' },
  cancelled: { textKey: 'downloadTasks.stateCancelled', type: 'warning' },
  quarantined: { textKey: 'downloadTasks.stateQuarantined', type: 'error' },
}
const stateMeta = (state: string) => {
  const meta = STATE_META[state]
  return meta ? { text: t(meta.textKey), type: meta.type } : { text: state, type: 'default' as const }
}
const knownStates = new Map<string, string>()

watch(() => store.downloads, downloads => {
  for (const task of downloads) {
    const previous = knownStates.get(task.id)
    knownStates.set(task.id, task.state)
    if (task.state === 'completed' && previous && previous !== 'completed') {
      completedTask.value = task.id
    }
  }
}, { deep: true, immediate: true })

async function action(kind: 'pause' | 'resume' | 'cancel', id: string) {
  try { await store[kind](id) } catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
}

const removing = ref('')
async function removeTask(id: string) {
  removing.value = id
  try { await store.removeDownload(id) } catch (error) { message.error(error instanceof Error ? error.message : String(error)) } finally { removing.value = '' }
}

async function confirmStart() {
  const task = store.downloadsById[completedTask.value]
  const deviceId = selectedDevice.value || deviceOptions.value[0]?.value
  if (!task || !deviceId) return message.warning(t('downloadTasks.selectDevice'))
  try {
    await store.start({ model_id: task.model_id, device_id: deviceId, request_id: store.createRequestId() })
    message.success(t('downloadTasks.startCreated'))
    completedTask.value = ''
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  }
}
</script>

<template>
  <div class="download-list">
    <article v-for="task in store.downloads" :key="task.id" class="glass-panel download-card">
      <div class="download-head"><div><strong>{{ task.model_id }}</strong><span>{{ task.destination }}</span></div><n-tag :type="stateMeta(task.state).type">{{ stateMeta(task.state).text }}</n-tag></div>
      <n-progress type="line" :percentage="progress(task.bytes_downloaded, task.total_bytes)" />
      <div class="download-meta"><span>{{ task.speed_bps ? `${(task.speed_bps / 1024 / 1024).toFixed(1)} MB/s` : t('downloadTasks.speedWaiting') }}</span><span>{{ task.eta_seconds != null ? `${t('downloadTasks.etaPrefix')} ${task.eta_seconds}s` : '' }}</span></div>
      <div v-if="task.error" class="download-error">{{ task.error }}</div>
      <div class="download-actions"><n-button v-if="task.state === 'downloading'" size="small" @click="action('pause', task.id)">{{ t('downloadTasks.pause') }}</n-button><n-button v-if="task.state === 'paused'" size="small" type="primary" @click="action('resume', task.id)">{{ t('downloadTasks.resume') }}</n-button><n-popconfirm v-if="['pending', 'downloading', 'paused'].includes(task.state)" @positive-click="action('cancel', task.id)"><template #trigger><n-button size="small">{{ t('downloadTasks.cancelDownload') }}</n-button></template>{{ t('downloadTasks.cancelConfirm') }}</n-popconfirm><n-popconfirm @positive-click="removeTask(task.id)"><template #trigger><n-button size="small" type="error" :loading="removing === task.id">{{ t('downloadTasks.deleteTask') }}</n-button></template>{{ t('downloadTasks.deleteConfirm') }}</n-popconfirm><n-button v-if="task.state === 'completed'" size="small" type="primary" @click="completedTask = task.id">{{ t('downloadTasks.startModel') }}</n-button></div>
    </article>
    <n-empty v-if="!store.downloads.length" :description="t('downloadTasks.empty')" />
    <n-modal :show="Boolean(completedTask)" preset="dialog" :title="t('downloadTasks.confirmTitle')" :positive-text="t('downloadTasks.confirmOk')" :negative-text="t('downloadTasks.confirmLater')" @positive-click="confirmStart" @negative-click="completedTask = ''">
      <p>{{ t('downloadTasks.confirmBody') }}</p>
      <n-select v-model:value="selectedDevice" :options="deviceOptions" :placeholder="t('downloadTasks.devicePlaceholder')" />
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
