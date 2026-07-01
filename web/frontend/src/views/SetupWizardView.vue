<script setup lang="ts">
import { ref, onMounted, computed, reactive } from 'vue'
import { useRouter } from 'vue-router'
import DendroShader from '../components/fx/DendroShader.vue'
import DendroEmblem from '../components/fx/DendroEmblem.vue'
import KeyAccordion, { type TestStatus } from '../components/setup/KeyAccordion.vue'
import { api, getSetupVersion, getDisclaimerStatus, agreeDisclaimer } from '../api'
import { useAuthStore } from '../stores/auth'
import { t } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'

const router = useRouter()
const authStore = useAuthStore()
const version = ref('dev')
const keys = ref<any[]>([])
const updates = ref<Record<string, string>>({})
const saving = ref(false)
const showOptional = ref(false)
const error = ref('')
const testingAll = ref(false)

const testStatuses = reactive<Record<string, TestStatus>>({})
const testMessages = reactive<Record<string, string>>({})
const testedRequiredKeys = ref<Set<string>>(new Set())
const modifiedKeys = ref<Set<string>>(new Set())

// 免责协议状态
const disclaimerAgreed = ref(false)        // 是否已同意（来自后端/localStorage）
const disclaimerChecked = ref(false)        // 当前勾选状态
const disclaimerScrolledToBottom = ref(false)
const disclaimerScrollRef = ref<HTMLElement | null>(null)

onMounted(async () => {
  // 获取版本号
  try {
    const data = await getSetupVersion()
    version.value = data.version || 'dev'
  } catch { /* 降级为 dev */ }

  try {
    const data = await api.getSetupKeys()
    keys.value = data.keys
    // 将已配置的密钥值同步到 updates，这样保存时会包含它们
    for (const k of data.keys) {
      if (k.configured && k.raw_value) {
        updates.value[k.key] = k.raw_value
      }
      // Initialize test statuses
      testStatuses[k.key] = 'untested'
      testMessages[k.key] = ''
    }
  } catch (e: any) {
    console.error('[SetupWizard] getSetupKeys failed:', e)
    // Fallback: 显示硬编码的 key 列表，确保页面不会空白
    keys.value = [
      { key: 'MIMO_API_KEY', label: t('setupWizard.fallback.MIMO_API_KEY.label'), desc: t('setupWizard.fallback.MIMO_API_KEY.desc'), url: 'https://platform.xiaomimimo.com?ref=SU5WDZ', url_desc: t('setupWizard.fallback.MIMO_API_KEY.url_desc'), required: true, configured: false, masked_value: '', raw_value: '' },
      { key: 'QQBOT_APP_ID', label: t('setupWizard.fallback.QQBOT_APP_ID.label'), desc: t('setupWizard.fallback.QQBOT_APP_ID.desc'), url: 'https://q.qq.com', url_desc: t('setupWizard.fallback.QQBOT_APP_ID.url_desc'), required: true, configured: false, masked_value: '', raw_value: '' },
      { key: 'QQBOT_APP_SECRET', label: t('setupWizard.fallback.QQBOT_APP_SECRET.label'), desc: t('setupWizard.fallback.QQBOT_APP_SECRET.desc'), url: 'https://q.qq.com', url_desc: t('setupWizard.fallback.QQBOT_APP_SECRET.url_desc'), required: true, configured: false, masked_value: '', raw_value: '' },
      { key: 'EMBED_API_KEY', label: t('setupWizard.fallback.EMBED_API_KEY.label'), desc: t('setupWizard.fallback.EMBED_API_KEY.desc'), url: 'https://siliconflow.cn', url_desc: t('setupWizard.fallback.EMBED_API_KEY.url_desc'), required: true, configured: false, masked_value: '', raw_value: '' },
      { key: 'WEBUI_PASSWORD', label: t('setupWizard.fallback.WEBUI_PASSWORD.label'), desc: t('setupWizard.fallback.WEBUI_PASSWORD.desc'), url: '', url_desc: t('setupWizard.fallback.WEBUI_PASSWORD.url_desc'), required: false, configured: false, masked_value: '', raw_value: '' },
      { key: 'SILICONFLOW_API_KEY', label: t('setupWizard.fallback.SILICONFLOW_API_KEY.label'), desc: t('setupWizard.fallback.SILICONFLOW_API_KEY.desc'), url: 'https://siliconflow.cn', url_desc: t('setupWizard.fallback.SILICONFLOW_API_KEY.url_desc'), required: false, configured: false, masked_value: '', raw_value: '' },
      { key: 'OPENROUTER_API_KEY', label: t('setupWizard.fallback.OPENROUTER_API_KEY.label'), desc: t('setupWizard.fallback.OPENROUTER_API_KEY.desc'), url: 'https://openrouter.ai', url_desc: t('setupWizard.fallback.OPENROUTER_API_KEY.url_desc'), required: false, configured: false, masked_value: '', raw_value: '' },
      { key: 'AGNES_API_KEY', label: t('setupWizard.fallback.AGNES_API_KEY.label'), desc: t('setupWizard.fallback.AGNES_API_KEY.desc'), url: 'https://agnes-ai.com', url_desc: t('setupWizard.fallback.AGNES_API_KEY.url_desc'), required: false, configured: false, masked_value: '', raw_value: '' },
      { key: 'MODELSCOPE_ACCESS_TOKEN', label: t('setupWizard.fallback.MODELSCOPE_ACCESS_TOKEN.label'), desc: t('setupWizard.fallback.MODELSCOPE_ACCESS_TOKEN.desc'), url: 'https://modelscope.cn', url_desc: t('setupWizard.fallback.MODELSCOPE_ACCESS_TOKEN.url_desc'), required: false, configured: false, masked_value: '', raw_value: '' },
    ]
    for (const k of keys.value) {
      testStatuses[k.key] = 'untested'
      testMessages[k.key] = ''
    }
    error.value = t('setupWizard.apiLoadFailed')
  }

  // 加载免责协议状态
  try {
    const status = await getDisclaimerStatus()
    disclaimerAgreed.value = !!status.agreed
    disclaimerChecked.value = !!status.agreed  // 已同意则默认勾选
    if (status.agreed) {
      localStorage.setItem('nahida_disclaimer_agreed', 'true')
    }
  } catch { /* 降级：首次使用，需要勾选 */ }

  // 也检查 localStorage（快速路径）
  if (localStorage.getItem('nahida_disclaimer_agreed') === 'true') {
    disclaimerAgreed.value = true
    disclaimerChecked.value = true
  }
})

const requiredKeys = computed(() => keys.value.filter(k => k.required))
const optionalKeys = computed(() => keys.value.filter(k => !k.required))

function handleUpdate(key: string, value: string) {
  updates.value[key] = value
  modifiedKeys.value.add(key)
  // Reset test status when value changes
  if (testStatuses[key] === 'passed' || testStatuses[key] === 'failed') {
    testStatuses[key] = 'untested'
    testMessages[key] = ''
    testedRequiredKeys.value.delete(key)
  }
  // SiliconFlow Key 联动：填一个自动填充另一个（如果另一个为空）
  if (key === 'EMBED_API_KEY' && value && !updates.value['SILICONFLOW_API_KEY']) {
    updates.value['SILICONFLOW_API_KEY'] = value
    modifiedKeys.value.add('SILICONFLOW_API_KEY')
  } else if (key === 'SILICONFLOW_API_KEY' && value && !updates.value['EMBED_API_KEY']) {
    updates.value['EMBED_API_KEY'] = value
    modifiedKeys.value.add('EMBED_API_KEY')
  }
}

function getExtraForKey(keyName: string): Record<string, string> | undefined {
  if (keyName === 'QQBOT_APP_ID') {
    const secret = updates.value['QQBOT_APP_SECRET']
    return secret ? { QQBOT_APP_SECRET: secret } : undefined
  }
  if (keyName === 'QQBOT_APP_SECRET') {
    const appId = updates.value['QQBOT_APP_ID']
    return appId ? { QQBOT_APP_ID: appId } : undefined
  }
  return undefined
}

async function handleTestKey(keyName: string) {
  const keyValue = updates.value[keyName]
  if (!keyValue) return

  testStatuses[keyName] = 'testing'
  testMessages[keyName] = ''

  try {
    const extra = getExtraForKey(keyName)
    const result = await api.testSetupKey(keyName, keyValue, extra)
    if (result.success) {
      testStatuses[keyName] = 'passed'
      testMessages[keyName] = result.message || t('setupWizard.testPass')
      // Track tested required key
      const keyItem = keys.value.find(k => k.key === keyName)
      if (keyItem?.required) {
        testedRequiredKeys.value.add(keyName)
      }
    } else {
      testStatuses[keyName] = 'failed'
      testMessages[keyName] = result.message || t('setupWizard.testFail')
      testedRequiredKeys.value.delete(keyName)
    }
  } catch (e: any) {
    testStatuses[keyName] = 'failed'
    testMessages[keyName] = e.message || t('setupWizard.testFailed')
    testedRequiredKeys.value.delete(keyName)
  }
}

async function handleTestAllRequired() {
  testingAll.value = true
  for (const k of requiredKeys.value) {
    const value = updates.value[k.key]
    if (!value) continue
    if (testStatuses[k.key] === 'passed') continue
    await handleTestKey(k.key)
  }
  testingAll.value = false
}

const allRequiredTestedAndPassed = computed(() => {
  const required = requiredKeys.value
  if (required.length === 0) return true
  return required.every(k => {
    const hasValue = k.configured || updates.value[k.key]
    if (!hasValue) return false
    // Only require testing for keys that were modified by the user
    if (modifiedKeys.value.has(k.key)) {
      return testedRequiredKeys.value.has(k.key) && testStatuses[k.key] === 'passed'
    }
    // Already configured keys that weren't modified are considered OK
    return true
  })
})

const hasUpdates = computed(() => modifiedKeys.value.size > 0)

function handleDisclaimerScroll(e: Event) {
  const el = e.target as HTMLElement
  if (el.scrollTop + el.clientHeight >= el.scrollHeight - 4) {
    disclaimerScrolledToBottom.value = true
  }
}

async function handleSave() {
  if (!hasUpdates.value) return

  // 免责协议校验
  if (!disclaimerAgreed.value && !disclaimerChecked.value) {
    error.value = t('disclaimer.mustAgree')
    return
  }
  // 首次同意则写入后端 + localStorage
  if (!disclaimerAgreed.value && disclaimerChecked.value) {
    try {
      await agreeDisclaimer(true)
      localStorage.setItem('nahida_disclaimer_agreed', 'true')
      disclaimerAgreed.value = true
    } catch (e: any) {
      error.value = e.message || t('setupWizard.disclaimerSaveFailed')
      return
    }
  }

  // Check if all modified required keys have been tested
  if (!allRequiredTestedAndPassed.value) {
    error.value = t('setupWizard.testRequired')
    return
  }

  saving.value = true
  error.value = ''
  try {
    // Only save modified keys
    const keysToSave: Record<string, string> = {}
    for (const key of modifiedKeys.value) {
      keysToSave[key] = updates.value[key]
    }
    await api.saveSetupKeys(keysToSave, true)
    const allRequired = requiredKeys.value.every(k =>
      k.configured || updates.value[k.key]
    )
    if (allRequired) {
      // 自动登录以获取 token，避免跳转后被重定向到登录页
      try {
        await authStore.login('')
      } catch {
        // 登录失败不影响跳转
      }
      router.replace('/setup/profile')
    }
  } catch (e: any) {
    // Check for KEY_TEST_FAILED error
    const msg = e.message || ''
    if (msg.includes('KEY_TEST_FAILED')) {
      error.value = `${t('setupWizard.someFailed')}：${msg}`
    } else {
      error.value = msg
    }
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="setup-page">
    <DendroShader />
    <div class="setup-center">
      <div class="setup-card glass-panel">
        <span class="vine corner-tl"></span>
        <span class="vine corner-br"></span>

        <div class="setup-header">
          <DendroEmblem :size="84" spin />
          <h1>{{ t('setup.title') }}</h1>
          <p class="subtitle">{{ t('setupWizard.greeting') }}</p>
          <p class="version-tag">v{{ version }}</p>
        </div>

        <div class="setup-body">
          <h2 class="section-title required-title">── {{ t('setupWizard.required') }} ──</h2>
          <KeyAccordion
            :items="requiredKeys"
            :test-statuses="testStatuses"
            :test-messages="testMessages"
            @update="handleUpdate"
            @test="handleTestKey"
          />
          <p class="auto-bind-hint">{{ t('setupWizard.masterBinding') }}</p>

          <button
            class="dendro-btn test-all-btn"
            :disabled="testingAll || !hasUpdates"
            @click="handleTestAllRequired"
          >
            {{ testingAll ? t('setupWizard.testing') : t('setupWizard.testAll') }}
          </button>

          <div class="optional-toggle" @click="showOptional = !showOptional">
            <span class="section-title optional-title">── {{ t('setupWizard.optional') }} ──</span>
            <span class="toggle-arrow" :class="{ 'arrow-open': showOptional }">❯</span>
          </div>
          <Transition name="collapse">
            <div v-if="showOptional" class="optional-body">
              <KeyAccordion
                :items="optionalKeys"
                :test-statuses="testStatuses"
                :test-messages="testMessages"
                @update="handleUpdate"
                @test="handleTestKey"
              />
            </div>
          </Transition>

          <p v-if="error" class="error-text">{{ error }}</p>

          <!-- 免责协议 -->
          <Tilt3D>
          <div class="disclaimer-section" v-if="!disclaimerAgreed">
            <h3 class="disclaimer-title">── {{ t('disclaimer.title') }} ──</h3>
            <div
              class="disclaimer-scroll"
              ref="disclaimerScrollRef"
              @scroll="handleDisclaimerScroll"
            >
              <pre class="disclaimer-text">{{ t('disclaimer.content') }}</pre>
            </div>
            <p class="disclaimer-hint">
              {{ disclaimerScrolledToBottom ? t('disclaimer.scrolledToBottom') : t('disclaimer.scrollToAgree') }}
            </p>
            <label class="disclaimer-check" :class="{ disabled: !disclaimerScrolledToBottom }">
              <input
                type="checkbox"
                v-model="disclaimerChecked"
                :disabled="!disclaimerScrolledToBottom"
              />
              <span>{{ t('disclaimer.agree') }}</span>
            </label>
          </div>
          </Tilt3D>
          <div class="disclaimer-section disclaimer-agreed-banner" v-if="disclaimerAgreed">
            <span>✓ {{ t('disclaimer.agreed') }}</span>
          </div>

          <button
            class="dendro-btn save-btn"
            :disabled="saving || !hasUpdates || !allRequiredTestedAndPassed || (!disclaimerAgreed && !disclaimerChecked)"
            @click="handleSave"
          >
            {{ saving ? t('setupWizard.saving') : t('setupWizard.save') }}
          </button>

          <p class="status-hint">
            <template v-if="!allRequiredTestedAndPassed && hasUpdates">
              {{ t('setupWizard.testFirst') }}
            </template>
            <template v-else-if="hasUpdates">
              {{ t('setupWizard.modifiedN') }} {{ modifiedKeys.size }} {{ t('setupWizard.configItems') }}
            </template>
            <template v-else>
              {{ t('setupWizard.allReady') }}
            </template>
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
  gap: 16px;
}

.section-title {
  font-size: 14px;
  font-family: 'Noto Serif SC', serif;
  letter-spacing: 2px;
  margin: 0;
}

.required-title {
  color: var(--dendro);
}

.optional-title {
  color: var(--cyan, #67e8f9);
}

.test-all-btn {
  width: 100%;
  height: 36px;
  font-size: 14px;
}

.auto-bind-hint {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.45);
  text-align: center;
  margin: 8px 0 0;
  line-height: 1.6;
}

.test-all-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
}

.optional-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  user-select: none;
  margin-top: 4px;
}

.optional-toggle:hover .optional-title {
  opacity: 0.8;
}

.toggle-arrow {
  color: var(--cyan, #67e8f9);
  font-size: 12px;
  transition: transform 0.3s var(--ease-smooth);
}

.toggle-arrow.arrow-open {
  transform: rotate(90deg);
}

.optional-body {
  overflow: hidden;
}

/* 折叠过渡动画 */
.collapse-enter-active,
.collapse-leave-active {
  transition: max-height 0.35s var(--ease-smooth),
              opacity 0.3s var(--ease-smooth);
  overflow: hidden;
}

.collapse-enter-from,
.collapse-leave-to {
  max-height: 0;
  opacity: 0;
}

.collapse-enter-to,
.collapse-leave-from {
  max-height: 600px;
  opacity: 1;
}

.error-text {
  color: var(--alert);
  font-size: 13px;
  margin: 0;
}

.save-btn {
  width: 100%;
  height: 44px;
  font-size: 16px;
  margin-top: 6px;
}

.save-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
}

.status-hint {
  color: var(--moon-dim);
  font-size: 12px;
  text-align: center;
  margin: 0;
}

.disclaimer-section {
  border: 1px solid var(--glass-border);
  border-radius: 10px;
  padding: 12px 14px;
  background: rgba(15, 31, 23, 0.35);
  margin-top: 4px;
}
.disclaimer-title {
  font-size: 13px;
  color: var(--wisdom);
  font-family: 'Noto Serif SC', serif;
  margin: 0 0 8px;
  text-align: center;
}
.disclaimer-scroll {
  max-height: 180px;
  overflow-y: auto;
  background: rgba(0, 0, 0, 0.25);
  border-radius: 6px;
  padding: 10px 12px;
  margin-bottom: 8px;
}
.disclaimer-text {
  font-size: 12px;
  color: var(--moon-dim);
  font-family: 'Noto Sans SC', sans-serif;
  white-space: pre-wrap;
  line-height: 1.7;
  margin: 0;
}
.disclaimer-hint {
  font-size: 11px;
  color: var(--moon-dim);
  text-align: center;
  margin: 0 0 8px;
  opacity: 0.7;
}
.disclaimer-check {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--dendro);
  cursor: pointer;
  user-select: none;
}
.disclaimer-check.disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.disclaimer-check input[type="checkbox"] {
  width: 16px;
  height: 16px;
  accent-color: var(--dendro);
  cursor: pointer;
}
.disclaimer-check.disabled input[type="checkbox"] {
  cursor: not-allowed;
}
.disclaimer-agreed-banner {
  text-align: center;
  color: var(--dendro);
  font-size: 12px;
  opacity: 0.7;
}
</style>
