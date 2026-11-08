/**
 * 全局焦点管理（审查 F1）。
 *
 * Esc/焦点原先零散补在各处（AppLayout 侧栏、ChatView 灯箱、slash 面板、
 * UniverseGraph），没有统一的焦点陷阱与"关闭后焦点归还"。naive-ui 的
 * n-modal/n-drawer 自带 trap 与还原，问题集中在**自绘覆盖层**上：Tab 能一路
 * 走出面板，关掉后焦点掉到 body。
 *
 * 这里提供一套与框架无关的实现：纯计算部分（nextFocusIndex）可单测，
 * DOM 部分在无 DOM 环境下自动降级为 no-op。
 */

/** 可聚焦元素选择器（与浏览器默认 Tab 序列对齐的常用子集） */
export const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'textarea:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/**
 * Tab / Shift+Tab 在环内的下一个下标。
 * current < 0 表示焦点已在环外（例如刚从面板外点进覆盖层），此时从头/尾进入。
 */
export function nextFocusIndex(current: number, length: number, shift: boolean): number {
  if (length <= 0) return -1
  if (current < 0 || current >= length) return shift ? length - 1 : 0
  if (shift) return current === 0 ? length - 1 : current - 1
  return current === length - 1 ? 0 : current + 1
}

/** 只有 Tab 参与环内切换；Esc 由各覆盖层自己决定关闭语义 */
export function isTrapKey(key: string): boolean {
  return key === 'Tab'
}

export interface FocusTrapHandle {
  activate(): void
  release(): void
}

/**
 * 给一个覆盖层容器装焦点陷阱：
 * - activate：记住当前焦点，聚焦容器内第一个可聚焦元素；
 * - Tab/Shift+Tab 在容器内循环，不会跑出去；
 * - release：移除监听并把焦点还给打开前的元素。
 */
export function attachFocusTrap(getContainer: () => HTMLElement | null): FocusTrapHandle {
  let previouslyFocused: HTMLElement | null = null
  let active = false

  const focusables = (): HTMLElement[] => {
    const root = getContainer()
    if (!root) return []
    return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      .filter(el => el.offsetParent !== null || el === root)
  }

  const onKeydown = (e: KeyboardEvent): void => {
    if (!active || !isTrapKey(e.key)) return
    const items = focusables()
    if (!items.length) return
    const activeEl = (typeof document !== 'undefined' ? document.activeElement : null) as HTMLElement | null
    const current = activeEl ? items.indexOf(activeEl) : -1
    e.preventDefault()
    const next = nextFocusIndex(current, items.length, e.shiftKey)
    if (next >= 0) items[next]?.focus()
  }

  return {
    activate() {
      if (active) return
      active = true
      if (typeof document === 'undefined') return
      previouslyFocused = document.activeElement as HTMLElement | null
      document.addEventListener('keydown', onKeydown, true)
      const items = focusables()
      if (items.length) items[0]?.focus()
    },
    release() {
      if (!active) return
      active = false
      if (typeof document !== 'undefined') {
        document.removeEventListener('keydown', onKeydown, true)
      }
      previouslyFocused?.focus?.()
      previouslyFocused = null
    },
  }
}
