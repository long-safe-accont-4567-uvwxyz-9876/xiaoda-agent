<script setup lang="ts">
/**
 * 学习记录 Tab：优先级标签着色 + 出现次数 + 列表。
 */
import { NButton, NPopconfirm, NTag } from 'naive-ui'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import type { LearningItem } from './types'

defineProps<{
  learnings: LearningItem[]
}>()

const emit = defineEmits<{
  (e: 'add'): void
  (e: 'edit', item: LearningItem): void
  (e: 'remove', id: number): void
}>()
</script>

<template>
  <div>
    <div class="tab-toolbar glass-panel">
      <n-button size="small" type="primary" @click="emit('add')">+ {{ t('insightView.addLearning') }}</n-button>
    </div>
    <div class="item-list">
      <Tilt3D v-for="l in learnings" :key="l.id"><div class="list-row glass-panel">
        <n-tag size="tiny" :type="l.priority === 'high' ? 'error' : l.priority === 'medium' ? 'warning' : 'default'"
               :bordered="false">{{ l.priority }}</n-tag>
        <span class="note-content">{{ l.summary }}</span>
        <span class="note-extra">× {{ l.recurrence_count }}</span>
        <n-button size="tiny" quaternary @click="emit('edit', l)">{{ t('insightView.edit') }}</n-button>
        <n-popconfirm @positive-click="emit('remove', l.id)">
          <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
          {{ t('insightView.deleteLearningConfirm') }}
        </n-popconfirm>
      </div></Tilt3D>
    </div>
    <div v-if="!learnings.length" class="empty-state"><p>{{ t('insightView.noLearning') }}</p></div>
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
