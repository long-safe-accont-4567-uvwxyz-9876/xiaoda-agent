<script setup lang="ts">
import { NAlert, NCard, NGrid, NGridItem, NTag } from 'naive-ui'
import type { CapabilityReport } from '../../api/providers'

defineProps<{ report: CapabilityReport | null }>()

const capabilities = [
  ['tools', '工具调用'],
  ['vision', '视觉输入'],
  ['streaming', '流式输出'],
  ['model_discovery', '模型发现'],
  ['json_mode', 'JSON 模式'],
] as const
</script>

<template>
  <n-alert v-if="!report" type="info">测试后显示能力结果</n-alert>
  <n-alert v-else-if="!report.available" type="error">{{ report.error || 'Provider 不可用' }}</n-alert>
  <n-card v-else size="small" title="能力检测结果">
    <n-grid cols="1 s:2 m:3" responsive="screen" :x-gap="10" :y-gap="10">
      <n-grid-item v-for="([field, label]) in capabilities" :key="field">
        <div class="capability-item">
          <span>{{ label }}</span>
          <n-tag :type="report.capabilities[field] ? 'success' : 'default'">
            {{ report.capabilities[field] ? '支持' : '不支持' }}
          </n-tag>
        </div>
      </n-grid-item>
    </n-grid>
    <div v-if="report.models.length" class="models">
      <n-tag v-for="model in report.models" :key="model" size="small">{{ model }}</n-tag>
    </div>
  </n-card>
</template>

<style scoped>
.capability-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.models { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
</style>
