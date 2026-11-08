/**
 * 在途请求取消登记（审计 P1-6）。
 *
 * 背景：全仓 0 处 AbortController。切页/切会话后，慢请求仍然返回并回写
 * store 或组件状态——轻则把旧会话的消息写进新会话视图，重则卸载后往
 * 已销毁的响应式对象上写数据（开发环境告警，生产环境表现为"幽灵消息"）。
 *
 * 语义边界（重要）：
 *  - 只取消"本组件自己发起"的请求，不取消共享缓存/全局单例上的请求；
 *  - 取消后 fetch 会以 AbortError 拒绝，调用方用 isAbortError() 静默跳过，
 *    不弹错误提示（这是预期行为，不是故障）；
 *  - store 层的 generation 守卫（loadSessionGeneration）继续作为跨会话
 *    正确性的最终防线，两者互不替代。
 */

export interface AbortRegistry {
  /** 新建一个在途请求的 signal 并登记 */
  create(): AbortSignal
  /** 取消全部在途请求（组件卸载/切页时调用） */
  abortAll(reason?: string): void
  /** 清空登记（不触发取消，用于复位的极端场景） */
  clear(): void
  /** 当前在途请求数（测试用） */
  readonly size: number
  /** 是否已被整体取消过（用于幂等断言） */
  readonly aborted: boolean
}

export function createAbortRegistry(): AbortRegistry {
  const controllers = new Set<AbortController>()
  let aborted = false

  return {
    create() {
      const controller = new AbortController()
      controllers.add(controller)
      // 请求结束（无论成功/失败/取消）后自动出栈，避免 Set 无界增长
      controller.signal.addEventListener('abort', () => controllers.delete(controller), { once: true })
      return controller.signal
    },
    abortAll(reason?: string) {
      aborted = true
      for (const controller of controllers) controller.abort(reason)
      controllers.clear()
    },
    clear() {
      controllers.clear()
    },
    get size() {
      return controllers.size
    },
    get aborted() {
      return aborted
    },
  }
}

/** 判断是否"被主动取消"——这类错误应静默处理，不提示用户 */
export function isAbortError(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const name = (error as { name?: string }).name
  return name === 'AbortError' || name === 'TimeoutError'
}
