/**
 * 画像组合式函数 —— 从 InsightView 抽出（2026-08-23 大文件拆分专项）。
 * 加载 + 手动整合触发 + WS 整合完成回调（onConsolidated 由视图注册到 WS）。
 */
import { ref } from 'vue'
import { get, post } from '../api'
import { useMessage } from 'naive-ui'
import { t } from '../i18n'
import type { PortraitData, PortraitHistoryEntry } from '../components/insight/types'

export function useInsightPortrait() {
  const message = useMessage()
  const portrait = ref<PortraitData>({})
  const portraitHistory = ref<PortraitHistoryEntry[]>([])
  const consolidating = ref(false)

  async function loadPortrait() {
    try {
      const data = await get<{
        portrait: PortraitData
        history: PortraitHistoryEntry[]
      }>('/insight/portrait')
      portrait.value = data.portrait || {}
      portraitHistory.value = data.history || []
    } catch (e: any) { message.error(e.message) }
  }

  async function consolidate() {
    consolidating.value = true
    try {
      await post('/insight/portrait/consolidate')
      message.info(t('insightView.consolidateStarted'))
    } catch (e: any) {
      consolidating.value = false
      message.error(e.message)
    }
  }

  function onConsolidated(e: { ok?: boolean; error?: string }) {
    consolidating.value = false
    if (e.ok) {
      message.success(t('insightView.consolidateDone'))
      loadPortrait()
    } else {
      message.error(`${t('insightView.consolidateFailed')}: ${e.error || t('insightView.unknownError')}`)
    }
  }

  return { portrait, portraitHistory, consolidating, loadPortrait, consolidate, onConsolidated }
}
