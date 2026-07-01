<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import GlslHills from '../components/fx/GlslHills.vue'
import DendroEmblem from '../components/fx/DendroEmblem.vue'
import { api, getSetupVersion } from '../api'
import { useAuthStore } from '../stores/auth'
import { t } from '../i18n'

const router = useRouter()
const authStore = useAuthStore()
const version = ref('dev')
const saving = ref(false)
const error = ref('')
const success = ref(false)

// 热门时区列表
const timezones = computed(() => [
  { value: 'Asia/Shanghai', label: t('userProfileSetup.timezones.Asia/Shanghai') },
  { value: 'Asia/Hong_Kong', label: t('userProfileSetup.timezones.Asia/Hong_Kong') },
  { value: 'Asia/Taipei', label: t('userProfileSetup.timezones.Asia/Taipei') },
  { value: 'Asia/Tokyo', label: t('userProfileSetup.timezones.Asia/Tokyo') },
  { value: 'Asia/Seoul', label: t('userProfileSetup.timezones.Asia/Seoul') },
  { value: 'Asia/Singapore', label: t('userProfileSetup.timezones.Asia/Singapore') },
  { value: 'Asia/Bangkok', label: t('userProfileSetup.timezones.Asia/Bangkok') },
  { value: 'Asia/Kolkata', label: t('userProfileSetup.timezones.Asia/Kolkata') },
  { value: 'Asia/Dubai', label: t('userProfileSetup.timezones.Asia/Dubai') },
  { value: 'Europe/London', label: t('userProfileSetup.timezones.Europe/London') },
  { value: 'Europe/Paris', label: t('userProfileSetup.timezones.Europe/Paris') },
  { value: 'Europe/Berlin', label: t('userProfileSetup.timezones.Europe/Berlin') },
  { value: 'Europe/Moscow', label: t('userProfileSetup.timezones.Europe/Moscow') },
  { value: 'America/New_York', label: t('userProfileSetup.timezones.America/New_York') },
  { value: 'America/Chicago', label: t('userProfileSetup.timezones.America/Chicago') },
  { value: 'America/Denver', label: t('userProfileSetup.timezones.America/Denver') },
  { value: 'America/Los_Angeles', label: t('userProfileSetup.timezones.America/Los_Angeles') },
  { value: 'America/Sao_Paulo', label: t('userProfileSetup.timezones.America/Sao_Paulo') },
  { value: 'Australia/Sydney', label: t('userProfileSetup.timezones.Australia/Sydney') },
  { value: 'Pacific/Auckland', label: t('userProfileSetup.timezones.Pacific/Auckland') },
])

// 默认值（用户可编辑，预填项目默认模板）
const defaultFields = {
  address_term: '',
  name: '',
  device: '',
  timezone: 'Asia/Shanghai',
  preferred_personality: t('userProfileSetup.defaultPersonality'),
  preferred_tone: t('userProfileSetup.defaultTone'),
  like_to_be_called: '',
  liked_reply_style: t('userProfileSetup.defaultLiked'),
  disliked_reply_style: t('userProfileSetup.defaultDisliked'),
  project_preferences: t('userProfileSetup.defaultPrefs'),
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
    // 空字段使用默认值，减少用户输入
    const payload = {
      ...fields.value,
      address_term: fields.value.address_term.trim() || t('userProfileSetup.defaultFriend'),
      name: fields.value.name.trim() || 'User',
      like_to_be_called: fields.value.address_term.trim() || t('userProfileSetup.defaultFriend'),
    }
    await api.saveSetupUserProfile(payload)
    localStorage.setItem('nahida_profile_done', 'true')
    success.value = true
    setTimeout(() => {
      router.replace('/')
    }, 1200)
  } catch (e: any) {
    error.value = e.message || t('userProfileSetup.saveFailed')
  } finally {
    saving.value = false
  }
}

async function handleSkip() {
  // 跳过：标记完成，不再自动弹出
  localStorage.setItem('nahida_profile_done', 'true')
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
          <h1>{{ t('userProfileSetup.title') }}</h1>
          <p class="subtitle">{{ t('userProfileSetup.greeting') }}</p>
          <p class="version-tag">v{{ version }}</p>
        </div>

        <div class="setup-body">
          <h2 class="section-title">── {{ t('userProfileSetup.userInfo') }} ──</h2>

          <div class="form-group">
            <label class="form-label">{{ t('userProfileSetup.addressTerm') }}</label>
            <input
              v-model="fields.address_term"
              class="dendro-input"
              type="text"
              :placeholder="t('userProfileSetup.addressEmptyDefault')"
            />
          </div>

          <div class="form-group">
            <label class="form-label">{{ t('userProfileSetup.nickname') }}</label>
            <input
              v-model="fields.name"
              class="dendro-input"
              type="text"
              :placeholder="t('userProfileSetup.nicknamePh')"
            />
          </div>

          <div class="form-group">
            <label class="form-label">{{ t('userProfileSetup.timezone') }}</label>
            <select v-model="fields.timezone" class="dendro-input dendro-select">
              <option v-for="tz in timezones" :key="tz.value" :value="tz.value">
                {{ tz.label }}
              </option>
            </select>
          </div>

          <h2 class="section-title section-gap">── {{ t('userProfileSetup.agentPersonality') }} ──</h2>

          <div class="form-group">
            <label class="form-label">{{ t('userProfileSetup.preferredPersonality') }}</label>
            <input
              v-model="fields.preferred_personality"
              class="dendro-input"
              type="text"
              :placeholder="t('userProfileSetup.personalityPh')"
            />
          </div>

          <div class="form-group">
            <label class="form-label">{{ t('userProfileSetup.preferredTone') }}</label>
            <input
              v-model="fields.preferred_tone"
              class="dendro-input"
              type="text"
              :placeholder="t('userProfileSetup.tonePh')"
            />
          </div>

          <h2 class="section-title section-gap">── {{ t('userProfileSetup.replyPrefs') }} ──</h2>

          <div class="form-group">
            <label class="form-label">{{ t('userProfileSetup.likedStyle') }}</label>
            <textarea
              v-model="fields.liked_reply_style"
              class="dendro-input dendro-textarea"
              :placeholder="t('userProfileSetup.likedPh')"
              rows="2"
            ></textarea>
          </div>

          <div class="form-group">
            <label class="form-label">{{ t('userProfileSetup.dislikedStyle') }}</label>
            <textarea
              v-model="fields.disliked_reply_style"
              class="dendro-input dendro-textarea"
              :placeholder="t('userProfileSetup.dislikedPh')"
              rows="2"
            ></textarea>
          </div>

          <h2 class="section-title section-gap">── {{ t('userProfileSetup.projectPrefs') }} ──</h2>

          <div class="form-group">
            <label class="form-label">{{ t('userProfileSetup.projectPrefs') }}</label>
            <textarea
              v-model="fields.project_preferences"
              class="dendro-input dendro-textarea"
              :placeholder="t('userProfileSetup.projectPrefsPh')"
              rows="5"
            ></textarea>
          </div>

          <p v-if="error" class="error-text">{{ error }}</p>
          <p v-if="success" class="success-text">{{ t('userProfileSetup.savedSuccess') }}</p>

          <div class="action-row">
            <button class="dendro-btn skip-btn" @click="handleSkip" :disabled="saving">
              {{ t('userProfileSetup.skip') }}
            </button>
            <button
              class="dendro-btn save-btn"
              :disabled="saving"
              @click="handleSave"
            >
              {{ saving ? t('setupWizard.saving') : t('userProfileSetup.saveEnter') }}
            </button>
          </div>

          <p class="status-hint">
            {{ t('userProfileSetup.infoHint') }}
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

.required {
  color: var(--alert);
  font-weight: bold;
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
