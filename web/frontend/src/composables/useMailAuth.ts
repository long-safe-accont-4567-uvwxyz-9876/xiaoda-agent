/**
 * 邮箱授权状态组合式函数 —— 从 MailView 抽出（2026-08-23 大文件拆分专项）。
 * 授权检测/发起授权/重授权检查/跳转对话窗；onAuthorized 在授权确认后由外部
 * 注入（原实现内联调用 loadInbox，时序保持一致）。
 */
import { computed, onScopeDispose, ref } from 'vue'
import { useRouter } from 'vue-router'
import { get, post } from '../api'
import { useMessage } from 'naive-ui'
import { t } from '../i18n'
import type { AuthStatus } from '../components/mail/types'

export function useMailAuth(options: { onAuthorized: () => void }) {
  const message = useMessage()
  const router = useRouter()

  const authStatus = ref<AuthStatus | null>(null)
  const authChecking = ref(false)
  const authLogging = ref(false)
  const authUrl = ref('')
  let authCheckTimer: ReturnType<typeof setTimeout> | null = null

  async function loadAuthStatus() {
    authChecking.value = true
    try {
      authStatus.value = await get<AuthStatus>('/mail/auth-status')
    } catch (e: any) {
      authStatus.value = {
        installed: false,
        cli_path: null,
        authorized: false,
        email: '',
        error: e.message || t('mailView.checkFailed'),
      }
    } finally {
      authChecking.value = false
    }
  }

  async function triggerAuthLogin() {
    authLogging.value = true
    authUrl.value = ''
    try {
      const res = await post<{ started: boolean; message: string; auth_url?: string; cli_path: string | null }>('/mail/auth-login')
      if (res.started) {
        if (res.auth_url) {
          // 服务器环境，返回授权 URL 让用户手动打开
          authUrl.value = res.auth_url
          authLogging.value = false
        } else {
          message.info(res.message || t('mailView.authBrowserOpened'))
          // 等待 5 秒后检查授权状态
          authCheckTimer = window.setTimeout(async () => {
            try {
              await loadAuthStatus()
              if (authStatus.value?.authorized) {
                message.success(t('mailView.authSuccess'))
                options.onAuthorized()
              }
            } catch (_) {
            } finally {
              authLogging.value = false
            }
          }, 5000)
        }
      } else {
        message.error(res.message || t('mailView.authFailed'))
        authLogging.value = false
      }
    } catch (e: any) {
      message.error(e.message || t('mailView.authFailed'))
      authLogging.value = false
    }
  }

  async function checkAuthAfterReauth() {
    authChecking.value = true
    try {
      await loadAuthStatus()
      if (authStatus.value?.authorized) {
        authUrl.value = ''
        message.success(t('mailView.authSuccess'))
        options.onAuthorized()
      } else {
        message.warning('授权尚未完成，请在浏览器中完成扫码授权后重试')
      }
    } catch (_) {
    } finally {
      authChecking.value = false
    }
  }

  function goToChat() {
    router.push({ name: 'chat' })
  }

  // 邮箱连接状态：0=未安装 1=未授权 2=已授权
  const authStep = computed(() => {
    if (!authStatus.value) return -1
    if (!authStatus.value.installed) return 0
    if (!authStatus.value.authorized) return 1
    return 2
  })

  onScopeDispose(() => {
    if (authCheckTimer) clearTimeout(authCheckTimer)
  })

  return {
    authStatus,
    authChecking,
    authLogging,
    authUrl,
    authStep,
    loadAuthStatus,
    triggerAuthLogin,
    checkAuthAfterReauth,
    goToChat,
  }
}
