<script setup lang="ts">
/**
 * 本能 Tab：置信度百分比 + 列表（content/summary/trigger_pattern 兜底链原样保留）。
 */
import { NButton, NPopconfirm } from 'naive-ui'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import type { InstinctItem } from './types'

defineProps<{
  instincts: InstinctItem[]
}>()

const emit = defineEmits<{
  (e: 'add'): void
  (e: 'edit', item: InstinctItem): void
  (e: 'remove', id: number): void
}>()
</script>

<template>
  <div>
    <div class="tab-toolbar glass-panel">
      <n-button size="small" type="primary" @click="emit('add')">+ {{ t('insightView.addInstinct') }}</n-button>
    </div>
    <div class="item-list">
      <Tilt3D v-for="ins in instincts" :key="ins.id"><div class="list-row glass-panel">
        <span class="note-content">{{ ins.content || ins.summary || ins.trigger_pattern }}</span>
        <span class="note-extra">{{ t('insightView.confidence') }} {{ ((ins.confidence || 0) * 100).toFixed(0) }}%</span>
        <n-button size="tiny" quaternary @click="emit('edit', ins)">{{ t('insightView.edit') }}</n-button>
        <n-popconfirm @positive-click="emit('remove', ins.id)">
          <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
          {{ t('insightView.deleteInstinctConfirm') }}
        </n-popconfirm>
      </div></Tilt3D>
    </div>
    <div v-if="!instincts.length" class="empty-state"><p>{{ t('insightView.noInstinct') }}</p></div>
  </div>
</template>

<style scoped>
.tab-toolbar { display: flex; align-items: center; gap: 10px; padding: 10px 14px; margin-bottom: 10px; }
.item-list { display: flex; flex-direction: column; gap: 6px; }
.list-row { display: flex; align-items: center; gap: 10px; padding: 8px 14px; font-size: 13px; }
.note-content { flex: 1; }
.note-extra { font-size: 11px; color: var(--moon-dim); }

.empty-state { padding: 30px; text-align: center; color: var(--moon-dim); }
</style>
