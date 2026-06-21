<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { NPopover, NTag, NSpin, useMessage } from 'naive-ui'
import { get, post } from '../../api'

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

const message = useMessage()

const currentProvider = ref('')
const currentModelId = ref('')
const providers = ref<ProviderGroup[]>([])
const loading = ref(false)
const showPopover = ref(false)

const currentDisplayName = computed(() => {
  for (const pg of providers.value) {
    for (const m of pg.models) {
      if (pg.provider === currentProvider.value && m.id === currentModelId.value) {
        return m.display_name
      }
    }
  }
  return currentModelId.value || '选择模型'
})

function isCurrent(provider: string, modelId: string): boolean {
  return provider === currentProvider.value && modelId === currentModelId.value
}

async function selectModel(provider: string, model: ModelInfo) {
  if (isCurrent(provider, model.id)) {
    showPopover.value = false
    return
  }
  try {
    await post('/models/chat-model', { provider, model_id: model.id })
    currentProvider.value = provider
    currentModelId.value = model.id
    showPopover.value = false
    emit('change', provider, model.id)
    if (!model.tool_calling) {
      message.warning('该模型不支持工具调用，部分功能可能受限')
    }
  } catch (e: any) {
    message.error(e.message || '切换模型失败')
  }
}

async function fetchCurrentModel() {
  try {
    const data = await get<{ provider: string; model_id: string }>('/models/chat-model')
    currentProvider.value = data.provider
    currentModelId.value = data.model_id
  } catch { /* 静默 */ }
}

async function fetchModels() {
  loading.value = true
  try {
    const data = await get<ProviderGroup[]>('/models/discover')
    providers.value = data
  } catch { /* 静默 */ }
  loading.value = false
}

onMounted(async () => {
  await Promise.all([fetchCurrentModel(), fetchModels()])
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
        <span class="model-name">{{ currentDisplayName }}</span>
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
  max-width: 140px;
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
