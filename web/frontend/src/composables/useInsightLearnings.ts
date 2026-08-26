/**
 * 学习记录 + 本能组合式函数 —— 从 InsightView 抽出（2026-08-23 大文件拆分专项）。
 * 原实现两表串行加载、静默吞错，行为保持不变。
 */
import { ref } from 'vue'
import { get, deleteLearning, deleteInstinct } from '../api'
import { useMessage } from 'naive-ui'
import { t } from '../i18n'
import type { InstinctItem, LearningItem } from '../components/insight/types'

export function useInsightLearnings() {
  const message = useMessage()
  const learnings = ref<LearningItem[]>([])
  const instincts = ref<InstinctItem[]>([])

  async function loadLearning() {
    try {
      learnings.value = await get<LearningItem[]>('/insight/learnings')
      instincts.value = await get<InstinctItem[]>('/insight/instincts')
    } catch { /* */ }
  }

  async function removeLearning(id: number) {
    try {
      await deleteLearning(id)
      learnings.value = learnings.value.filter(l => l.id !== id)
      message.success(t('insightView.learningDeleted'))
    } catch (e: any) { message.error(e.message) }
  }

  async function removeInstinct(id: number) {
    try {
      await deleteInstinct(id)
      instincts.value = instincts.value.filter(i => i.id !== id)
      message.success(t('insightView.instinctDeleted'))
    } catch (e: any) { message.error(e.message) }
  }

  return { learnings, instincts, loadLearning, removeLearning, removeInstinct }
}
