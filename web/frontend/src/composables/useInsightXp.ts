/**
 * XP 亲密度组合式函数 —— 从 InsightView 抽出（2026-08-23 大文件拆分专项）。
 * 状态加载 + 升级事件处理（含 toast 自动关闭定时器）；
 * 展示辅助（来源文案/图标/时间格式化）归 XpPanel。
 */
import { computed, onScopeDispose, ref } from 'vue'
import { getXpState, getXpLevels, type XpState, type XpLevelConfig } from '../api'

export interface XpLevelUpState {
  show: boolean
  level: number
  label: string
}

export function useInsightXp() {
  const xpState = ref<XpState>({} as XpState)
  const xpLevels = ref<XpLevelConfig[]>([])
  const xpLevelUp = ref<XpLevelUpState>({ show: false, level: 0, label: '' })
  let levelUpTimer: ReturnType<typeof setTimeout> | null = null

  async function loadXpData() {
    try {
      const [state, levelsResp] = await Promise.all([getXpState(), getXpLevels()])
      xpState.value = state
      xpLevels.value = levelsResp.levels || []
    } catch (e) {
      console.warn('[XP] 加载失败:', e)
    }
  }

  function onXpLevelUp(e: { level?: number; level_label?: string }) {
    xpLevelUp.value = { show: true, level: e.level || 0, label: e.level_label || '' }
    loadXpData()
    if (levelUpTimer) clearTimeout(levelUpTimer)
    levelUpTimer = setTimeout(() => { xpLevelUp.value.show = false }, 5000)
  }

  const nextLevelLabel = computed(() => {
    const next = (xpState.value?.level || 1) + 1
    const found = xpLevels.value?.find(l => l.level === next)
    return found?.label || `LV${next}`
  })

  onScopeDispose(() => {
    if (levelUpTimer) clearTimeout(levelUpTimer)
  })

  return { xpState, xpLevels, xpLevelUp, loadXpData, onXpLevelUp, nextLevelLabel }
}
