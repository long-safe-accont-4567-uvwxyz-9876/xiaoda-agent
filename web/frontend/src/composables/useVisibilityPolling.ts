/**
 * 可见性感知的轮询（审计低 L6 / 中 P1-5）。
 *
 * 背景：DashboardView 与 ComputeDevicesTab 都用 setInterval(..., 5000) 轮询，
 * 标签页切到后台后请求照发——按 5 秒 2 个接口算，一天约 3.4 万次请求，
 * 既白烧板子 CPU，也让真异常被轮询日志埋掉。
 *
 * 行为：document.hidden 时停表；重新可见时先立即拉一次再恢复节奏；
 * 组件卸载自动清理，调用方不需要再自己管 timer。
 *
 * dedupe（P1-5）：慢响应时 setInterval 会叠加下一轮请求，响应乱序回写覆盖新状态。
 * 打开后上一轮未结束即跳过本轮 tick。默认关，保持既有调用方语义不变。
 *
 * 纯逻辑（createVisibilityPoller）与生命周期绑定（useVisibilityPolling）分开，
 * 前者可用假定时器/假可见性做单元测试，不需要真实 DOM。
 */
import { onBeforeUnmount, onMounted } from 'vue'

export interface VisibilityPollingOptions {
  /** 挂载即开始轮询；false 时由调用方显式 start()（如受开关控制的面板） */
  autoStart?: boolean
  /** 上一轮未完成时跳过本轮（默认 false） */
  dedupe?: boolean
  /** 环境探测：默认读 document.hidden */
  isHidden?: () => boolean
  /** 订阅可见性变化并返回取消订阅函数 */
  subscribe?: (handler: () => void) => () => void
}

export interface VisibilityPoller {
  start(): void
  stop(): void
  readonly running: boolean
  readonly inFlight: boolean
}

const defaultIsHidden = (): boolean => typeof document !== 'undefined' && !!document.hidden

const defaultSubscribe = (handler: () => void): (() => void) => {
  if (typeof document === 'undefined') return () => {}
  document.addEventListener('visibilitychange', handler)
  return () => document.removeEventListener('visibilitychange', handler)
}

export function createVisibilityPoller(
  task: () => void | Promise<void>,
  intervalMs = 5000,
  options: VisibilityPollingOptions = {},
): VisibilityPoller {
  const isHidden = options.isHidden ?? defaultIsHidden
  const subscribe = options.subscribe ?? defaultSubscribe
  const dedupe = options.dedupe ?? false

  let timer: ReturnType<typeof setInterval> | null = null
  let unsubscribe: (() => void) | null = null
  let inFlight = false

  function run(): void {
    if (!dedupe) {
      void task()
      return
    }
    if (inFlight) return
    inFlight = true
    const done = () => { inFlight = false }
    try {
      const result = task()
      if (result && typeof (result as Promise<void>).then === 'function') {
        ;(result as Promise<void>).then(done, done)
      } else {
        done()
      }
    } catch {
      done()
    }
  }

  function clearTimer(): void {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  function start(): void {
    clearTimer()
    // 订阅必须无条件建立：挂载时页面就在后台（后台标签页打开链接）也得能
    // 在回到前台时恢复，否则"停表"就变成"永不再跑"。
    if (!unsubscribe) {
      unsubscribe = subscribe(() => {
        if (isHidden()) {
          // 后台：只停表，保留订阅，回到前台才能立刻恢复
          clearTimer()
        } else {
          run()
          start()
        }
      })
    }
    if (isHidden()) return
    timer = setInterval(run, intervalMs)
  }

  function stop(): void {
    clearTimer()
    unsubscribe?.()
    unsubscribe = null
    inFlight = false
  }

  return {
    start,
    stop,
    get running() {
      return timer !== null
    },
    get inFlight() {
      return inFlight
    },
  }
}

export function useVisibilityPolling(
  task: () => void | Promise<void>,
  intervalMs = 5000,
  options: VisibilityPollingOptions = {},
) {
  const { autoStart = true } = options
  const poller = createVisibilityPoller(task, intervalMs, options)

  onMounted(() => {
    if (autoStart) poller.start()
  })

  onBeforeUnmount(() => poller.stop())

  return { start: () => poller.start(), stop: () => poller.stop(), poller }
}
