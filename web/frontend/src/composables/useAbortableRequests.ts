/**
 * 组件级在途请求取消（审计 P1-6 的生命周期绑定层）。
 *
 * 卸载与 keep-alive 失活两种场景都要取消：失活时组件还活着但用户已经看不到了，
 * 继续回写状态没有意义，反而会和重新激活后的新请求打架。
 */
import { onBeforeUnmount, onDeactivated } from 'vue'
import { createAbortRegistry, type AbortRegistry } from '../utils/abortRegistry'

export function useAbortableRequests(): AbortRegistry {
  const registry = createAbortRegistry()
  const abort = () => registry.abortAll('component-disposed')
  onDeactivated(abort)
  onBeforeUnmount(abort)
  return registry
}
