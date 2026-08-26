/**
 * 壁纸类型判定工具（前端唯一事实源）
 *
 * 与后端 AgentWallpaperField / AgentBackdrop.layerKind 同规则：
 * video = mp4/webm；html = .html/.htm；其余（含 gif 动图）按 image 处理。
 */

export type WallpaperKind = 'image' | 'video' | 'html'

export function wallpaperKind(url?: string | null): WallpaperKind {
  const u = url || ''
  // (\?|#|$)：带 ?query 或 #fragment（如 a.mp4#t=0,5）也能正确识别
  if (/\.html?(\?|#|$)/i.test(u)) return 'html'
  if (/\.(mp4|webm)(\?|#|$)/i.test(u)) return 'video'
  return 'image'
}
