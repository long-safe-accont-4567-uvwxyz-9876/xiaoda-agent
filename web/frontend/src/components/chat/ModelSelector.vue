<script setup lang="ts">
import { ref, onMounted, watch, onBeforeUnmount } from 'vue'
import { NPopover, NTag, NSpin, useMessage } from 'naive-ui'
import { get, post, api } from '../../api'
import { useChatStore } from '../../stores/chat'
import { useAgentsStore } from '../../stores/agents'
import { getWsClient } from '../../api/ws'

const chat = useChatStore()
const agentsStore = useAgentsStore()
const ws = getWsClient()

interface ModelInfo {
  id: string
  display_name: string
  free: boolean
  tool_calling: boolean
  vision: boolean
}

interface ProviderGroup {
  provider: string
  label?: string
  models: ModelInfo[]
}

interface CurrentModel {
  provider: string
  model_id: string
  label: string
}

const message = useMessage()

const currentModel = ref<CurrentModel>({
  provider: '',
  model_id: '',
  label: '选择模型'
})
const providers = ref<ProviderGroup[]>([])
const loading = ref(false)
const showPopover = ref(false)

function buildModelLabel(provider: string, modelId: string): string {
  if (!provider && !modelId) return '选择模型'
  let providerLabel = provider
  let modelDisplayName = modelId
  for (const pg of providers.value) {
    if (pg.provider === provider) {
      providerLabel = pg.label || pg.provider
      for (const m of pg.models) {
        if (m.id === modelId) {
          modelDisplayName = m.display_name
          break
        }
      }
      break
    }
  }
  return `${providerLabel} / ${modelDisplayName}`
}

function updateCurrentModelLabel() {
  currentModel.value.label = buildModelLabel(currentModel.value.provider, currentModel.value.model_id)
}

function isCurrent(provider: string, modelId: string): boolean {
  return provider === currentModel.value.provider && modelId === currentModel.value.model_id
}

async function selectModel(provider: string, model: ModelInfo) {
  if (isCurrent(provider, model.id)) {
    showPopover.value = false
    return
  }
  try {
    const agent = chat.currentAgent
    if (agent && agent !== 'nahida') {
      // 子 Agent 活跃：更新该子 Agent 的模型配置
      await api.setAgentModel(agent, provider, model.id)
      // 刷新 agentsStore 以同步 Agent 管理卡片
      await agentsStore.load()
    } else {
      // 主体 nahida 活跃：更新 ROUTE_TABLE["chat"]
      await post('/models/chat-model', { provider, model_id: model.id })
    }
    currentModel.value = {
      provider,
      model_id: model.id,
      label: buildModelLabel(provider, model.id)
    }
    showPopover.value = false
    emit('change', provider, model.id)
    if (!model.tool_calling) {
      message.warning('该模型不支持工具调用，部分功能可能受限')
    }
  } catch (e: any) {
    message.error(e.message || '切换模型失败')
  }
}

/** 从 agentsStore 同步当前活跃 Agent 的模型显示 */
function syncCurrentModelFromStore() {
  const agent = chat.currentAgent
  if (!agent) return
  if (agent === 'nahida') {
    // 主体：从后端 API 获取
    fetchCurrentModel()
    return
  }
  // 子 Agent：从 agentsStore 读取
  const info = agentsStore.agents.find(a => a.name === agent)
  if (info && info.provider && info.model) {
    currentModel.value = {
      provider: info.provider,
      model_id: info.model,
      label: buildModelLabel(info.provider, info.model)
    }
  }
}

async function fetchCurrentModel() {
  try {
    const data = await get<{ provider: string; model_id: string }>('/models/chat-model')
    currentModel.value = {
      provider: data.provider,
      model_id: data.model_id,
      label: buildModelLabel(data.provider, data.model_id)
    }
  } catch { /* 静默 */ }
}

async function fetchModels() {
  loading.value = true
  try {
    const data = await get<ProviderGroup[]>('/models/discover')
    providers.value = data
    // providers 加载完成后，重新计算当前模型标签（处理异步加载顺序）
    updateCurrentModelLabel()
  } catch { /* 静默 */ }
  loading.value = false
}

/** WS 事件处理：Provider 排序/增删后刷新模型列表 */
function onConfigChanged(e: any) {
  if (e.domain === 'models') {
    fetchModels()
  }
}

onMounted(async () => {
  await agentsStore.load()
  await Promise.all([fetchCurrentModel(), fetchModels()])
  ws.on('config_changed', onConfigChanged)
})

onBeforeUnmount(() => {
  ws.off('config_changed', onConfigChanged)
})

// 监听 Agent 切换，同步当前模型显示
watch(() => chat.currentAgent, () => {
  syncCurrentModelFromStore()
})

const emit = defineEmits<{
  change: [provider: string, modelId: string]
}>()
</script>

<template>
  <NPopover
    v-model:show="showPopover"
    trigger="click"
    placement="bottom-start"
    :show-arrow="false"
    raw
    :style="{ padding: 0 }"
  >
    <template #trigger>
      <button class="model-chip">
        <span class="model-name">{{ currentModel.label }}</span>
        <span class="arrow-icon" :class="{ open: showPopover }">▾</span>
      </button>
    </template>

    <div class="model-panel glass-panel">
      <NSpin :show="loading" size="small">
        <div class="panel-scroll">
          <div v-for="pg in providers" :key="pg.provider" class="provider-group">
            <div class="provider-label">{{ pg.label || pg.provider }}</div>
            <div
              v-for="m in pg.models"
              :key="m.id"
              class="model-row"
              :class="{ current: isCurrent(pg.provider, m.id) }"
              @click="selectModel(pg.provider, m)"
            >
              <span class="model-display-name">{{ m.display_name }}</span>
              <span class="model-id">{{ m.id }}</span>
              <div class="model-badges">
                <span v-if="m.tool_calling" class="badge tool" title="支持工具调用">🔧</span>
                <span v-if="m.vision" class="badge vision" title="支持视觉">👁</span>
                <NTag v-if="m.free" type="success" size="small" class="free-tag">免费</NTag>
              </div>
            </div>
          </div>
          <div v-if="!loading && !providers.length" class="empty-hint">暂无可用模型</div>
        </div>
      </NSpin>
    </div>
  </NPopover>
</template>

<style scoped>
.model-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px 3px 12px;
  border-radius: 14px;
  border: 1px solid var(--glass-border);
  background: var(--glass-bg);
  backdrop-filter: blur(12px);
  color: var(--moon);
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s, box-shadow 0.2s;
  line-height: 1;
  white-space: nowrap;
}

.model-chip:hover {
  border-color: var(--dendro);
  background: rgba(127, 214, 80, 0.06);
  box-shadow: 0 0 0 1px rgba(127, 214, 80, 0.15);
}

.model-name {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.arrow-icon {
  font-size: 10px;
  color: var(--moon-dim);
  transition: transform 0.2s;
}

.arrow-icon.open {
  transform: rotate(180deg);
}

.model-panel {
  border-radius: 12px;
  border: 1px solid var(--glass-border);
  background: var(--glass-bg);
  backdrop-filter: blur(16px);
  min-width: 280px;
  max-width: 360px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
  overflow: hidden;
}

.panel-scroll {
  max-height: 380px;
  overflow-y: auto;
  padding: 6px 0;
}

.panel-scroll::-webkit-scrollbar {
  width: 4px;
}

.panel-scroll::-webkit-scrollbar-thumb {
  background: rgba(127, 214, 80, 0.2);
  border-radius: 2px;
}

.provider-group {
  margin-bottom: 4px;
}

.provider-group:last-child {
  margin-bottom: 0;
}

.provider-label {
  padding: 6px 14px 2px;
  font-size: 11px;
  font-weight: 600;
  color: var(--wisdom);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.model-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 14px;
  cursor: pointer;
  transition: background 0.15s;
  position: relative;
}

.model-row:hover {
  background: rgba(127, 214, 80, 0.06);
}

.model-row.current {
  background: rgba(127, 214, 80, 0.12);
}

.model-row.current::before {
  content: '';
  position: absolute;
  left: 0;
  top: 4px;
  bottom: 4px;
  width: 3px;
  border-radius: 0 2px 2px 0;
  background: var(--dendro);
}

.model-display-name {
  font-size: 13px;
  color: var(--moon);
  white-space: nowrap;
  flex-shrink: 0;
}

.model-id {
  font-size: 10px;
  color: var(--moon-dim);
  font-family: 'JetBrains Mono', monospace;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}

.model-badges {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}

.badge {
  font-size: 12px;
  line-height: 1;
}

.free-tag {
  font-size: 10px;
  line-height: 1;
}

.empty-hint {
  padding: 20px 14px;
  text-align: center;
  color: var(--moon-dim);
  font-size: 13px;
}
</style>
