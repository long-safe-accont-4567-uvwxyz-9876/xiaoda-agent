/**
 * 纳西妲宇宙 —— 性能监测与自适应降档（从 UniverseGraph.vue 拆出）
 *
 * 每 500ms 采样帧率；连续低帧率自动降级质量档位：
 * high → medium: 关 Bloom、星层减半；medium → low: 关链路粒子、节点降到 6 段。
 * 档位实际应用（applyQualityTier）留在引擎层，本模块只负责采样与判定。
 */
import type { Ref } from 'vue'
import type { QualityTier } from './types'

export interface PerfMonitorOptions {
  fps: Ref<number>
  qualityTier: Ref<QualityTier>
  /** 存活检查（组件卸载后停止采样） */
  isAlive: () => boolean
  /** 判定降档后同步调用（引擎应用新档位） */
  onTierApplied: () => void
}

export interface PerfMonitor {
  start(): void
  stop(): void
  /** 手动切换档位（开关灯）时清零连击计数 */
  resetStreak(): void
}

const SAMPLE_INTERVAL_MS = 500
const LOW_FPS_THRESHOLD = 30
const STREAK_TO_MEDIUM = 2
const STREAK_TO_LOW = 4

export function createPerfMonitor(opts: PerfMonitorOptions): PerfMonitor {
  let raf = 0
  let frameCount = 0
  let lastPerfTime = 0
  let lowFpsStreak = 0

  function start(): void {
    if (raf) return
    frameCount = 0
    lastPerfTime = performance.now()
    const tick = () => {
      if (!opts.isAlive()) return
      frameCount++
      const now = performance.now()
      const elapsed = now - lastPerfTime
      if (elapsed >= SAMPLE_INTERVAL_MS) {
        opts.fps.value = Math.round((frameCount * 1000) / elapsed)
        if (opts.fps.value < LOW_FPS_THRESHOLD) {
          lowFpsStreak++
          if (lowFpsStreak >= STREAK_TO_MEDIUM && opts.qualityTier.value === 'high') {
            opts.qualityTier.value = 'medium'
            opts.onTierApplied()
          } else if (lowFpsStreak >= STREAK_TO_LOW && opts.qualityTier.value === 'medium') {
            opts.qualityTier.value = 'low'
            opts.onTierApplied()
          }
        } else {
          lowFpsStreak = 0
        }
        frameCount = 0
        lastPerfTime = now
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
  }

  function stop(): void {
    if (raf) cancelAnimationFrame(raf)
    raf = 0
  }

  return {
    start,
    stop,
    resetStreak() { lowFpsStreak = 0 },
  }
}
