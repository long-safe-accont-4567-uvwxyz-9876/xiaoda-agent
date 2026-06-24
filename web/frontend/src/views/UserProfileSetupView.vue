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

// 热门时区列表
const timezones = [
  { value: 'Asia/Shanghai', label: '中国标准时间 (UTC+8)' },
  { value: 'Asia/Hong_Kong', label: '香港时间 (UTC+8)' },
  { value: 'Asia/Taipei', label: '台北时间 (UTC+8)' },
  { value: 'Asia/Tokyo', label: '日本标准时间 (UTC+9)' },
  { value: 'Asia/Seoul', label: '韩国标准时间 (UTC+9)' },
  { value: 'Asia/Singapore', label: '新加坡时间 (UTC+8)' },
  { value: 'Asia/Bangkok', label: '泰国时间 (UTC+7)' },
  { value: 'Asia/Kolkata', label: '印度时间 (UTC+5:30)' },
  { value: 'Asia/Dubai', label: '迪拜时间 (UTC+4)' },
  { value: 'Europe/London', label: '伦敦时间 (UTC+0)' },
  { value: 'Europe/Paris', label: '巴黎时间 (UTC+1)' },
  { value: 'Europe/Berlin', label: '柏林时间 (UTC+1)' },
  { value: 'Europe/Moscow', label: '莫斯科时间 (UTC+3)' },
  { value: 'America/New_York', label: '纽约时间 (UTC-5)' },
  { value: 'America/Chicago', label: '芝加哥时间 (UTC-6)' },
  { value: 'America/Denver', label: '丹佛时间 (UTC-7)' },
  { value: 'America/Los_Angeles', label: '洛杉矶时间 (UTC-8)' },
  { value: 'America/Sao_Paulo', label: '圣保罗时间 (UTC-3)' },
  { value: 'Australia/Sydney', label: '悉尼时间 (UTC+10)' },
  { value: 'Pacific/Auckland', label: '奥克兰时间 (UTC+12)' },
]

// 默认值（作为示例，用户可编辑）
const defaultFields = {
  address_term: '爸爸',
  name: '',
  device: '',
  timezone: 'Asia/Shanghai',
  preferred_personality: '纳西妲，小吉祥草王风格',
  preferred_tone: '温柔、软萌、清晰、有陪伴感',
  like_to_be_called: '爸爸',
  liked_reply_style: '有条理、能直接执行的方案',
  disliked_reply_style: '冷冰冰、敷衍或只有抽象建议的回答',
  project_preferences: '- 修改代码前先理解现有结构\n- 尽量不要大改项目，优先最小修改\n- 优先解决实际报错\n- 命令和路径要写清楚\n- 遇到危险操作要提醒确认',
  history_notes: '',
}

const fields = ref({ ...defaultFields })

onMounted(async () => {
  try {
    const data = await getSetupVersion()
    version.value = data.version || 'dev'
  } catch { /* 降级为 dev */ }

  try {
    const data = await api.getSetupUserProfile()
    // 用 API 返回的数据覆盖默认值（保留默认值中 API 没返回的字段）
    for (const key of Object.keys(defaultFields) as (keyof typeof defaultFields)[]) {
      if (data[key] !== undefined && data[key] !== '') {
        (fields.value as any)[key] = data[key]
      }
    }
  } catch (e: any) {
    console.error('[UserProfileSetup] load failed:', e)
    // 加载失败时使用默认值
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
          <p class="subtitle">初次见面，请多指教</p>
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

          <div class="form-group">
            <label class="form-label">时区</label>
            <select v-model="fields.timezone" class="dendro-input dendro-select">
              <option v-for="tz in timezones" :key="tz.value" :value="tz.value">
                {{ tz.label }}
              </option>
            </select>
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

.dendro-select {
  cursor: pointer;
  appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8' viewBox='0 0 12 8'%3E%3Cpath fill='%237fd650' d='M6 8L0 0h12z'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 14px center;
  padding-right: 36px;
}

.dendro-select option {
  background: #0f1f17;
  color: var(--moon);
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
