/**
 * 消息列表自动滚动（审计 P0-2 的 DOM 绑定层）。
 *
 * 纯判定逻辑在 utils/scrollFollow.ts；这里只负责：
 *  - 挂载时给容器加 passive scroll 监听（rAF 合并，滚动高频下不重复计算）；
 *  - 卸载时移除监听；
 *  - 暴露 handleContentGrow() 给"内容增长"的 watch 使用。
 *
 * 说明：监听与 useRenderedBand 各自独立——两者都只读几何、不写 DOM 布局，
 * 合并进一个 composable 反而会让两处职责纠缠。
 */
import { onBeforeUnmount, onMounted, type Ref } from 'vue'
import {
  createScrollFollower,
  type ScrollFollower,
  type ScrollFollowerOptions,
} from '../utils/scrollFollow'

export function useAutoScroll(
  elRef: Ref<HTMLElement | null>,
  options: ScrollFollowerOptions = {},
): ScrollFollower {
  const follower = createScrollFollower(() => elRef.value, options)
  let rafId = 0

  function onScroll() {
    if (rafId) return
    rafId = requestAnimationFrame(() => {
      rafId = 0
      follower.handleScroll()
    })
  }

  onMounted(() => {
    elRef.value?.addEventListener('scroll', onScroll, { passive: true })
  })

  onBeforeUnmount(() => {
    if (rafId) cancelAnimationFrame(rafId)
    rafId = 0
    elRef.value?.removeEventListener('scroll', onScroll)
  })

  return follower
}
