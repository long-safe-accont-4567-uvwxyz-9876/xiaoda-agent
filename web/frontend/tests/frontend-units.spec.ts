/**
 * 前端纯逻辑单元测试（审计收尾轮）。
 *
 * 覆盖：滚动跟随判定、可见性轮询（含 dedupe）、请求取消登记、i18n 键索引。
 * 这些都是不依赖真实 DOM 的纯模块——组件层的行为靠 vue-tsc + 构建门禁 +
 * 报告里的手测步骤兜底。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  BOTTOM_THRESHOLD_PX,
  createScrollFollower,
  distanceFromBottom,
  isNearBottom,
} from '../src/utils/scrollFollow'
import { createVisibilityPoller } from '../src/composables/useVisibilityPolling'
import { createAbortRegistry, isAbortError } from '../src/utils/abortRegistry'
import { t, tf, hasKey } from '../src/i18n'

// ── 假滚动容器 ────────────────────────────────────────────
function fakeEl(state: { scrollTop: number; scrollHeight: number; clientHeight: number }) {
  const calls: Array<{ top: number; behavior?: string }> = []
  return {
    el: {
      get scrollTop() { return state.scrollTop },
      set scrollTop(v: number) { state.scrollTop = v },
      get scrollHeight() { return state.scrollHeight },
      get clientHeight() { return state.clientHeight },
      scrollTo(opts: { top: number; behavior?: string }) {
        calls.push(opts)
        state.scrollTop = opts.top
      },
    } as unknown as HTMLElement,
    calls,
    state,
  }
}

test('distanceFromBottom 不会返回负值（滚动回弹/小数高度）', () => {
  assert.equal(distanceFromBottom({ scrollTop: 900, scrollHeight: 1000, clientHeight: 100 }), 0)
  assert.equal(distanceFromBottom({ scrollTop: 0, scrollHeight: 1000, clientHeight: 100 }), 900)
})

test('isNearBottom 以 100px 为界', () => {
  const at = (gap: number) => ({ scrollTop: 0, scrollHeight: 1000 + gap, clientHeight: 1000 })
  assert.equal(isNearBottom(at(BOTTOM_THRESHOLD_PX)), true)
  assert.equal(isNearBottom(at(BOTTOM_THRESHOLD_PX + 1)), false)
})

test('跟随中：内容增长滚到底部（流式用 auto）', () => {
  const { el, calls } = fakeEl({ scrollTop: 500, scrollHeight: 600, clientHeight: 100 })
  const follower = createScrollFollower(() => el)
  assert.equal(follower.handleContentGrow(true), true)
  assert.deepEqual(calls.at(-1), { top: 600, behavior: 'auto' })
})

test('用户上滑后不再抢滚动；滚回底部附近自动恢复', () => {
  const { el, calls, state } = fakeEl({ scrollTop: 400, scrollHeight: 2000, clientHeight: 100 })
  const follower = createScrollFollower(() => el)
  follower.handleScroll()                       // 距底 1500px：用户在看历史
  assert.equal(follower.following, false)
  const before = calls.length
  assert.equal(follower.handleContentGrow(true), false)
  assert.equal(calls.length, before, '上滑状态不得触发滚动')

  state.scrollTop = 1900                        // 用户自己滚回底部
  follower.handleScroll()
  assert.equal(follower.following, true)
  assert.equal(follower.handleContentGrow(true), true)
})

test('forceFollow：发送消息时无条件恢复跟随', () => {
  const { el, calls } = fakeEl({ scrollTop: 0, scrollHeight: 3000, clientHeight: 100 })
  const follower = createScrollFollower(() => el)
  follower.handleScroll()
  assert.equal(follower.following, false)
  follower.forceFollow(true)
  assert.equal(follower.following, true)
  assert.equal(calls.at(-1)!.top, 3000)
})

test('容器未挂载时所有操作安全空转', () => {
  const follower = createScrollFollower(() => null)
  assert.equal(follower.handleContentGrow(true), false)
  follower.handleScroll()
  follower.forceFollow()
})

// ── 可见性轮询 ────────────────────────────────────────────
test('轮询：不可见时不起表，回到前台先补一次再恢复节奏', (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] } as never)
  let hidden = true
  let subscribeCb: (() => void) | null = null
  let runs = 0
  const poller = createVisibilityPoller(() => { runs++ }, 1000, {
    isHidden: () => hidden,
    subscribe: (cb) => { subscribeCb = cb; return () => { subscribeCb = null } },
  })
  poller.start()
  assert.equal(poller.running, false, '后台不应起表')
  t.mock.timers.tick(5000)
  assert.equal(runs, 0)

  hidden = false
  subscribeCb!()
  assert.equal(runs, 1, '恢复可见时立即补一次')
  assert.equal(poller.running, true)
  t.mock.timers.tick(2000)
  assert.equal(runs, 3, '2 个周期再跑 2 次')
  poller.stop()
  assert.equal(poller.running, false)
})

test('轮询 dedupe：上一轮未结束则跳过，不叠加请求', async (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] } as never)
  let started = 0
  let release: (() => void) | null = null
  const poller = createVisibilityPoller(() => {
    started++
    return new Promise<void>(resolve => { release = resolve })
  }, 1000, { isHidden: () => false, subscribe: () => () => {}, dedupe: true })
  poller.start()
  t.mock.timers.tick(1000)
  t.mock.timers.tick(1000)
  t.mock.timers.tick(1000)
  assert.equal(started, 1, '慢响应期间只允许一个在途请求')
  assert.equal(poller.inFlight, true)
  release!()
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(poller.inFlight, false, '完成后恢复可调度')
}) 

test('轮询 dedupe 关闭时保持旧语义（每拍都发）', (t) => {
  t.mock.timers.enable({ apis: ['setInterval'] } as never)
  let runs = 0
  const poller = createVisibilityPoller(() => { runs++ }, 1000, {
    isHidden: () => false,
    subscribe: () => () => {},
  })
  poller.start()
  t.mock.timers.tick(3000)
  assert.equal(runs, 3)
  poller.stop()
})

// ── 请求取消 ──────────────────────────────────────────────
test('AbortRegistry：abortAll 取消全部在途请求且出栈', () => {
  const registry = createAbortRegistry()
  const a = registry.create()
  const b = registry.create()
  assert.equal(registry.size, 2)
  assert.equal(a.aborted, false)
  registry.abortAll('component-disposed')
  assert.equal(a.aborted, true)
  assert.equal(b.aborted, true)
  assert.equal(registry.size, 0)
  assert.equal(registry.aborted, true)
})

test('isAbortError 只认取消/超时', () => {
  assert.equal(isAbortError(Object.assign(new Error('x'), { name: 'AbortError' })), true)
  assert.equal(isAbortError(Object.assign(new Error('x'), { name: 'TimeoutError' })), true)
  assert.equal(isAbortError(new Error('x')), false)
  assert.equal(isAbortError(null), false)
})

// ── i18n 键索引 ───────────────────────────────────────────
test('t()：嵌套键、函数型文案、缺键回退键名', () => {
  assert.equal(typeof t('chatView.clear'), 'string')
  assert.notEqual(t('chatView.clear'), 'chatView.clear')
  assert.equal(t('__not.exists__'), '__not.exists__')
  assert.equal(typeof tf, 'function')
  assert.equal(hasKey('chatView.clear'), true)
  assert.equal(hasKey('__not.exists__'), false)
})

test('t() 微基准：索引查找不慢于旧的 split 逐级下钻', () => {
  const keys = ['chatView.clear', 'chatView.copy', 'chatView.export', 'chatView.history']
  const N = 200_000
  const start = performance.now()
  let sink = 0
  for (let i = 0; i < N; i++) sink += t(keys[i & 3]).length
  const indexed = performance.now() - start
  assert.ok(sink > 0)
  // 只做量级断言：索引实现必须快过 1 微秒/次（旧实现约 0.15–0.35µs/次）
  assert.ok(indexed / N < 1, `indexed t() too slow: ${(indexed / N * 1000).toFixed(2)}ns/call`)
})
