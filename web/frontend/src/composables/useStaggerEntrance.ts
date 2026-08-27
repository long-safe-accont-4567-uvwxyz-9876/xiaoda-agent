/**
 * 列表卡片/行 stagger 入场编排（GSAP 驱动）。
 *
 * 触发纪律（避免动画噪音）：
 * - 仅当挂载时列表为空、随后数据首次非空时播放一次 —— 即"加载骨架 → 内容"的衔接；
 *   pinia 缓存命中（挂载即有数据）不播，避免与路由过渡叠加运动；
 *   后续筛选/搜索引起的列表变化不重播。
 * - 目标为容器直接子元素，完成后 clearProps 归还样式基线，
 *   不与子组件（如 Tilt3D）的内联 transform 状态机冲突。
 * - low-gpu / prefers-reduced-motion 时整个模块零介入，元素保持默认可见。
 *
 * 闪现防护：数据首达时先在绘制前同步隐藏子元素，待 gsap chunk 就绪后
 * 由动画接管；chunk 加载失败或等待期间护栏命中则立即还原可见，
 * 保证任何异常路径的兜底都是"内容正常显示"。
 */
import { watch, onScopeDispose } from 'vue'
import type { Ref } from 'vue'
import { loadGsap, motionBlocked, entranceDefaults } from '../utils/gsapMotion'

export interface StaggerEntranceOptions {
  /** 上浮距离 px（从下方浮入） */
  distance?: number
  /** 相邻两项间隔（秒）；超长列表自动压缩间隔保总时长 */
  staggerEach?: number
}

/** 入场编排最多覆盖的条目数，超出部分一次性淡入，防止长列表拖沓 */
const MAX_SEQUENCED_ITEMS = 16

type Gsap = NonNullable<Awaited<ReturnType<typeof loadGsap>>>

export function useStaggerEntrance(
  container: Ref<HTMLElement | null>,
  source: Ref<unknown>,
  opts: StaggerEntranceOptions = {},
) {
  const { distance = 18, staggerEach = 0.055 } = opts

  // 挂载时列表非空视为缓存命中，本次生命周期不再编排
  let armed = !isFilled(source.value)
  let disposed = false
  let ctx: gsap.Context | null = null
  let tlRef: gsap.core.Timeline | null = null

  function isFilled(v: unknown): boolean {
    return Array.isArray(v) ? v.length > 0 : !!v
  }

  function reveal(targets: Element[]) {
    targets.forEach((t) => ((t as HTMLElement).style.visibility = ''))
  }

  watch(source, async (val) => {
    if (!armed || disposed || !isFilled(val)) return
    armed = false
    const el = container.value
    if (!el || !el.children.length) return

    const targets = Array.from(el.children)
    // flush:'post' 回调早于本轮绘制：同步隐藏，避免 chunk 网络延迟期的内容闪现
    targets.forEach((t) => ((t as HTMLElement).style.visibility = 'hidden'))

    let gsap: Gsap | null = null
    try {
      gsap = await loadGsap()
    } catch {
      gsap = null
    }
    // 护栏在等待期间可能变化（low-gpu 是异步实测判定），动效前复查；
    // 任一失败路径都先把内容还给用户
    if (disposed || !gsap || motionBlocked()) {
      reveal(targets)
      return
    }

    const seqCount = Math.min(targets.length, MAX_SEQUENCED_ITEMS)
    const seqTargets = targets.slice(0, seqCount)
    const restTargets = targets.slice(seqCount)

    // context 包裹：组件卸载（含动画进行中）统一 revert，
    // 自动恢复被动画元素的内联样式，比手写 kill 多一层保险
    ctx = gsap.context((self: gsap.Context) => {
      const tl = gsap.timeline({
        onComplete: () => {
          gsap.set(targets, { clearProps: 'opacity,visibility,transform' })
          self.kill()
        },
      })
      tl.to(seqTargets, {
        autoAlpha: entranceDefaults.autoAlpha,
        y: entranceDefaults.y,
        duration: entranceDefaults.duration,
        ease: entranceDefaults.ease,
        stagger: targets.length > MAX_SEQUENCED_ITEMS ? Math.max(0.02, staggerEach / 2) : staggerEach,
      }, 0)
      if (restTargets.length) {
        tl.to(restTargets, {
          autoAlpha: entranceDefaults.autoAlpha,
          y: entranceDefaults.y,
          duration: entranceDefaults.duration,
        }, 0.28)
      }
      tlRef = tl
    }, el)
  }, { flush: 'post' })

  onScopeDispose(() => {
    disposed = true
    ctx?.revert()
    ctx = null
  })

  /** 供视图探测是否仍处于编排期（调试用） */
  function isPlaying(): boolean {
    return !!tlRef?.isActive()
  }

  return { isPlaying }
}
