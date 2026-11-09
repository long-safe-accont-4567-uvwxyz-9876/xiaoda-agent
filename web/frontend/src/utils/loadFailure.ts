/**
 * 加载失败的统一出口（动作级空 catch 收敛，2026-11-09）。
 *
 * 判据：失败会让界面看起来像“这里本来就没有数据”的加载动作，都走这里，
 * 让用户能看到具体原因；纯清理/埋点/缩略图刷新这类失败不影响主流程的，
 * 用带 `静默兜底：` 注释的空 catch（见 scripts/check-silent-catch.mjs）。
 */
import { describeError, isCancelError } from './errors'
import { t } from '../i18n'

export function reportLoadError(
  error: unknown,
  scope: string,
  notify: (text: string) => void,
): void {
  // 取消是正常路径（切页/切会话），不当成失败上报
  if (isCancelError(error)) return
  // 带 scope 打到控制台，便于定位是哪个加载点失败；提示文案不带内部作用域名
  console.warn(`[load-failure] ${scope}`, error)
  notify(describeError(error, t('errors.loadFailed')))
}
