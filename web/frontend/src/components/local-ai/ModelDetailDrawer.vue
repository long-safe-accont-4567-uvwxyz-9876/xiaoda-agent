<script setup lang="ts">
import { computed, ref } from 'vue'
import { NButton, NDescriptions, NDescriptionsItem, NDrawer, NDrawerContent, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'

type CatalogEntry = ReturnType<typeof useLocalAiStore>['catalog'][number]

const props = defineProps<{ show: boolean; model: CatalogEntry | null; destination: string }>()
const emit = defineEmits<{ close: []; downloaded: [] }>()
const store = useLocalAiStore()
const message = useMessage()
const submitting = ref(false)
const size = computed(() => props.model ? `${(props.model.download_size / 1024 / 1024).toFixed(1)} MB` : '—')

async function download() {
  if (!props.model || !props.destination) return
  submitting.value = true
  try {
    await store.download({ model_id: props.model.id, destination: props.destination, request_id: store.createRequestId() })
    message.success('下载任务已创建')
    emit('downloaded')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <n-drawer :show="show" :width="480" @update:show="value => !value && emit('close')">
    <n-drawer-content v-if="model" title="模型详情" closable>
      <div class="detail-heading">
        <div><strong>{{ model.id }}</strong><span>{{ model.repository }}</span></div>
        <n-tag type="success">{{ model.purpose }}</n-tag>
      </div>
      <n-descriptions label-placement="left" :column="1" bordered>
        <n-descriptions-item label="版本">{{ model.revision }}</n-descriptions-item>
        <n-descriptions-item label="量化">{{ model.quantization || '未标注' }}</n-descriptions-item>
        <n-descriptions-item label="下载大小">{{ size }}</n-descriptions-item>
        <n-descriptions-item label="许可证">{{ model.license || '未标注' }}</n-descriptions-item>
        <n-descriptions-item label="存储目录">{{ destination }}</n-descriptions-item>
      </n-descriptions>
      <template #footer>
        <n-button type="primary" block :loading="submitting" @click="download">下载并安装</n-button>
      </template>
    </n-drawer-content>
  </n-drawer>
</template>

<style scoped>
.detail-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
.detail-heading strong, .detail-heading span { display: block; }
.detail-heading span { margin-top: 4px; color: var(--moon-dim); font-size: 12px; }
</style>
