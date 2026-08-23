/**
 * 记忆检索组合式函数 —— 从 InsightView 抽出（2026-08-23 大文件拆分专项）。
 * 搜索条件状态 + 列表加载 + 删除；UI 归 MemoryPanel。
 */
import { ref } from 'vue'
import { get, deleteMemory } from '../api'
import { useMessage } from 'naive-ui'
import { t } from '../i18n'
import type { MemoryItem } from '../components/insight/types'

export function useInsightMemories() {
  const message = useMessage()
  const memories = ref<MemoryItem[]>([])
  const memQuery = ref('')
  const importanceMin = ref(0)

  async function loadMemories() {
    try {
      memories.value = await get<MemoryItem[]>(
        `/insight/memories?q=${encodeURIComponent(memQuery.value)}&importance_min=${importanceMin.value}`)
    } catch (e: any) { message.error(e.message) }
  }

  async function removeMemory(id: number) {
    try {
      await deleteMemory(id)
      memories.value = memories.value.filter(m => m.id !== id)
      message.success(t('insightView.memoryDeleted'))
    } catch (e: any) { message.error(e.message) }
  }

  return { memories, memQuery, importanceMin, loadMemories, removeMemory }
}
