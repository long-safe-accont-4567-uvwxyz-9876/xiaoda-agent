/**
 * 日期/时间格式化的单一出口（审查 E1）。
 *
 * 原先 13 处直接 `toLocaleString('zh-CN')` 把语言写死：切到英文后时间仍然是
 * 中文格式，JSpace 面板更是直接 `toLocaleTimeString()` 跟随系统而不是应用语言。
 * 这里统一按 i18n 当前 locale 取 BCP-47 标签，组件只调这三个函数。
 */
import { getLang } from '../i18n'

/** 当前 UI 语言对应的 BCP-47 标签（显式传 lang 便于测试） */
export function localeTag(lang: string = getLang()): string {
  return lang === 'en' ? 'en-US' : 'zh-CN'
}

/** 完整日期时间（表格、历史记录行） */
export function formatDateTime(ts: number, tag: string = localeTag()): string {
  return new Date(ts).toLocaleString(tag)
}

/** 只要时分（聊天时间戳、活动流） */
export function formatTime(ts: number, tag: string = localeTag()): string {
  return new Date(ts).toLocaleTimeString(tag, { hour: '2-digit', minute: '2-digit' })
}

/** 月-日 时:分（经验条、今日面板这类紧凑位置） */
export function formatMonthDayTime(ts: number, tag: string = localeTag()): string {
  return new Date(ts).toLocaleString(tag, {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}
