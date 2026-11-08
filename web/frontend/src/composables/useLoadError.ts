/**
 * view 级加载错误态（审查 C1 / G3）。
 *
 * 数据拉取失败原先大量走空的 catch 块，页面直接渲染空态：用户与运维都
 * 分不清"确实没有数据"和"请求失败"。这里给每个视图一个 error ref + 统一的
 * 取文案方式，模板里配合 LoadErrorBar 提供重试入口。
 */
import { ref } from 'vue'
import type { Ref } from 'vue'
import { describeError, isCancelError } from '../utils/errors'
import { t } from '../i18n'

export interface LoadErrorHandle {
  loadError: Ref<string>
  fail: (error: unknown, fallbackKey?: string) => void
  clear: () => void
}

export function useLoadError(defaultFallbackKey = 'errors.loadFailed'): LoadErrorHandle {
  const loadError = ref('')

  function fail(error: unknown, fallbackKey = defaultFallbackKey): void {
    if (isCancelError(error)) return      // 取消是正常路径（切页/切会话）
    loadError.value = describeError(error, t(fallbackKey))
  }

  function clear(): void {
    loadError.value = ''
  }

  return { loadError, fail, clear }
}
