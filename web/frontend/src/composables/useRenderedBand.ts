// useRenderedBand —— 长消息列表"离屏惰性渲染"（零依赖）
// 聊天 1000 条上限下，全部行 v-html markdown 渲染 + 布局重排仍会在
// 弱机/WebView2 上让滚动与流式阶段掉帧。策略（与后端终端合帧自适应
// 退避同批优化）：
//   1. 只对"可视带"（视口 ± margin，默认 1.2 倍视口高）内的行渲染 markdown；
//   2. 带外行由调用方降级为纯文本（escapeHtmlText），DOM 常驻但成本极低；
//   3. 调用方给行加 content-visibility + contain-intrinsic-size，离屏行
//      连 layout/paint 由浏览器原生跳过。
// 几何采集（getBoundingClientRect）只在脏标记时发生（滚动/内容变化/重采样），
// 带宽用缓存的 top/height 数组 O(n) 每帧计算；结果以 Set 透出，与上次
// 相同则跳过赋值（滚动高频下避免整列表无谓重渲染）。
import { onBeforeUnmount, onMounted, shallowRef, type Ref, type ShallowRef } from 'vue'

export interface RenderedBandOptions {
  /** 可视带扩展倍数：1 = 视口外再渲染一屏高度的内容 */
  margin?: number
  /** 周期性重采样间隔(ms)，0 关闭；用于流式内容长度变化场景兜底重测 */
  resampleMs?: number
  /** 周期性重采样激活条件（如"仅流式期间"），避免静止时循环空测 */
  resampleActive?: () => boolean
}

export interface RenderedBand {
  /** 带内索引集合；null = 尚未测量，调用方应全量渲染兜底 */
  rendered: ShallowRef<ReadonlySet<number> | null>
  /** 请求重算（rAF 合并同帧多次调用）；forceDirty=true 强制重测几何 */
  refresh: (forceDirty?: boolean) => void
}

/** 纯 HTML 转义（带外行当纯文本输出，与 Vue 插值同级安全） */
export function escapeHtmlText(text: string): string {
  return text.replace(/[&<>"']/g, (ch) => {
    switch (ch) {
      case '&': return '&amp;'
      case '<': return '&lt;'
      case '>': return '&gt;'
      case '"': return '&quot;'
      default: return '&#39;'
    }
  })
}

/** 计算可视带索引区间 [first, last]（闭区间）；无行时 [-1, -1] */
export function computeBandRange(
  tops: number[], heights: number[],
  scrollTop: number, viewportHeight: number, margin: number,
): [number, number] {
  if (tops.length === 0) return [-1, -1]
  const topLimit = scrollTop - margin
  const bottomLimit = scrollTop + viewportHeight + margin
  let first = tops.length
  let last = -1
  for (let i = 0; i < tops.length; i++) {
    const rowTop = tops[i]
    const rowBottom = i + 1 < tops.length ? tops[i + 1] : tops[i] + heights[i]
    if (rowTop <= bottomLimit && rowBottom >= topLimit) {
      if (i < first) first = i
      if (i > last) last = i
    }
  }
  return first <= last ? [first, last] : [-1, -1]
}

const ROW_SELECTOR = ':scope > .message-row'

function sameBand(prev: ReadonlySet<number>, first: number, last: number): boolean {
  if (prev.size !== last - first + 1) return false
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  for (const v of prev) {
    if (v < min) min = v
    if (v > max) max = v
  }
  return min === first && max === last
}

export function useRenderedBand(
  containerRef: Ref<HTMLElement | null>,
  itemCount: Ref<number>,
  options: RenderedBandOptions = {},
): RenderedBand {
  const margin = options.margin ?? 1.2
  const resampleMs = options.resampleMs ?? 0
  const rendered = shallowRef<ReadonlySet<number> | null>(null)

  let tops: number[] = []
  let heights: number[] = []
  let dirty = true
  let rafId = 0
  let resampleId: ReturnType<typeof setInterval> | null = null

  /** 几何重测：行数与消息数一致才采用，否则保持旧几何（保守全量） */
  function measure(): boolean {
    const el = containerRef.value
    if (!el) return false
    const rows = el.querySelectorAll(ROW_SELECTOR)
    if (rows.length !== itemCount.value) return false
    const elRect = el.getBoundingClientRect()
    const scrollTop = el.scrollTop
    const n = rows.length
    const newTops = new Array<number>(n)
    const newHeights = new Array<number>(n)
    for (let i = 0; i < n; i++) {
      const rect = rows[i].getBoundingClientRect()
      newTops[i] = rect.top - elRect.top + scrollTop
      newHeights[i] = rect.height
    }
    tops = newTops
    heights = newHeights
    return true
  }

  function apply() {
    rafId = 0
    const el = containerRef.value
    const count = itemCount.value
    const prev = rendered.value
    if (!el || count === 0) {
      if (prev) rendered.value = null
      return
    }
    if (dirty) {
      if (!measure()) return  // 几何瞬态不可用：保持现状，下次刷新再试
      dirty = false
    }
    const vpHeight = el.clientHeight
    const [first, last] = computeBandRange(tops, heights, el.scrollTop, vpHeight, vpHeight * margin)
    if (first < 0) return
    if (prev && sameBand(prev, first, last)) return  // 带未变，跳过赋值
    const set = new Set<number>()
    for (let i = first; i <= last; i++) set.add(i)
    rendered.value = set
  }

  function refresh(forceDirty = false) {
    if (forceDirty) dirty = true
    if (rafId) return  // rAF 合并：同帧只算一次
    rafId = requestAnimationFrame(apply)
  }

  const onResize = () => refresh(true)

  onMounted(() => {
    refresh(true)
    window.addEventListener('resize', onResize)
    if (resampleMs > 0) {
      resampleId = setInterval(() => {
        if (!options.resampleActive || options.resampleActive()) refresh(true)
      }, resampleMs)
    }
  })

  onBeforeUnmount(() => {
    if (rafId) cancelAnimationFrame(rafId)
    window.removeEventListener('resize', onResize)
    if (resampleId !== null) clearInterval(resampleId)
  })

  return { rendered, refresh }
}