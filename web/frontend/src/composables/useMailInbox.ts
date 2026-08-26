/**
 * 收件箱预览组合式函数 —— 从 MailView 抽出（2026-08-23 大文件拆分专项）。
 */
import { ref } from 'vue'
import { get } from '../api'
import { useMessage } from 'naive-ui'
import { t } from '../i18n'
import type { InboxMail } from '../components/mail/types'

export function useMailInbox() {
  const message = useMessage()
  const inbox = ref<InboxMail[]>([])
  const inboxLoading = ref(false)

  async function loadInbox() {
    inboxLoading.value = true
    try {
      const res = await get<{ data: InboxMail[]; pagination: any }>('/mail/inbox?limit=10')
      inbox.value = Array.isArray(res?.data) ? res.data : []
    } catch (e: any) {
      message.error(e.message || t('mailView.loadFailed'))
    } finally {
      inboxLoading.value = false
    }
  }

  return { inbox, inboxLoading, loadInbox }
}
