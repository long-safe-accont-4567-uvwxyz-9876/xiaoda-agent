<script setup lang="ts">
import { computed, ref } from 'vue'
import { NButton, NEmpty, NInput, NSelect, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'
import ModelDetailDrawer from './ModelDetailDrawer.vue'
import StoragePickerDialog from './StoragePickerDialog.vue'

const store = useLocalAiStore()
const message = useMessage()
type CatalogEntry = typeof store.catalog[number]
const query = ref('')
const purpose = ref<string | null>(null)
const selected = ref<CatalogEntry | null>(null)
const destination = ref('')
const showStorage = ref(false)
const showDetail = ref(false)
const filtered = computed(() => store.catalog.filter(model => (!purpose.value || model.purpose === purpose.value) && `${model.id} ${model.repository}`.toLowerCase().includes(query.value.toLowerCase())))
const purposeOptions = [{ label: '对话', value: 'chat' }, { label: '向量', value: 'embedding' }, { label: '重排', value: 'reranker' }]

async function choose(model: CatalogEntry) {
  selected.value = model
  destination.value = ''
  showDetail.value = false
  if (store.defaultStorage) {
    try {
      const validation = await store.validateStorage(store.defaultStorage, model.download_size)
      if (validation.writable && !validation.error) {
        destination.value = validation.path
        showDetail.value = true
        return
      }
      message.warning(validation.error || validation.reason || '默认目录不可用，请重新选择')
    } catch (error) {
      message.warning(error instanceof Error ? error.message : String(error))
    }
  }
  showStorage.value = true
}

function selectStorage(path: string) {
  destination.value = path
  showStorage.value = false
  showDetail.value = true
}
</script>

<template>
  <div>
    <div class="market-toolbar"><n-input v-model:value="query" clearable placeholder="搜索模型或仓库" /><n-select v-model:value="purpose" clearable :options="purposeOptions" placeholder="全部用途" /></div>
    <div class="local-ai-grid">
      <article v-for="model in filtered" :key="model.id" class="glass-panel market-card">
        <div class="resource-head"><div><strong>{{ model.id }}</strong><span>{{ model.repository }}</span></div><n-tag>{{ model.purpose }}</n-tag></div>
        <div class="model-facts"><span>{{ model.quantization || '未标注量化' }}</span><span>{{ (model.download_size / 1024 / 1024).toFixed(1) }} MB</span></div>
        <n-button type="primary" @click="choose(model)">查看并下载</n-button>
      </article>
      <n-empty v-if="!filtered.length" description="没有匹配的市场模型" />
    </div>
    <StoragePickerDialog :show="showStorage" :required-bytes="selected?.download_size" @select="selectStorage" @cancel="showStorage = false" />
    <ModelDetailDrawer :show="showDetail" :model="selected" :destination="destination" @close="showDetail = false" @downloaded="showDetail = false" />
  </div>
</template>

<style scoped>
.market-toolbar { display: grid; grid-template-columns: minmax(220px, 1fr) 180px; gap: 12px; margin-bottom: 16px; }
.local-ai-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.market-card { display: grid; gap: 16px; padding: 16px; border-radius: 14px; }
.resource-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.resource-head strong, .resource-head span { display: block; }
.resource-head span, .model-facts { margin-top: 5px; color: var(--moon-dim); font-size: 12px; }
.model-facts { display: flex; justify-content: space-between; }
@media (max-width: 560px) { .market-toolbar { grid-template-columns: 1fr; } }
</style>
