/**
 * 邮箱页纯展示工具：时间格式化与发件人显示（从 MailView 原样迁移）。
 */
import { t } from '../../i18n'
import type { InboxMail } from './types'

export function fmtTime(ts: string | null): string {
  if (!ts) return t('mailView.neverPolled')
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function senderDisplay(m: InboxMail): string {
  const name = m.from?.name?.trim()
  const email = m.from?.email?.trim()
  if (name && email) return `${name} <${email}>`
  return email || name || '—'
}
