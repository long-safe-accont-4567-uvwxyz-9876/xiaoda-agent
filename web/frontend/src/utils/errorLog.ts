/**
 * 全局前端错误记录（审计 P0-1）。
 *
 * 背景：main.ts 原先没有 app.config.errorHandler，全仓也没有 onErrorCaptured，
 * 一条脏消息在渲染期抛错就会让整页白屏——用户只看到空白，控制台里也拿不到
 * 结构化上下文（是哪个组件、哪个生命周期阶段、什么时候）。
 *
 * 策略：
 *  1. 统一入口 reportError()，同时写 console 与环形缓冲（默认 50 条）；
 *  2. 缓冲只保留可排查的短字段（组件名/阶段/消息/时间），不保留整段堆栈，
 *     避免把潜在的用户数据长期驻留内存；
 *  3. 面向用户的文案由组件自己给（见 ErrorBoundary / ChatView），
 *     这里只负责"记下来"，绝不把原始堆栈糊到界面上。
 */

export interface ErrorRecord {
  /** 人类可读的短消息（已截断） */
  message: string
  /** Vue 的 info 字段，如 "render function" / "setup function" */
  info: string
  /** 出错的组件名（能拿到时） */
  component: string
  /** 发生时间（毫秒时间戳） */
  at: number
}

const MAX_RECORDS = 50
const MAX_MESSAGE_LEN = 300

const records: ErrorRecord[] = []

function shortMessage(err: unknown): string {
  if (err instanceof Error) return (err.message || err.name || 'Error').slice(0, MAX_MESSAGE_LEN)
  if (typeof err === 'string') return err.slice(0, MAX_MESSAGE_LEN)
  try {
    return String(err).slice(0, MAX_MESSAGE_LEN)
  } catch {
    return 'Unknown error'
  }
}

function componentName(instance: unknown): string {
  if (!instance || typeof instance !== 'object') return ''
  const name = (instance as { $options?: { name?: string } }).$options?.name
  return typeof name === 'string' ? name : ''
}

/**
 * 记录一条前端错误。永远不会抛错——错误上报本身失败不能再制造新错误。
 *
 * @param err 捕获到的错误
 * @param info 生命周期阶段（Vue 的 info 参数）
 * @param instance 出错的组件实例（可选）
 */
export function reportError(err: unknown, info = '', instance?: unknown): ErrorRecord {
  const record: ErrorRecord = {
    message: shortMessage(err),
    info: String(info || ''),
    component: componentName(instance),
    at: Date.now(),
  }
  records.push(record)
  if (records.length > MAX_RECORDS) records.splice(0, records.length - MAX_RECORDS)
  // 保留原始错误对象供 devtools 展开（生产环境也只是 console，不弹窗）
  // eslint-disable-next-line no-console
  console.error(`[app-error]${record.component ? ` <${record.component}>` : ''} ${record.info}`, err)
  return record
}

/** 只读快照：供排查面板/测试断言使用 */
export function getErrorRecords(): readonly ErrorRecord[] {
  return records
}

/** 清空缓冲（切换会话/用户主动重试时调用） */
export function clearErrorRecords(): void {
  records.length = 0
}
