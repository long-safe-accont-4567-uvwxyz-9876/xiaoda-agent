/**
 * 命令确认卡的超时/倒计时（审查 G2）。
 *
 * 后端 tool_engine/tool_executor.py 的 CMD_CONFIRM_TIMEOUT=60s 到期即按失败返回，
 * 而前端卡片只是"收到 WS 就渲染"：刷新页面卡片消失、没有任何倒计时，
 * 用户既不知道还剩多久，也没有恢复入口。这里给出与后端一致的截止时间、
 * 倒计时文案与"过期"判定，供卡片与恢复逻辑共用。
 */

/** 与 tool_executor.CMD_CONFIRM_TIMEOUT 对齐（秒） */
export const CMD_CONFIRM_TIMEOUT_MS = 60_000
/** 剩余时间低于该阈值时进入"即将超时"提醒态 */
export const CMD_CONFIRM_WARN_MS = 10_000

export type ConfirmPhase = 'pending' | 'expiring' | 'expired'

/** 剩余毫秒（永不为负）；deadline 缺失按"还没开始计时"处理 */
export function confirmRemainingMs(deadline: number, now: number): number {
  if (!Number.isFinite(deadline) || deadline <= 0) return CMD_CONFIRM_TIMEOUT_MS
  return Math.max(0, deadline - now)
}

/** 0:59 / 0:07 形式的倒计时（秒向上取整，避免显示 0:00 却还没超时） */
export function formatCountdown(remainingMs: number): string {
  const total = Math.max(0, Math.ceil(remainingMs / 1000))
  const mm = Math.floor(total / 60)
  const ss = total % 60
  return `${mm}:${String(ss).padStart(2, '0')}`
}

export function confirmPhase(remainingMs: number, warnMs: number = CMD_CONFIRM_WARN_MS): ConfirmPhase {
  if (remainingMs <= 0) return 'expired'
  if (remainingMs <= warnMs) return 'expiring'
  return 'pending'
}

/** 恢复刷新前的待确认请求：已过期的直接丢掉（后端那边也早已超时） */
export function pruneExpired<T extends { deadline: number }>(items: T[], now: number): T[] {
  return items.filter(item => item.deadline > now)
}
