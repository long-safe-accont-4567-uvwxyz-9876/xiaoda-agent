<script setup lang="ts">
/**
 * 记忆检索 Tab：搜索工具栏 + 记忆列表。列表数据经 props 注入，
 * 搜索/增删改通过 emits 上抛（useInsightMemories 在视图层持有状态）。
 */
import { NButton, NInput, NPopconfirm, NSlider, NTag } from 'naive-ui'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import type { MemoryItem } from './types'

defineProps<{
  memories: MemoryItem[]
  query: string
  importanceMin: number
}>()

const emit = defineEmits<{
  (e: 'update:query', v: string): void
  (e: 'update:importanceMin', v: number): void
  (e: 'search'): void
  (e: 'add'): void
  (e: 'edit', item: MemoryItem): void
  (e: 'remove', id: number): void
}>()
</script>

<template>
  <div>
    <div class="mem-toolbar glass-panel">
      <n-button size="small" type="primary" @click="emit('add')">+ {{ t('insightView.add') }}</n-button>
      <n-input :value="query" :placeholder="t('insightView.searchMemoryPh')" clearable
               style="max-width: 280px" @update:value="emit('update:query', $event)"
               @keydown.enter="emit('search')" />
      <label class="slider-label">
        {{ t('insightView.importanceMin') }} ≥ {{ importanceMin.toFixed(1) }}
        <n-slider :value="importanceMin" :min="0" :max="1" :step="0.1"
                  style="width: 140px"
                  @update:value="(v: number) => { emit('update:importanceMin', v); emit('search') }" />
      </label>
      <n-button size="small" @click="emit('search')">{{ t('insightView.searchBtn') }}</n-button>
    </div>
    <div class="mem-list">
      <Tilt3D v-for="m in memories" :key="m.id"><div class="mem-row glass-panel">
        <div class="mem-main">
          <span class="mem-summary">{{ m.summary }}</span>
          <div class="mem-meta">
            <span>{{ '★'.repeat(Math.round((m.importance || 0) * 5)) || '☆' }}</span>
            <n-tag v-if="m.emotion_label" size="tiny" :bordered="false">{{ m.emotion_label }}</n-tag>
            <span>{{ new Date(m.timestamp * 1000).toLocaleString('zh-CN') }}</span>
            <n-tag v-if="m.via === 'vector'" size="tiny" type="info" :bordered="false">{{ t('insightView.semanticHit') }}</n-tag>
          </div>
        </div>
        <n-button size="tiny" quaternary @click="emit('edit', m)">{{ t('insightView.edit') }}</n-button>
        <n-popconfirm @positive-click="emit('remove', m.id)">
          <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
          {{ t('insightView.deleteMemoryConfirm') }}
        </n-popconfirm>
      </div></Tilt3D>
    </div>
  </div>
</template>

<style scoped>
.mem-toolbar {
  display: flex; align-items: center; gap: 14px;
  padding: 12px 14px; margin-bottom: 12px; flex-wrap: wrap;
}
.slider-label { display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--moon-dim); }

.mem-list { display: flex; flex-direction: column; gap: 8px; }
.mem-row { display: flex; align-items: center; gap: 12px; padding: 10px 14px; }
.mem-main { flex: 1; min-width: 0; }
.mem-summary { font-size: 13.5px; }
.mem-meta {
  display: flex; align-items: center; gap: 8px;
  font-size: 11px; color: var(--wisdom); margin-top: 4px;
}
</style>
