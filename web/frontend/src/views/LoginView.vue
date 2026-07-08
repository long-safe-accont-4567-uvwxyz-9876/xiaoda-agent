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
    // 尝试空密码登录来检测
    const resp = await fetch('/api/v1/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: '' }),
    })
    if (resp.ok) {
      // 无密码，自动获取 token
      const result = await resp.json()
      if (result.data?.token) {
        noPassword.value = true
      }
    } else {
      noPassword.value = false
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
</script>

<template>
  <div class="login-page app-bg" :style="{ backgroundImage: `var(--backdrop-tint), url('${loginBg}')` }">
    <Tilt3D :max-x="5" :max-y="7">
      <div class="login-card glass-panel">
        <span class="vine corner-tl"></span>
        <span class="vine corner-br"></span>

        <div class="login-header">
          <DendroEmblem :size="84" spin />
          <h1>{{ t('login.title') }}</h1>
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
        </form>
      </div>
    </Tilt3D>
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

.login-header h1 {
  color: var(--dendro);
  font-size: 24px;
  margin: 18px 0 6px;
  font-weight: 700;
  letter-spacing: 3px;
  font-family: 'Noto Serif SC', serif;
  text-shadow: 0 0 18px rgba(127, 214, 80, 0.35);
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
</style>