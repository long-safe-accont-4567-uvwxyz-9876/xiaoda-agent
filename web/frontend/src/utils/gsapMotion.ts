/**
 * GSAP 动效引擎封装：懒加载 + 统一性能护栏 + Sumeru 令牌映射。
 *
 * 设计约束（2026-08-27 前端动效迭代）：
 * - gsap 通过动态 import() 引入，vite 自动拆独立 chunk，不占首屏加载；
 *   首个动画调用触发加载，未触发动画的页面零成本。
 * - 复用 App.vue 已有的 body.low-gpu 弱 GPU 检测结果与 prefers-reduced-motion
 *   系统偏好：命中任一护栏时所有入口直接跳过（元素保持可见、无新增开销）。
 * - 不替换存量 CSS transition；本模块只服务 GSAP 编排类动画
 *   （stagger 入场、反馈震颤），逐帧走 transform/opacity 合成器属性。
 *
 * 类型说明：gsap 的类型定义通过全局命名空间 gsap 暴露（gsap.TweenVars 等），
 * 由下方动态 import('gsap') 触发声明文件加载，无需静态 import type。
 */

/** Sumeru tokens 三条动效曲线对应的 GSAP ease 映射（取最接近的内置曲线）。 */
export const sumeruEases = {
  out: 'power3.out',          // --ease-out: cubic-bezier(0.16, 1, 0.3, 1)
  smooth: 'power2.inOut',     // --ease-smooth: cubic-bezier(0.4, 0, 0.2, 1)
  spring: 'back.out(1.4)',    // --ease-spring: cubic-bezier(0.2, 0.85, 0.25, 1.15)
} as const

let gsapPromise: Promise<typeof import('gsap')['gsap']> | null = null

/** 动效是否被环境护栏拦截（低性能设备 / 用户系统关闭动画）。 */
export function motionBlocked(): boolean {
  if (typeof window === 'undefined') return true
  return (
    document.body.classList.contains('low-gpu') ||
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  )
}

/** 加载 gsap 核心；护栏命中时返回 null，调用方跳过动画。 */
export function loadGsap(): Promise<typeof import('gsap')['gsap'] | null> {
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
