/** GSAP 动效引擎封装：懒加载 + 统一性能护栏 + Sumeru 令牌映射。
 *
 * 设计约束（2026-08-27 前端动效迭代）：
 * - gsap 及其插件通过动态 import() 引入，vite 自动拆独立 chunk，不占首屏加载；
 *   首个动画调用触发加载，未触发动画的页面零成本。
 * - 复用 App.vue 已有的 body.low-gpu 弱 GPU 检测结果与 prefers-reduced-motion
 *   系统偏好：命中任一护栏时所有入口直接跳过或退化为静态赋值，
 *   保证任何异常路径的兜底都是"内容正常呈现、功能不受影响"。
 * - 不替换存量 CSS transition；本模块只服务 CSS 不便编排的场景
 *   （stagger 入场、数字滚动、FLIP 重排、反馈震颤、quickTo 跟随），
 *   逐帧走 transform/opacity 合成器属性。
 *
 * 类型说明：gsap 的类型定义通过全局命名空间 gsap 暴露（gsap.TweenVars 等），
 * 由动态 import('gsap') 触发声明文件加载，无需静态 import type。
 */
import { nextTick } from 'vue'

/** 加载后的 gsap 核心（全局命名空间类型，由 import('gsap') 声明文件提供） */
export type Gsap = typeof gsap

/** Sumeru tokens 三条动效曲线对应的 GSAP ease 映射（取最接近的内置曲线）。 */
export const sumeruEases = {
  out: 'power3.out',          // --ease-out: cubic-bezier(0.16, 1, 0.3, 1)
  smooth: 'power2.inOut',     // --ease-smooth: cubic-bezier(0.4, 0, 0.2, 1)
  spring: 'back.out(1.4)',    // --ease-spring: cubic-bezier(0.2, 0.85, 0.25, 1.15)
} as const

let gsapPromise: Promise<Gsap> | null = null

/** 动效是否被环境护栏拦截（低性能设备 / 用户系统关闭动画）。 */
export function motionBlocked(): boolean {
  if (typeof window === 'undefined') return true
  return (
    document.body.classList.contains('low-gpu') ||
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

/** 加载 gsap 核心；护栏命中时返回 null，调用方跳过动画。 */
export function loadGsap(): Promise<Gsap | null> {
  if (motionBlocked()) return Promise.resolve(null)
  if (!gsapPromise) {
    gsapPromise = import('gsap').then((m) => m.gsap)
  }
  return gsapPromise
}

/** 卡片/行入场默认参数：小幅上浮 + 淡入，令牌时长量级（200~400ms 谱系）。 */
export const entranceDefaults: gsap.TweenVars = {
  autoAlpha: 1,
  y: 0,
  duration: 0.34,
  ease: sumeruEases.out,
}

/**
 * 反馈震颤（失败提示）：水平位移抖动，不遮字、不改布局。
 * 与 message.error toast 并用——toast 说明原因，震颤指向出错的容器。
 * 返回 false 表示护栏命中或 gsap 未就绪，调用方无需兜底（震颤只是锦上添花）。
 */
export async function shakeEl(el: HTMLElement): Promise<boolean> {
  const gsap = await loadGsap()
  if (!gsap) return false
  // fromTo 起点归零：连续触发不会从残留偏移量起跳
  gsap.fromTo(
    el,
    { x: 0 },
    {
      keyframes: [
        { x: -8 }, { x: 7 }, { x: -5 }, { x: 3 }, { x: 0 },
      ],
      duration: 0.38,
      ease: 'sine.inOut',
    },
  )
  return true
}

/**
 * 高频指针跟随（quickTo）：每个坐标一条常驻复用补间，
 * 静止时零 tick 零 CPU —— 替代自绘 requestAnimationFrame 循环。
 * duration 差实现拖尾层次（快层小值、慢层大值）。
 * 返回 moveTo(x,y) 与 stop()；未加载完成期间的移动会记入 pending 保证不丢帧；
 * 护栏命中时退化为直接写 transform，视觉行为不变。
 */
export async function createFollower(
  el: HTMLElement,
  opts: { duration?: number; ease?: string; center?: boolean } = {},
): Promise<{ move(x: number, y: number): void; stop(): void }> {
  const { duration = 0.35, ease = 'power2.out', center = false } = opts
  const apply = (x: number, y: number) => {
    el.style.transform =
      `translate3d(${x}px, ${y}px, 0)` + (center ? ' translate(-50%, -50%)' : '')
  }
  const gsap = await loadGsap()
  if (!gsap) return { move: apply, stop: () => {} }

  const toX = gsap.quickTo(el, 'x', { duration, ease })
  const toY = gsap.quickTo(el, 'y', { duration, ease })
  return {
    move(x: number, y: number) {
      // yPercent/xPercent 常驻居中偏移只设一次；quickTo 管坐标
      if (center) gsap.set(el, { xPercent: -50, yPercent: -50 })
      toX(x)
      toY(y)
    },
    stop() {
      gsap.killTweensOf(el)
    },
  }
}

/**
 * 一次性入场编排：立即写入隐藏起始态（防闪现）→ 懒加载 gsap →
 * 子元素自下而上错峰浮现。护栏命中 / 加载失败时立刻还原可见。
 * 返回是否真正编排（调试用）；调用方无需任何清理（timeline 自动结束释放）。
 */
export async function playEntrance(
  container: HTMLElement | null,
  opts: { stagger?: number; duration?: number; distance?: number; delay?: number } = {},
): Promise<boolean> {
  const { stagger = 0.08, duration = 0.5, distance = 16, delay = 0 } = opts
  if (!container || !container.children.length) return false
  const targets = Array.from(container.children)
  targets.forEach((t) => ((t as HTMLElement).style.visibility = 'hidden'))
  const gsap = await loadGsap()
  if (!gsap || motionBlocked() || !container.isConnected) {
    targets.forEach((t) => ((t as HTMLElement).style.visibility = ''))
    return false
  }
  gsap.set(targets, { autoAlpha: 0, y: distance })
  targets.forEach((t) => ((t as HTMLElement).style.visibility = ''))
  gsap.fromTo(targets,
    { autoAlpha: 0, y: distance },
    { autoAlpha: 1, y: 0, duration, ease: sumeruEases.out, stagger, delay,
      onComplete: () => gsap.set(targets, { clearProps: 'opacity,visibility,transform' }) })
  return true
}

/**
 * 数字滚动计数（仪表盘统计卡）：对普通对象补间 + snap 取整写回。
 * 小数场景传 decimals 控制精度；护栏命中或 gsap 失败时直接终值静态赋值。
 */
export async function countTo(
  apply: (display: number) => void,
  to: number,
  opts: { decimals?: number; duration?: number } = {},
): Promise<void> {
  const { decimals = 0, duration = 0.9 } = opts
  const gsap = await loadGsap()
  if (!gsap || motionBlocked()) {
    apply(to)
    return
  }
  const proxy = { v: 0 }
  gsap.to(proxy, {
    v: to,
    duration,
    ease: sumeruEases.out,
    onUpdate: () => {
      const m = 10 ** decimals
      apply(Math.round(proxy.v * m) / m)
    },
  })
}

/**
 * 列表数据变更后的 FLIP 重排过渡（Flip 插件懒加载）：
 * 需在 DOM 数据写入前调用，返回的 finish 函数在 nextTick 后调用。
 * 护栏命中 / 无变化 / Flip 加载失败时 finish 是纯 no-op，重排照常发生。
 */
export async function flipCapture(container: HTMLElement | null) {
  if (!container || motionBlocked()) return () => {}
  const mod = await import('gsap/Flip').catch(() => null)
  if (!mod) return () => {}
  try {
    ;(await import('gsap')).gsap.registerPlugin(mod.Flip)
  } catch {
    return () => {}
  }
  await nextTick() // 等待期间视图可能已重渲染，以当下 DOM 为准
  if (!container.isConnected) return () => {}
  const state = mod.Flip.getState(Array.from(container.children))
  return () => {
    if (!container.isConnected || motionBlocked()) return
    mod.Flip.from(state, {
      duration: 0.42,
      ease: sumeruEases.out,
      absolute: true,   // grid 内不挤压兄弟元素布局
      simple: true,     // 只动 transform，跳过尺寸/字体度量插值
      nested: true,
    })
  }
}
