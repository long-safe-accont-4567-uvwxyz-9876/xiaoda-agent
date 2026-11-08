/**
 * 列表分页的通用小工具（审查 M3：画廊固定 limit=48 不翻页）。
 * 后端 /media/gallery 支持 page(0 基) 与 limit≤100，前端只取了第一页。
 */

/** 返回满页说明可能还有下一页（少一页时停止翻页请求） */
export function hasMoreAfterPage(returned: number, pageSize: number): boolean {
  return pageSize > 0 && returned >= pageSize
}

export function nextPage(page: number): number {
  return Number.isFinite(page) && page >= 0 ? page + 1 : 0
}
