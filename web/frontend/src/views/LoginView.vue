<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useAgentsStore } from '../stores/agents'
import { useRouter } from 'vue-router'
import { api, get } from '../api'
import Tilt3D from '../components/fx/Tilt3D.vue'
import DendroEmblem from '../components/fx/DendroEmblem.vue'
import { t } from '../i18n'

const DEFAULT_BG = '/assets/webui_background.jpg'

const auth = useAuthStore()
const agentsStore = useAgentsStore()
const router = useRouter()
const password = ref('')
const error = ref('')
const loading = ref(false)
const noPassword = ref(false)
const loginBg = computed(() => agentsStore.mainWallpaper || DEFAULT_BG)

onMounted(async () => {
  try {
    const data = await get<{ wallpaper?: string }>('/agents/public-wallpaper')
    if (data?.wallpaper) {
      agentsStore.mainWallpaper = data.wallpaper
    }
  } catch {
    // 忽略，使用默认背景
  }
  try {
    const data = await api.getSetupFirstRun()
    if (data?.first_run) {
      router.replace('/setup')
      return
    }
  } catch {
    // 忽略
  }
  // 检测是否设置了密码
  try {
    const result = await api.login('')
    if (result.token) {
      noPassword.value = true
    }
  } catch {
    noPassword.value = false
  }
})

async function handleLogin() {
  error.value = ''
  loading.value = true
  try {
    // 无密码时传空字符串，后端会自动放行
    await auth.login(password.value)
    // 登录成功后检查用户资料是否完成
    try {
      const data = await api.getSetupFirstRun()
      if (data?.first_run) {
        router.replace('/setup')
        return
      }
      if (!data?.profile_done) {
        router.replace('/setup/profile')
        return
      }
    } catch {
      // 检查失败，走正常流程
    }
    router.replace('/')
  } catch (e: any) {
    error.value = e.message || t('login.loginFailed')
  } finally {
    loading.value = false
  }
}

// ── 主人找回密码 ───────────────────────────────────────────
const showRecover = ref(false)
const recoverQuestion = ref('')
const recoverHasQuestion = ref(false)
const recoverAnswer = ref('')
const recoverNewPassword = ref('')
const recoverLoading = ref(false)
const recoverSubmitting = ref(false)
const recoverError = ref('')
const recoverOk = ref(false)

async function openRecover() {
  showRecover.value = true
  recoverError.value = ''
  recoverOk.value = false
  recoverAnswer.value = ''
  recoverNewPassword.value = ''
  recoverQuestion.value = ''
  recoverHasQuestion.value = false
  recoverLoading.value = true
  try {
    const data = await api.getRecoverQuestion()
    recoverQuestion.value = data?.question || ''
    recoverHasQuestion.value = !!data?.has_question
    if (!recoverHasQuestion.value) {
      recoverError.value = t('login.recoverNoQuestion')
    }
  } catch (e: any) {
    recoverError.value = e.message || t('login.recoverNoQuestion')
  } finally {
    recoverLoading.value = false
  }
}

function closeRecover() {
  showRecover.value = false
  recoverOk.value = false
  recoverError.value = ''
}

async function submitRecover() {
  recoverError.value = ''
  if (!recoverAnswer.value.trim()) {
    recoverError.value = t('login.recoverAnswerRequired')
    return
  }
  if ((recoverNewPassword.value || '').length < 8) {
    recoverError.value = t('login.recoverPasswordTooShort')
    return
  }
  recoverSubmitting.value = true
  try {
    await api.recoverPassword(recoverAnswer.value.trim(), recoverNewPassword.value)
    recoverOk.value = true
    recoverAnswer.value = ''
    recoverNewPassword.value = ''
    error.value = ''
    password.value = ''
  } catch (e: any) {
    recoverError.value = e.message || t('login.loginFailed')
  } finally {
    recoverSubmitting.value = false
  }
}
</script>

<template>
  <div class="login-page app-bg" :style="{ backgroundImage: `var(--backdrop-tint), url('${loginBg}')` }">
    <Tilt3D :max-x="5" :max-y="7">
      <div class="login-card glass-panel shimmer-band">
        <span class="vine corner-tl"></span>
        <span class="vine corner-br"></span>

        <div class="login-header">
          <DendroEmblem :size="84" spin class="animate-float" />
          <h1 class="login-title">{{ t('login.title') }}</h1>
          <p class="subtitle">{{ t('login.subtitle') }}</p>
        </div>

        <form @submit.prevent="handleLogin" class="login-form">
          <input
            v-if="!noPassword"
            v-model="password"
            type="password"
            class="dendro-input"
            :placeholder="t('login.passwordPlaceholder')"
            :disabled="loading"
            autofocus
          />
          <p v-if="noPassword" class="hint-text">{{ t('login.noPassword') }}</p>
          <p v-if="error" class="error-text">{{ error }}</p>
          <button type="submit" class="dendro-btn login-btn" :disabled="loading">
            {{ loading ? t('login.connecting') : t('login.enter') }}
          </button>
          <button
            v-if="!noPassword"
            type="button"
            class="recover-link"
            :disabled="loading"
            @click="openRecover"
          >
            {{ t('login.recoverLink') }}
          </button>
        </form>
      </div>
    </Tilt3D>

    <!-- 找回密码弹窗 -->
    <div v-if="showRecover" class="recover-overlay" @click.self="closeRecover">
      <div class="recover-modal glass-panel">
        <h2 class="recover-title">{{ t('login.recoverTitle') }}</h2>
        <template v-if="recoverOk">
          <p class="recover-ok">{{ t('login.recoverOk') }}</p>
          <button class="dendro-btn login-btn" @click="closeRecover">{{ t('cancel') }}</button>
        </template>
        <template v-else>
          <p v-if="recoverLoading" class="hint-text">{{ t('login.recoverLoading') }}</p>
          <template v-else-if="recoverHasQuestion">
            <p class="hint-text">{{ t('login.recoverHint') }}</p>
            <div class="recover-question">
              <span class="recover-q-label">{{ t('login.recoverQuestionLabel') }}</span>
              <span class="recover-q-text">{{ recoverQuestion }}</span>
            </div>
            <div class="recover-form">
              <input
                v-model="recoverAnswer"
                type="password"
                class="dendro-input"
                :placeholder="t('login.recoverAnswerPlaceholder')"
                :disabled="recoverSubmitting"
              />
              <input
                v-model="recoverNewPassword"
                type="password"
                class="dendro-input"
                :placeholder="t('login.recoverNewPasswordPlaceholder')"
                :disabled="recoverSubmitting"
              />
              <p v-if="recoverError" class="error-text">{{ recoverError }}</p>
              <div class="recover-actions">
                <button
                  type="button"
                  class="dendro-btn recover-cancel"
                  :disabled="recoverSubmitting"
                  @click="closeRecover"
                >
                  {{ t('cancel') }}
                </button>
                <button
                  type="button"
                  class="dendro-btn login-btn"
                  :disabled="recoverSubmitting"
                  @click="submitRecover"
                >
                  {{ recoverSubmitting ? t('login.recoverSubmitting') : t('login.recoverSubmit') }}
                </button>
              </div>
            </div>
          </template>
          <p v-if="recoverError && !recoverHasQuestion" class="error-text">{{ recoverError }}</p>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
}

.login-card {
  width: 384px;
  max-width: 92vw;
  padding: 44px 36px;
  text-align: center;
  position: relative;
  overflow: hidden;
}

/* 藤蔓角饰 */
.vine {
  position: absolute;
  width: 90px;
  height: 90px;
  pointer-events: none;
  background:
    radial-gradient(circle at 0 0, transparent 56px, rgba(127, 214, 80, 0.35) 57px, transparent 59px),
    radial-gradient(circle at 14px 14px, transparent 40px, rgba(232, 213, 163, 0.25) 41px, transparent 43px);
}
.corner-tl { top: 0; left: 0; }
.corner-br { bottom: 0; right: 0; transform: rotate(180deg); }

.login-title {
  font-size: 24px;
  margin: 18px 0 6px;
  font-weight: 700;
  letter-spacing: 3px;
  font-family: 'Noto Serif SC', serif;
  background: var(--gradient-dendro, linear-gradient(135deg, #b8ff85, #8fe560 45%, #4fd6a5));
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: transparent;
  filter: drop-shadow(0 0 14px rgba(143, 229, 96, 0.35));
}

.subtitle {
  color: var(--wisdom);
  font-size: 13px;
  margin-bottom: 30px;
  font-family: 'Noto Serif SC', serif;
  opacity: 0.85;
}

.login-form { display: flex; flex-direction: column; gap: 16px; }

.login-form .dendro-input {
  width: 100%;
  padding: 12px 16px;
  font-size: 15px;
  text-align: center;
  letter-spacing: 2px;
}

.login-btn { width: 100%; padding: 12px; font-size: 16px; margin-top: 6px; }

.error-text { color: var(--alert); font-size: 13px; }
.hint-text { color: var(--wisdom); font-size: 13px; opacity: 0.7; }

/* 找回密码 */
.recover-link {
  background: none;
  border: none;
  color: var(--cyan, #67e8f9);
  font-size: 12.5px;
  cursor: pointer;
  margin-top: 4px;
  opacity: 0.85;
  text-decoration: underline;
  text-underline-offset: 3px;
  transition: opacity 0.2s;
}
.recover-link:hover { opacity: 1; }
.recover-link:disabled { opacity: 0.4; cursor: not-allowed; }

.recover-overlay {
  position: fixed;
  inset: 0;
  z-index: 100;
  background: rgba(5, 12, 8, 0.72);
  backdrop-filter: blur(6px);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.recover-modal {
  width: 384px;
  max-width: 94vw;
  padding: 32px 30px;
  text-align: center;
  border-radius: 16px;
}
.recover-title {
  font-size: 19px;
  margin: 0 0 14px;
  font-weight: 700;
  letter-spacing: 2px;
  font-family: 'Noto Serif SC', serif;
  color: var(--dendro);
  text-shadow: 0 0 14px rgba(127, 214, 80, 0.35);
}
.recover-question {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: center;
  margin: 6px 0 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.25);
}
.recover-q-label { font-size: 11.5px; color: var(--moon-dim); }
.recover-q-text { font-size: 14px; color: var(--moon); line-height: 1.5; word-break: break-all; }
.recover-form { display: flex; flex-direction: column; gap: 12px; }
.recover-form .dendro-input { width: 100%; padding: 11px 14px; font-size: 14px; text-align: center; letter-spacing: 1px; }
.recover-actions { display: flex; gap: 10px; }
.recover-actions .login-btn { flex: 1; padding: 11px; font-size: 14px; margin-top: 0; }
.recover-cancel {
  flex: 0 0 auto;
  padding: 11px 20px;
  font-size: 14px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--moon);
  border: 1px solid var(--glass-border);
}
.recover-ok { color: var(--dendro); font-size: 14px; margin: 8px 0 18px; line-height: 1.6; }
.recover-modal .login-btn { width: 100%; margin-top: 14px; }
</style>