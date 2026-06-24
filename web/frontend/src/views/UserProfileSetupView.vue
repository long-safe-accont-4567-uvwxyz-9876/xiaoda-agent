<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import GlslHills from '../components/fx/GlslHills.vue'
import DendroEmblem from '../components/fx/DendroEmblem.vue'
import { api, getSetupVersion } from '../api'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const authStore = useAuthStore()
const version = ref('dev')
const saving = ref(false)
const error = ref('')
const success = ref(false)

const fields = ref({
  address_term: '',
  name: '',
  device: '',
  timezone: '',
  preferred_personality: '',
  preferred_tone: '',
  like_to_be_called: '',
  liked_reply_style: '',
  disliked_reply_style: '',
  project_preferences: '',
  history_notes: '',
})

onMounted(async () => {
  try {
    const data = await getSetupVersion()
    version.value = data.version || 'dev'
  } catch { /* 降级为 dev */ }

  try {
    const data = await api.getSetupUserProfile()
    Object.assign(fields.value, data)
  } catch (e: any) {
    console.error('[UserProfileSetup] load failed:', e)
  }
})

async function handleSave() {
  saving.value = true
  error.value = ''
  success.value = false
  try {
    await api.saveSetupUserProfile(fields.value)
    success.value = true
    setTimeout(() => {
      router.replace('/')
    }, 1200)
  } catch (e: any) {
    error.value = e.message || '保存失败'
  } finally {
    saving.value = false
  }
}

function handleSkip() {
  router.replace('/')
}
</script>

<template>
  <div class="setup-page">
    <GlslHills />
    <div class="setup-center">
      <div class="setup-card glass-panel">
        <span class="vine corner-tl"></span>
        <span class="vine corner-br"></span>

        <div class="setup-header">
          <DendroEmblem :size="84" spin />
          <h1>个人资料 · 偏好设置</h1>
          <p class="subtitle">告诉我关于你的一切</p>
          <p class="version-tag">v{{ version }}</p>
        </div>

        <div class="setup-body">
          <h2 class="section-title">── 用户信息 ──</h2>

          <div class="form-group">
            <label class="form-label">称呼</label>
            <input
              v-model="fields.address_term"
              class="dendro-input"
              type="text"
              placeholder="如：主人 / 爸爸 / 你的名字"
            />
          </div>

          <div class="form-group">
            <label class="form-label">姓名</label>
            <input
              v-model="fields.name"
              class="dendro-input"
              type="text"
              placeholder="你的真实姓名（可选）"
            />
          </div>

          <div class="form-row">
            <div class="form-group">
              <label class="form-label">设备</label>
              <input
                v-model="fields.device"
                class="dendro-input"
                type="text"
                placeholder="自动检测"
                readonly
              />
            </div>
            <div class="form-group">
              <label class="form-label">时区</label>
              <input
                v-model="fields.timezone"
                class="dendro-input"
                type="text"
                placeholder="Asia/Shanghai"
              />
            </div>
          </div>

          <h2 class="section-title section-gap">── 助手人格 ──</h2>

          <div class="form-group">
            <label class="form-label">偏好的助手人格</label>
            <input
              v-model="fields.preferred_personality"
              class="dendro-input"
              type="text"
              placeholder="如：温柔聪慧 / 活泼可爱"
            />
          </div>

          <div class="form-group">
            <label class="form-label">偏好语气</label>
            <input
              v-model="fields.preferred_tone"
              class="dendro-input"
              type="text"
              placeholder="如：温柔、软萌、清晰"
            />
          </div>

          <div class="form-group">
            <label class="form-label">喜欢被称呼为</label>
            <input
              v-model="fields.like_to_be_called"
              class="dendro-input"
              type="text"
              placeholder="如：爸爸 / 主人 / 朋友"
            />
          </div>

          <h2 class="section-title section-gap">── 回复偏好 ──</h2>

          <div class="form-group">
            <label class="form-label">喜欢的回复风格</label>
            <textarea
              v-model="fields.liked_reply_style"
              class="dendro-input dendro-textarea"
              placeholder="如：有条理、能直接执行的方案"
              rows="2"
            ></textarea>
          </div>

          <div class="form-group">
            <label class="form-label">不喜欢的回复风格</label>
            <textarea
              v-model="fields.disliked_reply_style"
              class="dendro-input dendro-textarea"
              placeholder="如：冷冰冰、敷衍、只有抽象建议"
              rows="2"
            ></textarea>
          </div>

          <h2 class="section-title section-gap">── 项目偏好 ──</h2>

          <div class="form-group">
            <label class="form-label">项目偏好（每行一条）</label>
            <textarea
              v-model="fields.project_preferences"
              class="dendro-input dendro-textarea"
              placeholder="- 修改代码前先理解现有结构&#10;- 尽量不要大改项目，优先最小修改"
              rows="5"
            ></textarea>
          </div>

          <p v-if="error" class="error-text">{{ error }}</p>
          <p v-if="success" class="success-text">保存成功！正在进入主界面…</p>

          <div class="action-row">
            <button class="dendro-btn skip-btn" @click="handleSkip" :disabled="saving">
              跳过
            </button>
            <button
              class="dendro-btn save-btn"
              :disabled="saving"
              @click="handleSave"
            >
              {{ saving ? '草元素汇聚中…' : '保存并进入' }}
            </button>
          </div>

          <p class="status-hint">
            这些信息将保存在 USER.md 中，帮助纳西妲更好地了解你
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.setup-page {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
}

.setup-center {
  position: relative;
  z-index: 1;
  width: 100%;
  display: flex;
  justify-content: center;
  padding: 20px;
}

.setup-card {
  width: 640px;
  max-width: 100%;
  max-height: 90vh;
  overflow-y: auto;
  padding: 40px 36px;
  text-align: center;
  position: relative;
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

.setup-header h1 {
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
  margin-bottom: 6px;
  font-family: 'Noto Serif SC', serif;
  opacity: 0.85;
}

.version-tag {
  color: var(--moon-dim);
  font-size: 11px;
  margin-bottom: 24px;
  opacity: 0.5;
}

.setup-body {
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.section-title {
  font-size: 14px;
  font-family: 'Noto Serif SC', serif;
  letter-spacing: 2px;
  margin: 0;
  color: var(--dendro);
}

.section-gap {
  margin-top: 12px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-row {
  display: flex;
  gap: 12px;
}

.form-row .form-group {
  flex: 1;
}

.form-label {
  font-size: 12px;
  color: var(--moon-dim);
  font-family: 'Noto Sans SC', sans-serif;
  letter-spacing: 1px;
}

.dendro-input {
  width: 100%;
  padding: 10px 14px;
  background: rgba(15, 31, 23, 0.6);
  border: 1px solid rgba(127, 214, 80, 0.18);
  border-radius: 8px;
  color: var(--moon);
  font-size: 14px;
  font-family: 'Noto Sans SC', sans-serif;
  transition: border-color 0.2s, box-shadow 0.2s;
  box-sizing: border-box;
}

.dendro-input:focus {
  outline: none;
  border-color: var(--dendro);
  box-shadow: 0 0 0 2px rgba(127, 214, 80, 0.15);
}

.dendro-input::placeholder {
  color: rgba(242, 247, 238, 0.3);
}

.dendro-input[readonly] {
  opacity: 0.6;
  cursor: default;
}

.dendro-textarea {
  resize: vertical;
  min-height: 36px;
  font-family: 'Noto Sans SC', sans-serif;
  line-height: 1.6;
}

.error-text {
  color: var(--alert);
  font-size: 13px;
  margin: 0;
  text-align: center;
}

.success-text {
  color: var(--dendro);
  font-size: 13px;
  margin: 0;
  text-align: center;
}

.action-row {
  display: flex;
  gap: 12px;
  margin-top: 6px;
}

.skip-btn {
  flex: 0 0 120px;
  height: 44px;
  font-size: 14px;
  opacity: 0.7;
}

.save-btn {
  flex: 1;
  height: 44px;
  font-size: 16px;
}

.save-btn:disabled,
.skip-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
}

.status-hint {
  color: var(--moon-dim);
  font-size: 12px;
  text-align: center;
  margin: 0;
  opacity: 0.7;
}
</style>
