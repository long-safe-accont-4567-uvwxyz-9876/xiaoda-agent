<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useRouter } from 'vue-router'
import { api } from '../api'
import Tilt3D from '../components/fx/Tilt3D.vue'
import DendroEmblem from '../components/fx/DendroEmblem.vue'

const auth = useAuthStore()
const router = useRouter()
const password = ref('')
const error = ref('')
const loading = ref(false)

onMounted(async () => {
  try {
    const data = await api.getSetupFirstRun()
    if (data?.first_run) {
      router.replace('/setup')
    }
  } catch {
    // 忽略
  }
})

async function handleLogin() {
  error.value = ''
  loading.value = true
  try {
    await auth.login(password.value)
    router.replace('/')
  } catch (e: any) {
    error.value = e.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page app-bg">
    <Tilt3D :max-x="5" :max-y="7">
      <div class="login-card glass-panel">
        <span class="vine corner-tl"></span>
        <span class="vine corner-br"></span>

        <div class="login-header">
          <DendroEmblem :size="84" spin />
          <h1>纳西妲 · 世界树</h1>
          <p class="subtitle">智慧之神在此恭候，爸爸</p>
        </div>

        <form @submit.prevent="handleLogin" class="login-form">
          <input
            v-model="password"
            type="password"
            class="dendro-input"
            placeholder="输入访问密码…"
            :disabled="loading"
            autofocus
          />
          <p v-if="error" class="error-text">{{ error }}</p>
          <button type="submit" class="dendro-btn login-btn" :disabled="loading">
            {{ loading ? '草元素汇聚中…' : '进入世界树' }}
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
</style>
