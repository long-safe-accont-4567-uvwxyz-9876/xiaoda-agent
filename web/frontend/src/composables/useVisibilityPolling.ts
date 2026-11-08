/**
 * 可见性感知的轮询（审计低 L6）。
 *
 * 背景：DashboardView 与 ComputeDevicesTab 都用 setInterval(..., 5000) 轮询，
 * 标签页切到后台后请求照发——按 5 秒 2 个接口算，一天约 3.4 万次请求，
 * 既白烧板子 CPU，也让真异常被轮询日志埋掉。
 *
 * 行为：document.hidden 时停表；重新可见时先立即拉一次再恢复节奏；
 * 组件卸载自动清理，调用方不需要再自己管 timer。
 */
import { onBeforeUnmount, onMounted } from 'vue'

export interface VisibilityPollingOptions {
  /** 挂载即开始轮询；false 时由调用方显式 start()（如受开关控制的面板） */
  autoStart?: boolean
}

export function useVisibilityPolling(
  task: () => void | Promise<void>,
  intervalMs = 5000,
  options: VisibilityPollingOptions = {},
) {
  const { autoStart = true } = options
  let timer: ReturnType<typeof setInterval> | null = null

  function stop() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  function start() {
    stop()
    if (typeof document !== 'undefined' && document.hidden) return
    timer = setInterval(() => { void task() }, intervalMs)
  }

  function onVisibilityChange() {
    if (document.hidden) {
      stop()
    } else {
      void task()
      start()
    }
  }

  onMounted(() => {
    document.addEventListener('visibilitychange', onVisibilityChange)
    if (autoStart) start()
  })

  onBeforeUnmount(() => {
    document.removeEventListener('visibilitychange', onVisibilityChange)
    stop()
  })

  return { start, stop }
}
