<script setup lang="ts">
import { ref, onMounted, computed, reactive } from 'vue'
import { useRouter } from 'vue-router'
import DendroShader from '../components/fx/DendroShader.vue'
import DendroEmblem from '../components/fx/DendroEmblem.vue'
import KeyAccordion, { type TestStatus } from '../components/setup/KeyAccordion.vue'
import { api } from '../api'
import { useAuthStore } from '../stores/auth'

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

onMounted(async () => {
  try {
    const data = await api.getSetupKeys()
    keys.value = data.keys
    // Initialize test statuses
    for (const k of data.keys) {
      testStatuses[k.key] = 'untested'
      testMessages[k.key] = ''
    }
  } catch (e: any) {
    console.error('[SetupWizard] getSetupKeys failed:', e)
    // Fallback: 显示硬编码的 key 列表，确保页面不会空白
    keys.value = [
      { key: 'MIMO_API_KEY', label: 'MiMo API 密钥', desc: '小米 MiMo 大模型 API 密钥（主 LLM + TTS + Vision）', url: 'https://platform.xiaomimimo.com?ref=SU5WDZ', url_desc: '注册 → 控制台 → API Keys', required: true, configured: false, masked_value: '' },
      { key: 'QQBOT_APP_ID', label: 'QQ Bot App ID', desc: 'QQ 机器人应用 ID', url: 'https://q.qq.com', url_desc: '创建机器人应用 → 获取 AppID', required: true, configured: false, masked_value: '' },
      { key: 'QQBOT_APP_SECRET', label: 'QQ Bot App Secret', desc: 'QQ 机器人应用密钥', url: 'https://q.qq.com', url_desc: '同一页面的 AppSecret', required: true, configured: false, masked_value: '' },
      { key: 'EMBED_API_KEY', label: '向量嵌入 API 密钥', desc: '硅基流动嵌入模型密钥', url: 'https://siliconflow.cn', url_desc: '注册 → API Keys → 复制', required: true, configured: false, masked_value: '' },
      { key: 'WEBUI_PASSWORD', label: 'Web UI 密码', desc: '留空则无需密码登录', url: '', url_desc: '', required: false, configured: false, masked_value: '' },
      { key: 'SILICONFLOW_API_KEY', label: 'SiliconFlow API 密钥', desc: '硅基流动 API 密钥', url: 'https://siliconflow.cn', url_desc: '注册 → API Keys', required: false, configured: false, masked_value: '' },
      { key: 'OPENROUTER_API_KEY', label: 'OpenRouter API 密钥', desc: 'OpenRouter API 密钥', url: 'https://openrouter.ai', url_desc: '注册 → API Keys', required: false, configured: false, masked_value: '' },
      { key: 'AGNES_API_KEY', label: 'Agnes AI 图像/视频密钥', desc: '图片生成和视频生成的核心依赖', url: 'https://agnes-ai.com', url_desc: '注册 → API Keys', required: false, configured: false, masked_value: '' },
      { key: 'MODELSCOPE_ACCESS_TOKEN', label: '魔搭 Access Token', desc: '魔搭 ModelScope 免费模型发现', url: 'https://modelscope.cn', url_desc: '注册 → 个人中心 → 访问令牌', required: false, configured: false, masked_value: '' },
    ]
    for (const k of keys.value) {
      testStatuses[k.key] = 'untested'
      testMessages[k.key] = ''
    }
    error.value = 'API 加载失败，显示默认配置项。请刷新页面重试。'
  }
})

const requiredKeys = computed(() => keys.value.filter(k => k.required))
const optionalKeys = computed(() => keys.value.filter(k => !k.required))

function handleUpdate(key: string, value: string) {
  updates.value[key] = value
  // Reset test status when value changes
  if (testStatuses[key] === 'passed' || testStatuses[key] === 'failed') {
    testStatuses[key] = 'untested'
    testMessages[key] = ''
    testedRequiredKeys.value.delete(key)
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
      testMessages[keyName] = result.message || '测试通过'
      // Track tested required key
      const keyItem = keys.value.find(k => k.key === keyName)
      if (keyItem?.required) {
        testedRequiredKeys.value.add(keyName)
      }
    } else {
      testStatuses[keyName] = 'failed'
      testMessages[keyName] = result.message || '测试失败'
      testedRequiredKeys.value.delete(keyName)
    }
  } catch (e: any) {
    testStatuses[keyName] = 'failed'
    testMessages[keyName] = e.message || '测试请求失败'
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
    // If the key has a value (configured or updated), it must be tested and passed
    // Only check updates that were entered by the user
    if (updates.value[k.key]) {
      return testedRequiredKeys.value.has(k.key) && testStatuses[k.key] === 'passed'
    }
    // Already configured keys that weren't modified are considered OK
    return true
  })
})

const hasUpdates = computed(() => Object.keys(updates.value).length > 0)

async function handleSave() {
  if (!hasUpdates.value) return

  // Check if all required keys with new values have been tested
  if (!allRequiredTestedAndPassed.value) {
    error.value = '请先测试所有必填 API Key'
    return
  }

  saving.value = true
  error.value = ''
  try {
    await api.saveSetupKeys(updates.value, true)
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
      router.replace('/')
    }
  } catch (e: any) {
    // Check for KEY_TEST_FAILED error
    const msg = e.message || ''
    if (msg.includes('KEY_TEST_FAILED')) {
      error.value = `部分 Key 测试未通过：${msg}`
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
          <h1>纳西妲 · 配置向导</h1>
          <p class="subtitle">世界的记忆，由我来守护</p>
          <p class="version-tag">v{{ version }}</p>
        </div>

        <div class="setup-body">
          <h2 class="section-title required-title">── 必填配置 ──</h2>
          <KeyAccordion
            :items="requiredKeys"
            :test-statuses="testStatuses"
            :test-messages="testMessages"
            @update="handleUpdate"
            @test="handleTestKey"
          />
          <p class="auto-bind-hint">主人身份自动绑定：私聊第一条消息自动识别；拉群时自动绑定群主身份，无需手动配置。</p>

          <button
            class="dendro-btn test-all-btn"
            :disabled="testingAll || !hasUpdates"
            @click="handleTestAllRequired"
          >
            {{ testingAll ? '测试中…' : '测试全部必填项' }}
          </button>

          <div class="optional-toggle" @click="showOptional = !showOptional">
            <span class="section-title optional-title">── 选填配置 ──</span>
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

          <button
            class="dendro-btn save-btn"
            :disabled="saving || !hasUpdates || !allRequiredTestedAndPassed"
            @click="handleSave"
          >
            {{ saving ? '草元素汇聚中…' : '保存配置' }}
          </button>

          <p class="status-hint">
            <template v-if="!allRequiredTestedAndPassed && hasUpdates">
              请先测试所有必填 API Key
            </template>
            <template v-else-if="hasUpdates">
              已修改 {{ Object.keys(updates).length }} 项配置，全部必填项测试通过
            </template>
            <template v-else>
              请配置必填项后保存
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
</style>
