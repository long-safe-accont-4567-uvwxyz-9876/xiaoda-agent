/**
 * 邮箱配置 + 状态统计组合式函数 —— 从 MailView 抽出（2026-08-23 大文件拆分专项）。
 * 自动保存链路（deep watch → 400ms debounce → PUT，含并发合并与首次加载跳过）
 * 原样保留；saveConfig 成功后刷新 stats 的联动不变。
 */
import { onScopeDispose, ref, watch } from 'vue'
import { get, put } from '../api'
import { useMessage } from 'naive-ui'
import { t } from '../i18n'
import type { MailConfig, MailStats } from '../components/mail/types'

export function useMailSettings() {
  const message = useMessage()

  const config = ref<MailConfig>({
    enabled: false,
    mode: 'off',
    allowed_senders: [],
    owner_email: '',
    reply_channel: 'mail',
    max_per_day: 50,
    dnd_start: 0,
    dnd_end: 0,
  })
  const stats = ref<MailStats | null>(null)
  const configLoading = ref(false)
  const statsLoading = ref(false)
  const saving = ref(false)

  // 自动保存（debounce）：config 变化后延迟 400ms 自动 PUT
  let saveTimer: ReturnType<typeof setTimeout> | null = null
  let savePending = false
  let initialized = false  // 防止首次 loadConfig 触发自动保存

  async function loadConfig() {
    configLoading.value = true
    try {
      const data = await get<MailConfig>('/mail/config')
      config.value = {
        enabled: !!data.enabled,
        mode: data.mode || 'off',
        allowed_senders: Array.isArray(data.allowed_senders) ? data.allowed_senders : [],
        owner_email: data.owner_email || '',
        reply_channel: data.reply_channel || 'mail',
        max_per_day: typeof data.max_per_day === 'number' ? data.max_per_day : 50,
        dnd_start: typeof data.dnd_start === 'number' ? data.dnd_start : 0,
        dnd_end: typeof data.dnd_end === 'number' ? data.dnd_end : 0,
      }
    } catch (e: any) {
      message.error(e.message || t('mailView.loadFailed'))
    } finally {
      configLoading.value = false
    }
  }

  async function loadStats() {
    statsLoading.value = true
    try {
      stats.value = await get<MailStats>('/mail/stats')
    } catch (e: any) {
      message.error(e.message || t('mailView.loadFailed'))
    } finally {
      statsLoading.value = false
    }
  }

  async function saveConfig() {
    if (saving.value) {
      savePending = true
      return
    }
    saving.value = true
    try {
      await put('/mail/config', {
        enabled: config.value.enabled,
        mode: config.value.mode,
        allowed_senders: config.value.allowed_senders,
        owner_email: config.value.owner_email,
        reply_channel: config.value.reply_channel,
        max_per_day: config.value.max_per_day,
        dnd_start: config.value.dnd_start,
        dnd_end: config.value.dnd_end,
      })
      message.success(t('mailView.saved'))
      loadStats()
    } catch (e: any) {
      message.error(e.message || t('mailView.loadFailed'))
    } finally {
      saving.value = false
      if (savePending) {
        savePending = false
        await saveConfig()
      }
    }
  }

  function scheduleSave() {
    if (!initialized) return  // 首次加载跳过
    if (saveTimer) clearTimeout(saveTimer)
    saveTimer = setTimeout(() => { saveConfig() }, 400)
  }

  // 监听 config 变化，自动触发保存
  watch(config, () => scheduleSave(), { deep: true })

  /** 首次 loadConfig 完成后调用，放开自动保存 */
  function setInitialized() {
    initialized = true
  }

  onScopeDispose(() => {
    if (saveTimer) clearTimeout(saveTimer)
  })

  return {
    config,
    stats,
    configLoading,
    statsLoading,
    saving,
    loadConfig,
    loadStats,
    setInitialized,
  }
}
