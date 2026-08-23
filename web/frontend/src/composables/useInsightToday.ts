/**
 * 今日事件组合式函数 —— 从 InsightView 抽出（2026-08-23 大文件拆分专项）。
 * 只负责数据；时间格式化/图标映射等纯展示逻辑在 TodayPanel。
 */
import { ref } from 'vue'
import { get } from '../api'
import { useMessage } from 'naive-ui'
import type { TodayItem, TodayStats } from '../components/insight/types'

export interface TodayData {
  items: TodayItem[]
  stats: TodayStats
}

export function useInsightToday() {
  const message = useMessage()
  const todayData = ref<TodayData>({ items: [], stats: {} })

  async function loadToday() {
    try { todayData.value = await get<TodayData>('/insight/today') } catch (e: any) { message.error(e.message) }
  }

  return { todayData, loadToday }
}
