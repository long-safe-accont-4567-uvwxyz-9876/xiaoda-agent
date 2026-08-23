/**
 * 情绪数据组合式函数 —— 从 InsightView 抽出（2026-08-23 大文件拆分专项）。
 * 只负责 API 调用与加载态；echarts 渲染归 EmotionPanel 组件。
 */
import { ref } from 'vue'
import { get } from '../api'
import { useMessage } from 'naive-ui'
import { t } from '../i18n'
import type { EmotionCurrent, EmotionHistoryRow } from '../components/insight/types'

export const EMOTION_COLORS: Record<string, string> = {
  '喜悦': '#7fd650', '悲伤': '#60a5fa', '愤怒': '#f87171', '焦虑': '#fbbf24',
  '害羞': '#f9a8d4', '好奇': '#a78bfa', '思考': '#67e8f9', '恐惧': '#94a3b8', '平静': '#9ca3af',
}

export function useInsightEmotion() {
  const message = useMessage()
  const currentEmotion = ref<EmotionCurrent>({})
  const history = ref<EmotionHistoryRow[]>([])

  async function loadEmotion() {
    try {
      currentEmotion.value = await get<EmotionCurrent>('/insight/emotion/current')
      history.value = await get<EmotionHistoryRow[]>('/insight/emotion/history?days=7')
    } catch (e: any) { message.error(e.message) }
  }

  return { currentEmotion, history, loadEmotion }
}
