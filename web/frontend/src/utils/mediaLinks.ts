/**
 * 媒体资源的两处卫生问题（审查 M5）。
 *
 * 1) `window.open(url, '_blank')` 不带 noopener：新窗口能通过 window.opener
 *    反向操作打开页（反向 tabnabbing）。
 * 2) 删除画廊项时把文件名直接拼进 DELETE 路径而未 encodeURIComponent：
 *    名字里带 `#`（会被当成 fragment）或 `?` 的素材会删错或 404。
 */

/** 保留原窗口引用关系的特性串（noreferrer 同时屏蔽 Referer） */
export const SAFE_WINDOW_FEATURES = 'noopener,noreferrer'

type OpenFn = (url: string, target: string, features: string) => unknown

/** 安全地在新标签页打开外链；open 可注入，便于单测 */
export function openExternal(url: string, open?: OpenFn): void {
  const opener: OpenFn = open ?? ((u, t, f) => window.open(u, t, f))
  opener(url, '_blank', SAFE_WINDOW_FEATURES)
}

/** 画廊删除路径：type 与 name 都必须编码（name 里可能有空格/#/?/中文） */
export function galleryDeletePath(type: string, name: string): string {
  return `/media/gallery/${encodeURIComponent(type)}/${encodeURIComponent(name)}`
}
