/**
 * 未保存编辑态离开守卫的 Vue 接线（审查 C2）。
 * 判定逻辑在 utils/unsavedGuard.ts（纯函数、可单测），这里只负责挂路由与
 * beforeunload 两个入口。
 */
import { onBeforeUnmount, onMounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { leaveDecision } from '../utils/unsavedGuard'

export interface UnsavedGuardOptions {
  /** 当前是否存在未保存编辑 */
  isDirty: () => boolean
  /** 询问用户：true 表示放弃修改继续离开 */
  confirmLeave: () => Promise<boolean>
}

export function useUnsavedGuard(options: UnsavedGuardOptions): void {
  onBeforeRouteLeave(async () => {
    if (leaveDecision(options.isDirty(), false) === 'leave') return true
    let confirmed = false
    try {
      confirmed = await options.confirmLeave()
    } catch {
      confirmed = false           // 确认框自己挂了也不能把用户困在页面里
    }
    return leaveDecision(options.isDirty(), confirmed) === 'leave'
  })

  // 刷新/关标签由浏览器原生对话框兜底（文案由浏览器决定，不能自定义）
  const onBeforeUnload = (e: BeforeUnloadEvent): void => {
    if (!options.isDirty()) return
    e.preventDefault()
    e.returnValue = ''
  }

  onMounted(() => window.addEventListener('beforeunload', onBeforeUnload))
  onBeforeUnmount(() => window.removeEventListener('beforeunload', onBeforeUnload))
}
