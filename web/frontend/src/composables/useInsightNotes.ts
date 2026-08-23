/**
 * 笔记列表组合式函数 —— 从 InsightView 抽出（2026-08-23 大文件拆分专项）。
 * 原 loadNotes 静默吞错（catch 空块），行为保持不变。
 */
import { ref } from 'vue'
import { get, deleteNote } from '../api'
import { useMessage } from 'naive-ui'
import { t } from '../i18n'
import type { NoteItem } from '../components/insight/types'

export function useInsightNotes() {
  const message = useMessage()
  const notes = ref<NoteItem[]>([])

  async function loadNotes() {
    try { notes.value = await get<NoteItem[]>('/insight/notebook') } catch { /* */ }
  }

  async function removeNote(id: number) {
    try {
      await deleteNote(id)
      notes.value = notes.value.filter(n => n.id !== id)
      message.success(t('insightView.noteArchived'))
    } catch (e: any) { message.error(e.message) }
  }

  return { notes, loadNotes, removeNote }
}
