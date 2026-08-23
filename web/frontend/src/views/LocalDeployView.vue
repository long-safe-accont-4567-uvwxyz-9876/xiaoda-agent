<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NAlert, NButton, NSpin, NTabPane, NTabs, useMessage } from 'naive-ui'
import ComputeDevicesTab from '../components/local-ai/ComputeDevicesTab.vue'
import DeploymentsTab from '../components/local-ai/DeploymentsTab.vue'
import DownloadTasksTab from '../components/local-ai/DownloadTasksTab.vue'
import InstalledModelsTab from '../components/local-ai/InstalledModelsTab.vue'
import ModelMarketTab from '../components/local-ai/ModelMarketTab.vue'
import SystemModelNodesTab from '../components/local-ai/SystemModelNodesTab.vue'
import { useLocalAiStore } from '../stores/localAi'
import { t } from '../i18n'

const store = useLocalAiStore()
const message = useMessage()
const activeTab = ref('deployments')
const summary = computed(() => {
  const running = store.instances.filter(item => item.state !== 'stopped' && item.state !== 'failed').length
  const available = store.devices.filter(item => item.state === 'available').length
  const downloading = store.downloads.filter(item => ['pending', 'downloading', 'paused'].includes(item.state)).length
  return `${running} ${t('localDeployView.unitInstances')} · ${store.models.length} ${t('localDeployView.unitModels')} · ${available} ${t('localDeployView.unitDevices')} · ${downloading} ${t('localDeployView.unitDownloads')}`
})

async function load() {
  try { await store.load() } catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
}

onMounted(() => {
  store.connectWebSocket()
  load()
})
onBeforeUnmount(() => store.disconnectWebSocket())
</script>

<template>
  <div class="local-deploy-view">
    <header class="view-header"><div><h2>🖥️ {{ t('localDeployView.title') }}</h2><p>{{ summary }}</p></div><n-button :loading="store.loading" @click="load">{{ t('refresh') }}</n-button></header>
    <n-alert v-if="store.error" type="error" :title="t('localDeployView.loadFailed')">{{ store.error }}</n-alert>
    <n-spin :show="store.loading && !store.devices.length && !store.catalog.length">
      <n-tabs v-model:value="activeTab" type="line" animated display-directive="show" class="local-ai-tabs">
        <n-tab-pane name="deployments" :tab="t('localDeployView.tabDeploy')"><DeploymentsTab /></n-tab-pane>
        <n-tab-pane name="market" :tab="t('localDeployView.tabMarket')"><ModelMarketTab /></n-tab-pane>
        <n-tab-pane name="installed" :tab="t('localDeployView.tabInstalled')"><InstalledModelsTab /></n-tab-pane>
        <n-tab-pane name="devices" :tab="t('localDeployView.tabDevicesShort')"><ComputeDevicesTab /></n-tab-pane>
        <n-tab-pane name="nodes" :tab="t('localDeployView.tabNodes')"><SystemModelNodesTab /></n-tab-pane>
        <n-tab-pane name="downloads" :tab="t('localDeployView.tabDownloads')"><DownloadTasksTab /></n-tab-pane>
      </n-tabs>
    </n-spin>
  </div>
</template>

<style scoped>
.local-deploy-view { max-width: 1080px; margin: 0 auto; padding: 20px 24px; }
.view-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 18px; }
.view-header h2 { margin: 0; font-family: 'Noto Serif SC', serif; }
.view-header p { margin: 6px 0 0; color: var(--moon-dim); font-size: 13px; }
.local-ai-tabs { margin-top: 10px; }
@media (max-width: 600px) { .local-deploy-view { padding: 16px; } .view-header { align-items: center; } :deep(.n-tabs-nav-scroll-wrapper) { overflow-x: auto; } }
</style>
