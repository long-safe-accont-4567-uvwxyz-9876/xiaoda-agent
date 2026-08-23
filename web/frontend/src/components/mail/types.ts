/**
 * MailView 拆解后的共享类型（2026-08-23 大文件拆分专项）。
 * 与后端 web/routers/mail.py 的响应体一一对应。
 */
export interface MailConfig {
  enabled: boolean
  mode: 'off' | 'allowlist' | 'all'
  allowed_senders: string[]
  owner_email: string
  reply_channel: 'mail' | 'mail_and_qq'
  max_per_day: number
  dnd_start: number  // 免打扰开始小时（0-23），0+0=不启用
  dnd_end: number    // 免打扰结束小时（0-23）
}

export interface MailStats {
  enabled: boolean
  mode: string
  daily_count: number
  max_per_day: number
  processed_total: number
  last_poll_time: string | null
}

export interface InboxMail {
  message_id: string
  subject: string
  from: { email: string; name: string }
  created_at: string
  is_read: boolean
}

export interface AuthStatus {
  installed: boolean
  cli_path: string | null
  authorized: boolean
  email: string
  error: string
}
