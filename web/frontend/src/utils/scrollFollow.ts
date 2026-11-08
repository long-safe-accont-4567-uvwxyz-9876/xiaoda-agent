/**
 * 聊天消息列表"跟随底部"判定（审计 P0-2）。
 *
 * 背景：ChatView 原来的自动滚动只 watch 了 `chat.messages.length`，
 * 而流式回复是**同一条消息的 content 在增长**，length 不变——首帧之后
 * 视口就再也不动了，长回答必须手动往下拖。
 *
 * 只把 length 换成 content.length 也不对：用户在生成过程中上滑看历史，
 * 每次 delta 都会把他拽回底部，反而更难受。正确语义是"跟随意图"：
 *  - 用户处于底部附近（<= threshold）→ 认为他想跟随，内容增长就滚到底；
 *  - 用户主动上滑离开底部 → 停止跟随，不再抢滚动；
 *  - 用户自己滚回底部附近 → 自动恢复跟随。
 *
 * 这里只放纯逻辑（不依赖 DOM 类型），便于用假元素做单元测试；
 * 生命周期与监听绑定在 composables/useAutoScroll.ts。
 */

/** 距底部多少像素内算"在底部附近" */
export const BOTTOM_THRESHOLD_PX = 100

export interface ScrollMetrics {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}

/** 距底部距离（负值/抖动统一夹到 0） */
export function distanceFromBottom(m: ScrollMetrics): number {
  return Math.max(0, m.scrollHeight - m.scrollTop - m.clientHeight)
}

/** 是否处于"跟随底部"的意图区内 */
export function isNearBottom(m: ScrollMetrics, threshold: number = BOTTOM_THRESHOLD_PX): boolean {
  return distanceFromBottom(m) <= threshold
}

export interface ScrollFollowerOptions {
  threshold?: number
}

export interface ScrollFollower {
  /** 当前是否处于跟随状态（只读语义：由滚动手势与显式 forceFollow 决定） */
  readonly following: boolean
  /** 滚动事件：按当前位置重算跟随意图 */
  handleScroll(): void
  /**
   * 内容增长（流式 delta / 消息追加）。
   * @param streaming 流式期间用 auto（即时贴底），否则用 smooth
   * @returns 是否真的触发了滚动
   */
  handleContentGrow(streaming: boolean): boolean
  /** 用户主动发起（发送消息、点重发）——无条件恢复跟随并贴底 */
  forceFollow(streaming?: boolean): void
  /** 会话切换等场景复位（默认恢复跟随） */
  reset(following?: boolean): void
}

export function createScrollFollower(
  getEl: () => HTMLElement | null,
  options: ScrollFollowerOptions = {},
): ScrollFollower {
  const threshold = options.threshold ?? BOTTOM_THRESHOLD_PX
  let following = true

  function scrollToBottom(behavior: ScrollBehavior): boolean {
    const el = getEl()
    if (!el) return false
    el.scrollTo({ top: el.scrollHeight, behavior })
    return true
  }

  return {
    get following() {
      return following
    },
    handleScroll() {
      const el = getEl()
      if (!el) return
      following = isNearBottom(el, threshold)
    },
    handleContentGrow(streaming: boolean) {
      if (!following) return false
      return scrollToBottom(streaming ? 'auto' : 'smooth')
    },
    forceFollow(streaming = true) {
      following = true
      scrollToBottom(streaming ? 'auto' : 'smooth')
    },
    reset(next = true) {
      following = next
    },
  }
}
