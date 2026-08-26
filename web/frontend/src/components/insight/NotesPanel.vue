<script setup lang="ts">
/**
 * 笔记 Tab：归档式笔记列表（确认文案为"归档"语义，原样保留）。
 */
import { NButton, NPopconfirm, NTag } from 'naive-ui'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import type { NoteItem } from './types'

defineProps<{
  notes: NoteItem[]
}>()

const emit = defineEmits<{
  (e: 'add'): void
  (e: 'edit', item: NoteItem): void
  (e: 'remove', id: number): void
}>()
</script>

<template>
  <div>
    <div class="tab-toolbar glass-panel">
      <n-button size="small" type="primary" @click="emit('add')">+ {{ t('insightView.addNote') }}</n-button>
    </div>
    <div class="item-list">
      <Tilt3D v-for="n in notes" :key="n.id"><div class="list-row glass-panel">
        <n-tag size="tiny" :bordered="false">{{ n.kind }}</n-tag>
        <span class="note-content">{{ n.content }}</span>
        <n-button size="tiny" quaternary @click="emit('edit', n)">{{ t('insightView.edit') }}</n-button>
        <n-popconfirm @positive-click="emit('remove', n.id)">
          <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
          {{ t('insightView.archiveNoteConfirm') }}
        </n-popconfirm>
      </div></Tilt3D>
    </div>
    <div v-if="!notes.length" class="empty-state"><p>{{ t('insightView.noNotes') }}</p></div>
  </div>
</template>

<style scoped>
.tab-toolbar { display: flex; align-items: center; gap: 10px; padding: 10px 14px; margin-bottom: 10px; }
.item-list { display: flex; flex-direction: column; gap: 6px; }
.list-row { display: flex; align-items: center; gap: 10px; padding: 8px 14px; font-size: 13px; }
.note-content { flex: 1; }

.empty-state { padding: 30px; text-align: center; color: var(--moon-dim); }
</style>
