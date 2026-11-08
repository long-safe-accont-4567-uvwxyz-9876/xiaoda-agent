/**
 * 错误文案收敛（审查 C1）。
 *
 * 抽样统计 src 下 90 处 catch 既不设 error ref 也不弹提示；数据拉取类的静默
 * 失败会让图表空着、页面不解释原因，用户与运维都无法区分"空数据"与"请求失败"。
 * 这里提供统一的取文案方式，供 view 级 error ref 使用。
 */

/** 把任意抛出物转成可展示的一句话；空异常不返回空串 */
export function describeError(error: unknown, fallback = ''): string {
  if (error instanceof Error && error.message) return error.message
  if (typeof error === 'string' && error.trim()) return error
  if (error && typeof error === 'object') {
    const message = (error as { message?: unknown }).message
    if (typeof message === 'string' && message.trim()) return message
    const code = (error as { code?: unknown }).code
    if (typeof code === 'string' && code.trim()) return code
  }
  return fallback
}

/** 判断是否为请求取消（AbortError）：取消是正常路径，不该弹错误 */
export function isCancelError(error: unknown): boolean {
  return !!error && typeof error === 'object' && (error as { name?: unknown }).name === 'AbortError'
}
