/**
 * 自动播放被浏览器拦截（审查 M4）。
 *
 * ChatView 的 audioEl.play().catch(() => { playingUrl.value = '' }) 只清播放态，
 * 用户看不到任何原因：未与页面交互前触发 autoSpeak，界面像是"什么都没发生"。
 * 这里识别拦截错误，调用方据此给一次可见提示。
 */

/** NotAllowedError 是 Chrome/Safari/Firefox 拦截自动播放的统一信号 */
export function isAutoplayBlocked(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const name = (error as { name?: unknown }).name
  if (name === 'NotAllowedError') return true
  // 个别旧版 WebKit 只给 message
  const message = (error as { message?: unknown }).message
  return typeof message === 'string' && /not allowed|user gesture|autoplay/i.test(message)
}
