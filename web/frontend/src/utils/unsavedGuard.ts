/**
 * 未保存编辑态离开提示（审查 C2）。
 *
 * 全仓原先没有任何 beforeRouteLeave / beforeunload / isDirty 守卫：设置页与模型
 * 选择页的编辑态（改密码弹窗里的输入、待添加的命令、待保存的 Key）一旦点侧栏、
 * 刷新或关标签就静默丢失。这里把"脏不脏"和"要不要拦"做成纯函数，
 * composables/useUnsavedGuard.ts 负责挂到路由与 window 上。
 */

export type LeaveDecision = 'leave' | 'ask'

/** 快照比较：结构化数据按 JSON 归一后对比（无第三方 deep-equal 依赖） */
export function hasUnsavedChanges(snapshot: unknown, current: unknown): boolean {
  return stableStringify(snapshot) !== stableStringify(current)
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'null'
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(',')}]`
  const entries = Object.keys(value as Record<string, unknown>)
    .sort()
    .map(k => `${JSON.stringify(k)}:${stableStringify((value as Record<string, unknown>)[k])}`)
  return `{${entries.join(',')}}`
}

/** 脏且用户还没确认时返回 ask（拦住导航），否则放行 */
export function leaveDecision(dirty: boolean, confirmed: boolean): LeaveDecision {
  return dirty && !confirmed ? 'ask' : 'leave'
}
