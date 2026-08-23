<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import {
  NButton, NInput, NInputNumber, NRadioGroup, NRadioButton, NSelect,
  NTag, NSpin, useMessage,
} from 'naive-ui'
import { get, put, post } from '../api'
import { t } from '../i18n'
import ViewTitleIcon from '../components/fx/ViewTitleIcon.vue'

const message = useMessage()
const loading = ref(false)
const saving = ref(false)
const primary = ref('')
const engines = ref<any[]>([])

const keyInputs = reactive<Record<string, string>>({})
const testing = ref(false)
const testQuery = ref('')
const testEngine = ref('compare')
const testTopK = ref(5)
const testResult = ref<Record<string, any> | null>(null)

const engineOptions = [
  { label: t('searchEngines.compareAll'), value: 'compare' },
  { label: 'AnySearch', value: 'anysearch' },
  { label: 'Tavily', value: 'tavily' },
  { label: 'Bing', value: 'bing' },
]

const primaryOptions = [
  { label: t('searchEngines.primaryAuto'), value: '' },
  { label: 'AnySearch', value: 'anysearch' },
  { label: 'Tavily', value: 'tavily' },
  { label: 'Bing', value: 'bing' },
]

onMounted(async () => {
  await loadConfig()
})

async function loadConfig() {
  loading.value = true
  try {
    const data = await get<any>('/search-engines/config')
    primary.value = data.primary || ''
    engines.value = data.engines || []
  } catch (e: any) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

async function saveConfig(withKeys: boolean) {
  saving.value = true
  try {
    const body: Record<string, any> = { primary: primary.value }
    if (withKeys) {
      body.keys = Object.fromEntries(
        Object.entries(keyInputs).filter(([, v]) => (v || '').trim() !== ''))
    }
    const data = await put<{ primary: string }>('/search-engines/config', body)
    if (withKeys) message.success(t('searchEngines.savedKeys'))
    else message.success(t('searchEngines.savedPrimary'))
    primary.value = data.primary ?? primary.value
    await loadConfig()
    Object.keys(keyInputs).forEach(k => keyInputs[k] = '')
  } catch (e: any) {
    message.error(e.message)
  } finally {
    saving.value = false
  }
}

async function runTest() {
  if (!testQuery.value.trim()) {
    message.warning(t('searchEngines.needQuery'))
    return
  }
  testing.value = true
  testResult.value = null
  try {
    const data = await post<Record<string, unknown>>('/search-engines/test', {
      query: testQuery.value,
      engine: testEngine.value,
      top_k: testTopK.value,
    })
    testResult.value = data
  } catch (e: any) {
    message.error(e.message)
  } finally {
    testing.value = false
  }
}

function engineName(id: string): string {
  const map: Record<string, string> = { anysearch: 'AnySearch', tavily: 'Tavily', bing: 'Bing' }
  return map[id] || id
}
</script>

<script lang="ts">
export default { name: 'SearchEnginesView' }
</script>

<template>
  <div class="se-view">
    <div class="view-header">
      <h2 class="view-title view-title-icon"><ViewTitleIcon name="search" /> {{ t('searchEngines.title') }}</h2>
    </div>

    <n-spin :show="loading">
      <!-- 主引擎选择 -->
      <div class="se-section glass-panel">
        <h3 class="section-title">{{ t('searchEngines.primaryTitle') }}</h3>
        <p class="section-desc">{{ t('searchEngines.primaryDesc') }}</p>
        <n-radio-group v-model:value="primary">
          <n-radio-button v-for="opt in primaryOptions" :key="opt.value" :value="opt.value" :label="opt.label" />
        </n-radio-group>
        <div class="save-row">
          <n-button type="primary" size="small" :loading="saving" @click="saveConfig(false)">
            {{ t('searchEngines.savePrimary') }}
          </n-button>
        </div>
      </div>

      <!-- 引擎状态与 Key 配置 -->
      <div class="se-section glass-panel">
        <h3 class="section-title">{{ t('searchEngines.enginesTitle') }}</h3>
        <div class="engine-grid">
          <div v-for="e in engines" :key="e.id" class="engine-card">
            <div class="engine-head">
              <span class="engine-name">{{ e.name }}</span>
              <n-tag size="tiny" :type="e.available ? 'success' : 'default'">
                {{ e.available ? t('searchEngines.available') : t('searchEngines.unavailable') }}
              </n-tag>
              <n-tag size="tiny">{{ e.latency_hint }}</n-tag>
            </div>
            <p class="engine-desc">{{ e.desc }}</p>
            <div v-if="e.key_env" class="engine-key-row">
              <n-input
                v-model:value="keyInputs[e.key_env]"
                size="small"
                type="password"
                show-password-on="click"
                :placeholder="e.key_configured ? e.masked_key : t('searchEngines.keyPlaceholder')"
              />
            </div>
          </div>
        </div>
        <div class="save-row">
          <n-button type="primary" size="small" :loading="saving" @click="saveConfig(true)">
            {{ t('searchEngines.saveKeys') }}
          </n-button>
          <span class="save-hint">{{ t('searchEngines.keyHint') }}</span>
        </div>
      </div>

      <!-- 手动测试 -->
      <div class="se-section glass-panel">
        <h3 class="section-title">{{ t('searchEngines.testTitle') }}</h3>
        <p class="section-desc">{{ t('searchEngines.testDesc') }}</p>
        <div class="test-row">
          <n-input
            v-model:value="testQuery"
            style="flex: 1"
            :placeholder="t('searchEngines.testPlaceholder')"
            @keyup.enter="runTest"
          />
          <n-select v-model:value="testEngine" :options="engineOptions" style="width: 150px" size="small" />
          <n-input-number v-model:value="testTopK" :min="1" :max="10" size="small" style="width: 90px" />
          <n-button type="primary" :loading="testing" @click="runTest">{{ t('searchEngines.testBtn') }}</n-button>
        </div>

        <div v-if="testResult" class="test-output">
          <!-- 单引擎 -->
          <template v-if="testResult.mode === 'single'">
            <div class="single-head">
              <n-tag type="info">{{ engineName(testResult.engine) }}</n-tag>
              <span class="metric">{{ testResult.latency_ms }}ms</span>
              <span class="metric">{{ testResult.count }} {{ t('searchEngines.resultsUnit') }}</span>
              <n-tag v-if="testResult.error" type="error">{{ testResult.error }}</n-tag>
            </div>
            <div v-for="(r, i) in (testResult.results || [])" :key="i" class="result-item">
              <div class="result-title">{{ i + 1 }}. {{ r.title }}</div>
              <div class="result-snippet">{{ r.snippet }}</div>
              <a class="result-url" :href="r.url" target="_blank" rel="noopener">{{ r.url }}</a>
            </div>
          </template>
          <!-- 三引擎对比 -->
          <template v-else>
            <div class="compare-grid">
              <div v-for="e in (testResult.engines || [])" :key="e.engine" class="compare-col">
                <div class="compare-head">
                  <span class="engine-name">{{ engineName(e.engine) }}</span>
                  <n-tag v-if="e.error" size="tiny" type="error">{{ t('searchEngines.failed') }}</n-tag>
                  <n-tag v-else size="tiny" :type="e.count ? 'success' : 'warning'">
                    {{ e.count }} {{ t('searchEngines.resultsUnit') }}
                  </n-tag>
                </div>
                <div class="compare-latency">{{ e.latency_ms }}ms</div>
                <div v-if="e.error" class="compare-error">{{ e.error }}</div>
                <div v-for="(r, i) in (e.results || []).slice(0, 5)" :key="i" class="compare-item">
                  {{ i + 1 }}. {{ r.title }}
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </n-spin>
  </div>
</template>

<style scoped>
.se-view {
  padding: 0 0 24px;
}

.view-header {
  margin-bottom: 16px;
}

.view-title {
  font-family: 'Noto Serif SC', serif;
  margin: 0;
}

.se-section {
  padding: 18px 20px;
  margin-bottom: 16px;
}

.section-title {
  font-size: 14px;
  color: var(--dendro);
  margin: 0 0 6px;
}

.section-desc {
  font-size: 12.5px;
  color: var(--moon-dim);
  margin: 0 0 14px;
  line-height: 1.5;
}

.save-row {
  margin-top: 14px;
  display: flex;
  align-items: center;
  gap: 10px;
}

.save-hint {
  font-size: 11.5px;
  color: var(--moon-dim);
}

.engine-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}

.engine-card {
  padding: 12px 14px;
  border-radius: 10px;
  background: rgba(10, 20, 14, 0.6);
  border: 1px solid var(--glass-border);
}

.engine-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.engine-name {
  font-size: 13.5px;
  font-weight: 700;
  color: var(--moon);
}

.engine-desc {
  font-size: 11.5px;
  color: var(--moon-dim);
  margin: 0 0 8px;
  line-height: 1.5;
}

.engine-key-row {
  margin-top: 4px;
}

.test-row {
  display: flex;
  gap: 10px;
  align-items: center;
}

.test-output {
  margin-top: 16px;
}

.single-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.metric {
  font-size: 13px;
  color: var(--dendro);
  font-family: 'JetBrains Mono', monospace;
  font-weight: 700;
}

.result-item {
  padding: 8px 12px;
  border-radius: 8px;
  background: rgba(10, 20, 14, 0.6);
  border: 1px solid var(--glass-border);
  margin-bottom: 6px;
}

.result-title {
  font-size: 13px;
  color: var(--moon);
  font-weight: 600;
  margin-bottom: 3px;
}

.result-snippet {
  font-size: 12px;
  color: var(--moon-dim);
  margin-bottom: 3px;
  line-height: 1.5;
}

.result-url {
  font-size: 11px;
  color: var(--dendro);
  word-break: break-all;
  text-decoration: none;
}

.compare-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}

.compare-col {
  padding: 12px 14px;
  border-radius: 10px;
  background: rgba(10, 20, 14, 0.6);
  border: 1px solid var(--glass-border);
}

.compare-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.compare-latency {
  font-size: 16px;
  font-weight: 700;
  color: var(--dendro);
  font-family: 'JetBrains Mono', monospace;
  margin: 6px 0 8px;
}

.compare-error {
  font-size: 11.5px;
  color: #da5252;
  line-height: 1.5;
}

.compare-item {
  font-size: 12px;
  color: var(--moon);
  padding: 3px 0;
  border-top: 1px dashed var(--glass-border);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
