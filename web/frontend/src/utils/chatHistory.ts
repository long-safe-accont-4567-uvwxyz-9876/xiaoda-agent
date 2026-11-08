/**
 * 会话历史分页与切换回滚（审查 B1 / B2）。
 *
 * B1：chat.ts 只调 getMessages(sid) 拿最近 50 轮，后端 chat.py 的 before 时间
 *     游标与 limit≤200 从未被用上——长会话早期消息不可达。
 * B2：loadSession 先 switchSession + set_session 再 await 历史，失败不回滚，
 *     界面标题已变而消息还是上一个会话的。
 *
 * 纯逻辑集中在这里，store 只做编排，便于 node 单测覆盖。
 */

/** 后端 before 是秒级浮点时间戳；取当前最早一条消息的时间作为游标 */
export function olderCursorSeconds(oldestTimestampMs: number): number {
  if (!Number.isFinite(oldestTimestampMs) || oldestTimestampMs <= 0) return 0
  return oldestTimestampMs / 1000
}

/** 把更早的一页合并到列表头部，按 id 去重（后端两页边界可能重叠） */
export function mergeOlderPage<T extends { id: string }>(existing: T[], incoming: T[]): T[] {
  if (!incoming.length) return existing
  const seen = new Set(existing.map(item => item.id))
  const fresh = incoming.filter(item => !seen.has(item.id))
  if (!fresh.length) return existing
  return [...fresh, ...existing]
}

/** 返回条数小于 limit 说明已经到底（配合 before 游标翻页） */
export function pageExhausted(returned: number, limit: number): boolean {
  return returned < limit
}

/**
 * 历史加载失败后要回到哪个会话：只有"仍是最近一次切换"才回滚，
 * 迟到的旧请求不得把用户刚切过去的新会话顶掉。
 */
export function rollbackSessionId(
  previous: string,
  attempted: string,
  generation: number,
  latestGeneration: number,
): string | null {
  if (!previous || previous === attempted) return null
  if (generation !== latestGeneration) return null
  return previous
}
