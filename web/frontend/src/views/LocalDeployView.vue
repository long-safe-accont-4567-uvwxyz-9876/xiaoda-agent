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

const store = useLocalAiStore()
const message = useMessage()
const activeTab = ref('deployments')
const summary = computed(() => `${store.instances.filter(item => item.state !== 'stopped' && item.state !== 'failed').length} 个运行实例 · ${store.models.length} 个已安装模型 · ${store.devices.filter(item => item.state === 'available').length} 个可用设备 · ${store.downloads.filter(item => ['pending', 'downloading', 'paused'].includes(item.state)).length} 个下载任务`)

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
    <header class="view-header"><div><h2>🖥️ 本地部署</h2><p>{{ summary }}</p></div><n-button :loading="store.loading" @click="load">刷新</n-button></header>
    <n-alert v-if="store.error" type="error" title="本地 AI 资源加载失败">{{ store.error }}</n-alert>
    <n-spin :show="store.loading && !store.devices.length && !store.catalog.length">
      <n-tabs v-model:value="activeTab" type="line" animated display-directive="show" class="local-ai-tabs">
        <n-tab-pane name="deployments" tab="部署"><DeploymentsTab /></n-tab-pane>
        <n-tab-pane name="market" tab="模型广场"><ModelMarketTab /></n-tab-pane>
        <n-tab-pane name="installed" tab="已安装"><InstalledModelsTab /></n-tab-pane>
        <n-tab-pane name="devices" tab="算力设备"><ComputeDevicesTab /></n-tab-pane>
        <n-tab-pane name="nodes" tab="功能节点"><SystemModelNodesTab /></n-tab-pane>
        <n-tab-pane name="downloads" tab="下载任务"><DownloadTasksTab /></n-tab-pane>
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
