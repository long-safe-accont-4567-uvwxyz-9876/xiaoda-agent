<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import {
  NSwitch, NSlider, NInputNumber, NButton, NInput, NTag, NPopconfirm,
  NTabs, NTabPane, NSpin, useMessage,
} from 'naive-ui'
import { get, put, post } from '../api'
import { t } from '../i18n'
import ViewTitleIcon from '../components/fx/ViewTitleIcon.vue'

const message = useMessage()
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const activeTab = ref('switches')

const config = reactive<Record<string, boolean | number>>({})
const defaults = ref<Record<string, boolean | number>>({})
const testQuery = ref('')
const testTopK = ref(5)
const testExpect = ref('')
interface RetrievalTestItem {
  id?: string | null
  summary: string
  score: number
  importance: number
  emotion_label: string
  source: string
  matched?: boolean
}
const testResults = ref<RetrievalTestItem[]>([])
const testCount = ref(0)
const testMetrics = ref<Record<string, any> | null>(null)
const testError = ref('')

const evalText = ref('')
const evaluating = ref(false)
const evalReport = ref<Record<string, any> | null>(null)

const boolKeys = computed(() => [
  { key: 'RERANKER_ENABLED', label: t('retrieval.labels.RERANKER_ENABLED'), desc: t('retrieval.descs.RERANKER_ENABLED') },
  { key: 'QUERY_TRANSFORM_ENABLED', label: t('retrieval.labels.QUERY_TRANSFORM_ENABLED'), desc: t('retrieval.descs.QUERY_TRANSFORM_ENABLED') },
  { key: 'HYDE_ENABLED', label: t('retrieval.labels.HYDE_ENABLED'), desc: t('retrieval.descs.HYDE_ENABLED') },
  { key: 'MEMORY_RETRIEVAL_DIFFUSION', label: t('retrieval.labels.MEMORY_RETRIEVAL_DIFFUSION'), desc: t('retrieval.descs.MEMORY_RETRIEVAL_DIFFUSION') },
  { key: 'RETRIEVAL_SMART_SKIP', label: t('retrieval.labels.RETRIEVAL_SMART_SKIP'), desc: t('retrieval.descs.RETRIEVAL_SMART_SKIP') },
  { key: 'RETRIEVAL_PARALLEL_TRANSFORM', label: t('retrieval.labels.RETRIEVAL_PARALLEL_TRANSFORM'), desc: t('retrieval.descs.RETRIEVAL_PARALLEL_TRANSFORM') },
  { key: 'RETRIEVAL_PARALLEL_SEARCH', label: t('retrieval.labels.RETRIEVAL_PARALLEL_SEARCH'), desc: t('retrieval.descs.RETRIEVAL_PARALLEL_SEARCH') },
  { key: 'QUERY_CACHE_ENABLED', label: t('retrieval.labels.QUERY_CACHE_ENABLED'), desc: t('retrieval.descs.QUERY_CACHE_ENABLED') },
  { key: 'PARENT_CHILD_CHUNK_ENABLED', label: t('retrieval.labels.PARENT_CHILD_CHUNK_ENABLED'), desc: t('retrieval.descs.PARENT_CHILD_CHUNK_ENABLED') },
  { key: 'KG_V2_ENABLED', label: t('retrieval.labels.KG_V2_ENABLED'), desc: t('retrieval.descs.KG_V2_ENABLED') },
  { key: 'CONTEXTUAL_RETRIEVAL_ENABLED', label: t('retrieval.labels.CONTEXTUAL_RETRIEVAL_ENABLED'), desc: t('retrieval.descs.CONTEXTUAL_RETRIEVAL_ENABLED') },
  { key: 'MEMORY_DISTILL_ENABLED', label: t('retrieval.labels.MEMORY_DISTILL_ENABLED'), desc: t('retrieval.descs.MEMORY_DISTILL_ENABLED') },
])

const floatSliders = computed(() => [
  { key: 'RAG_RERANK_WEIGHT', label: t('retrieval.labels.RAG_RERANK_WEIGHT'), min: 0, max: 1, step: 0.05, desc: t('retrieval.descs.RAG_RERANK_WEIGHT') },
  { key: 'RAG_KG_WEIGHT', label: t('retrieval.labels.RAG_KG_WEIGHT'), min: 0, max: 1, step: 0.05, desc: t('retrieval.descs.RAG_KG_WEIGHT') },
  { key: 'RAG_IMPORTANCE_WEIGHT', label: t('retrieval.labels.RAG_IMPORTANCE_WEIGHT'), min: 0, max: 1, step: 0.05, desc: t('retrieval.descs.RAG_IMPORTANCE_WEIGHT') },
  { key: 'RAG_MIN_FINAL_SCORE', label: t('retrieval.labels.RAG_MIN_FINAL_SCORE'), min: 0, max: 0.5, step: 0.01, desc: t('retrieval.descs.RAG_MIN_FINAL_SCORE') },
  { key: 'RAG_VEC_MAX_DISTANCE', label: t('retrieval.labels.RAG_VEC_MAX_DISTANCE'), min: 0.5, max: 2.0, step: 0.05, desc: t('retrieval.descs.RAG_VEC_MAX_DISTANCE') },
  { key: 'RAG_VEC_SOFT_PENALTY', label: t('retrieval.labels.RAG_VEC_SOFT_PENALTY'), min: 0, max: 1, step: 0.05, desc: t('retrieval.descs.RAG_VEC_SOFT_PENALTY') },
  { key: 'QUERY_CACHE_THRESHOLD', label: t('retrieval.labels.QUERY_CACHE_THRESHOLD'), min: 0.5, max: 1, step: 0.01, desc: t('retrieval.descs.QUERY_CACHE_THRESHOLD') },
  { key: 'MEMORY_WARM_VEC_WEIGHT', label: t('retrieval.labels.MEMORY_WARM_VEC_WEIGHT'), min: 0, max: 1, step: 0.1, desc: t('retrieval.descs.MEMORY_WARM_VEC_WEIGHT') },
  { key: 'EMOTION_TRIGGER_THRESHOLD', label: t('retrieval.labels.EMOTION_TRIGGER_THRESHOLD'), min: 0, max: 1, step: 0.05, desc: t('retrieval.descs.EMOTION_TRIGGER_THRESHOLD') },
])

const intInputs = computed(() => [
  { key: 'RAG_RECALL_LIMIT', label: t('retrieval.labels.RAG_RECALL_LIMIT'), min: 10, max: 500, desc: t('retrieval.descs.RAG_RECALL_LIMIT') },
  { key: 'RAG_RERANK_LIMIT', label: t('retrieval.labels.RAG_RERANK_LIMIT'), min: 5, max: 200, desc: t('retrieval.descs.RAG_RERANK_LIMIT') },
  { key: 'QUERY_EXPAND_COUNT', label: t('retrieval.labels.QUERY_EXPAND_COUNT'), min: 0, max: 10, desc: t('retrieval.descs.QUERY_EXPAND_COUNT') },
  { key: 'RERANKER_OVERSAMPLE_RATIO', label: t('retrieval.labels.RERANKER_OVERSAMPLE_RATIO'), min: 1, max: 10, desc: t('retrieval.descs.RERANKER_OVERSAMPLE_RATIO') },
  { key: 'QUERY_CACHE_MAX_SIZE', label: t('retrieval.labels.QUERY_CACHE_MAX_SIZE'), min: 10, max: 1000, desc: t('retrieval.descs.QUERY_CACHE_MAX_SIZE') },
  { key: 'QUERY_CACHE_TTL', label: t('retrieval.labels.QUERY_CACHE_TTL'), min: 30, max: 3600, desc: t('retrieval.descs.QUERY_CACHE_TTL') },
  { key: 'MEMORY_WARM_MAX', label: t('retrieval.labels.MEMORY_WARM_MAX'), min: 0, max: 100, desc: t('retrieval.descs.MEMORY_WARM_MAX') },
  { key: 'MEMORY_COLD_MAX', label: t('retrieval.labels.MEMORY_COLD_MAX'), min: 0, max: 10000, desc: t('retrieval.descs.MEMORY_COLD_MAX') },
  { key: 'MEMORY_DISTILL_BATCH', label: t('retrieval.labels.MEMORY_DISTILL_BATCH'), min: 5, max: 100, desc: t('retrieval.descs.MEMORY_DISTILL_BATCH') },
])

const isModified = computed(() => {
  if (!defaults.value) return false
  for (const k of Object.keys(defaults.value)) {
    if (config[k] !== defaults.value[k]) return true
  }
  return false
})

onMounted(async () => {
  await loadConfig()
})

async function loadConfig() {
  loading.value = true
  try {
    const data = await get<Record<string, boolean | number> & { _defaults?: Record<string, boolean | number> }>('/retrieval/config')
    Object.keys(data).forEach(k => {
      if (k !== '_defaults') config[k] = data[k]
    })
    defaults.value = data._defaults || {}
  } catch (e: any) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

async function saveConfig() {
  saving.value = true
  try {
    const updates: Record<string, boolean | number> = {}
    for (const k of Object.keys(defaults.value)) {
      if (config[k] !== undefined) updates[k] = config[k]
    }
    const data = await put<{ current: Record<string, boolean | number> }>('/retrieval/config', { updates })
    Object.keys(data.current || {}).forEach(k => {
      if (k !== '_defaults') config[k] = data.current[k]
    })
    message.success(t('retrieval.saved'))
  } catch (e: any) {
    message.error(e.message)
  } finally {
    saving.value = false
  }
}

async function resetConfig() {
  try {
    const data = await post<{ current: Record<string, boolean | number>; reset_keys: string[] }>('/retrieval/config/reset', {})
    Object.keys(data.current || {}).forEach(k => {
      if (k !== '_defaults') config[k] = data.current[k]
    })
    message.success((t('retrieval.restored') as string).replace('{n}', String(data.reset_keys?.length || 0)))
  } catch (e: any) {
    message.error(e.message)
  }
}

async function runTest() {
  if (!testQuery.value.trim()) {
    message.warning(t('retrieval.testNeedQuery'))
    return
  }
  testing.value = true
  testError.value = ''
  testResults.value = []
  testMetrics.value = null
  try {
    const data = await post<{
      query: string
      results: RetrievalTestItem[]
      count: number
      metrics?: Record<string, number> | null
      error?: string
    }>('/retrieval/test', {
      query: testQuery.value,
      top_k: testTopK.value,
      expect_keywords: splitKeywords(testExpect.value),
    })
    testResults.value = data.results || []
    testCount.value = data.count || 0
    testMetrics.value = data.metrics || null
    if (data.error) testError.value = data.error
  } catch (e: any) {
    testError.value = e.message
  } finally {
    testing.value = false
  }
}

function splitKeywords(s: string): string[] {
  return s.split(/[,，]/).map(x => x.trim()).filter(Boolean)
}

function pct(v: unknown): string {
  return typeof v === 'number' ? `${(v * 100).toFixed(1)}%` : '—'
}

async function runEval() {
  const cases = parseEvalCases(evalText.value)
  if (!cases.length) {
    message.warning(t('retrieval.evalNeedCases'))
    return
  }
  evaluating.value = true
  try {
    const data = await post<{
      top_k: number
      cases: Array<Record<string, unknown>>
      cases_total?: number
      aggregate?: { recall_macro: number; precision_macro: number; f1_macro: number; mrr_macro: number; hit_rate: number }
    }>('/retrieval/evaluate', { cases, top_k: testTopK.value })
    evalReport.value = data
  } catch (e: any) {
    message.error(e.message)
  } finally {
    evaluating.value = false
  }
}

function parseEvalCases(text: string): Array<{ query: string; expect_keywords?: string[] }> {
  return text.split('\n').map(line => line.trim()).filter(Boolean).map(line => {
    const [query, kws] = line.split(/[|｜]/)
    const expect = splitKeywords(kws || '')
    return expect.length ? { query: query.trim(), expect_keywords: expect }
      : { query: query.trim() }
  }).filter(c => c.query)
}

function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

function tsSuffix(): string {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
}

async function exportConfig() {
  try {
    const data = await get<any>('/retrieval/config')
    const modified = Object.keys(data._defaults || {}).filter(
      k => k !== '_defaults' && data[k] !== data._defaults[k])
    downloadJson(`retrieval-config-${tsSuffix()}.json`, {
      exported_at: new Date().toISOString(),
      modified_keys: modified,
      values: Object.fromEntries(Object.keys(data).filter(k => k !== '_defaults').map(k => [k, data[k]])),
      defaults: data._defaults,
    })
    message.success(t('retrieval.exportDone'))
  } catch (e: any) {
    message.error(e.message)
  }
}

function exportEvalReport() {
  if (!evalReport.value) return
  downloadJson(`retrieval-eval-${tsSuffix()}.json`, {
    exported_at: new Date().toISOString(),
    top_k: evalReport.value.top_k,
    ...evalReport.value,
  })
  message.success(t('retrieval.exportDone'))
}

function isDefault(key: string): boolean {
  return defaults.value[key] !== undefined && config[key] === defaults.value[key]
}
</script>

<template>
  <div class="retrieval-view">
    <div class="page-header">
      <h2 class="view-title view-title-icon"><ViewTitleIcon name="retrieval" /> {{ t('retrieval.title') }}</h2>
      <div class="page-actions">
        <n-button tertiary @click="exportConfig">{{ t('retrieval.exportConfig') }}</n-button>
        <n-button :disabled="!isModified" type="primary" :loading="saving" @click="saveConfig">
          {{ t('retrieval.save') }}
        </n-button>
        <n-popconfirm @positive-click="resetConfig">
          <template #trigger>
            <n-button :disabled="!isModified" type="warning" ghost>{{ t('retrieval.resetBtn') }}</n-button>
          </template>
          {{ t('retrieval.resetConfirm') }}
        </n-popconfirm>
      </div>
    </div>

    <n-spin :show="loading">
      <n-tabs type="line" animated v-model:value="activeTab">
        <!-- 开关 -->
        <n-tab-pane name="switches" :tab="t('retrieval.tabSwitches')">
          <div class="config-grid">
            <div v-for="item in boolKeys" :key="item.key" class="config-card glass-panel">
              <div class="card-header">
                <span class="card-label">{{ item.label }}</span>
                <n-switch :value="config[item.key] as boolean" @update:value="(v: boolean) => config[item.key] = v" />
              </div>
              <p class="card-desc">{{ item.desc }}</p>
              <n-tag v-if="!isDefault(item.key)" size="tiny" type="warning">{{ t('retrieval.modified') }}</n-tag>
            </div>
          </div>
        </n-tab-pane>

        <!-- 权重与阈值 -->
        <n-tab-pane name="weights" :tab="t('retrieval.tabWeights')">
          <div class="slider-section">
            <h3 class="section-title">{{ t('retrieval.weightTitle') }}</h3>
            <div v-for="item in floatSliders" :key="item.key" class="slider-row">
              <div class="slider-header">
                <span class="slider-label">{{ item.label }}</span>
                <span class="slider-value">{{ (config[item.key] as number)?.toFixed(2) }}</span>
                <n-tag v-if="!isDefault(item.key)" size="tiny" type="warning">{{ t('retrieval.modified') }}</n-tag>
              </div>
              <n-slider
                :value="config[item.key] as number"
                @update:value="(v: number) => config[item.key] = v"
                :min="item.min" :max="item.max" :step="item.step"
              />
              <p class="slider-desc">{{ item.desc }}</p>
            </div>
          </div>
        </n-tab-pane>

        <!-- 数值参数 -->
        <n-tab-pane name="numbers" :tab="t('retrieval.tabNumbers')">
          <div class="number-grid">
            <div v-for="item in intInputs" :key="item.key" class="number-card glass-panel">
              <div class="number-header">
                <span class="number-label">{{ item.label }}</span>
                <n-tag v-if="!isDefault(item.key)" size="tiny" type="warning">{{ t('retrieval.modified') }}</n-tag>
              </div>
              <n-input-number
                :value="config[item.key] as number"
                @update:value="(v: number | null) => { if (v !== null) config[item.key] = v }"
                :min="item.min" :max="item.max"
                size="small"
                class="number-input"
              />
              <p class="number-desc">{{ item.desc }}</p>
            </div>
          </div>
        </n-tab-pane>

        <!-- 召回测试 -->
        <n-tab-pane name="test" :tab="t('retrieval.tabTest')">
          <div class="test-section glass-panel">
            <h3 class="section-title">{{ t('retrieval.testTitle') }}</h3>
            <p class="test-desc">{{ t('retrieval.testDesc') }}</p>
            <div class="test-input-row">
              <n-input
                v-model:value="testQuery"
                :placeholder="t('retrieval.testPlaceholder')"
                class="test-query"
                @keyup.enter="runTest"
              />
              <n-input-number
                v-model:value="testTopK"
                :min="1" :max="20"
                size="small"
                class="test-top-k"
                placeholder="Top-K"
              />
              <n-button class="test-run-button" type="primary" :loading="testing" @click="runTest">{{ t('retrieval.testBtn') }}</n-button>
            </div>
            <n-input
              v-model:value="testExpect"
              :placeholder="t('retrieval.expectPlaceholder')"
              size="small"
              class="test-expect-input"
            />

            <div v-if="testError" class="test-error">
              <n-tag type="error">{{ testError }}</n-tag>
            </div>

            <div v-if="testMetrics" class="metrics-grid">
              <div v-if="testMetrics.has_expect" class="metric-chip">
                <span class="metric-label">{{ t('retrieval.recall') }}</span>
                <span class="metric-value">{{ pct(testMetrics.recall) }}</span>
              </div>
              <div v-if="testMetrics.has_expect" class="metric-chip">
                <span class="metric-label">{{ t('retrieval.precision') }}</span>
                <span class="metric-value">{{ pct(testMetrics.precision) }}</span>
              </div>
              <div v-if="testMetrics.has_expect" class="metric-chip">
                <span class="metric-label">{{ t('retrieval.f1') }}</span>
                <span class="metric-value">{{ pct(testMetrics.f1) }}</span>
              </div>
              <div v-if="testMetrics.has_expect" class="metric-chip">
                <span class="metric-label">{{ t('retrieval.mrr') }}</span>
                <span class="metric-value">{{ pct(testMetrics.mrr) }}</span>
              </div>
              <div v-if="testMetrics.has_expect" class="metric-chip">
                <span class="metric-label">{{ t('retrieval.firstHit') }}</span>
                <span class="metric-value">{{ testMetrics.first_hit_rank || '—' }}</span>
              </div>
              <div class="metric-chip">
                <span class="metric-label">{{ t('retrieval.aboveThr') }}</span>
                <span class="metric-value">{{ testMetrics.above_threshold }}/{{ testMetrics.returned }}</span>
              </div>
              <div class="metric-chip">
                <span class="metric-label">{{ t('retrieval.latency') }}</span>
                <span class="metric-value">{{ testMetrics.latency_ms }}ms</span>
              </div>
              <div class="metric-chip">
                <span class="metric-label">{{ t('retrieval.scoreStats') }}</span>
                <span class="metric-value">{{ testMetrics.score_max }} / {{ testMetrics.score_mean }} / {{ testMetrics.score_min }}</span>
              </div>
            </div>

            <div v-if="testResults.length" class="test-results">
              <div class="results-header">
                <span>{{ (t('retrieval.hits') as string).replace('{n}', String(testCount)) }}</span>
              </div>
              <div v-for="(r, i) in testResults" :key="r.id || i" class="result-item" :class="{ 'result-matched': r.matched }">
                <div class="result-header">
                  <span class="result-rank">#{{ i + 1 }}</span>
                  <span class="result-score">{{ t('retrieval.score') }}: {{ (r.score || 0).toFixed(4) }}</span>
                  <span class="result-importance">{{ t('retrieval.importance') }}: {{ r.importance || 0 }}</span>
                  <n-tag v-if="r.matched" size="tiny" type="success">{{ t('retrieval.matched') }}</n-tag>
                  <n-tag v-if="r.emotion_label" size="tiny" type="info">{{ r.emotion_label }}</n-tag>
                  <n-tag v-if="r.source" size="tiny">{{ r.source }}</n-tag>
                </div>
                <p class="result-summary">{{ r.summary }}</p>
              </div>
            </div>
            <div v-else-if="!testing && testQuery && !testError" class="test-empty">
              {{ t('retrieval.testEmpty') }}
            </div>
          </div>

          <!-- 批量评测 -->
          <div class="test-section glass-panel eval-section">
            <div class="eval-header">
              <h3 class="section-title">{{ t('retrieval.evalTitle') }}</h3>
              <n-button v-if="evalReport" size="small" tertiary @click="exportEvalReport">
                {{ t('retrieval.exportReport') }}
              </n-button>
            </div>
            <p class="test-desc">{{ t('retrieval.evalDesc') }}</p>
            <n-input
              v-model:value="evalText"
              type="textarea"
              :rows="5"
              :placeholder="t('retrieval.evalPlaceholder')"
            />
            <div class="test-input-row eval-run-row">
              <n-button type="primary" :loading="evaluating" @click="runEval">
                {{ t('retrieval.evalBtn') }}
              </n-button>
            </div>

            <div v-if="evalReport" class="eval-summary">
              <div class="metrics-grid">
                <div class="metric-chip">
                  <span class="metric-label">{{ t('retrieval.recall') }}</span>
                  <span class="metric-value">{{ pct(evalReport.aggregate?.recall_macro) }}</span>
                </div>
                <div class="metric-chip">
                  <span class="metric-label">{{ t('retrieval.precision') }}</span>
                  <span class="metric-value">{{ pct(evalReport.aggregate?.precision_macro) }}</span>
                </div>
                <div class="metric-chip">
                  <span class="metric-label">{{ t('retrieval.f1') }}</span>
                  <span class="metric-value">{{ pct(evalReport.aggregate?.f1_macro) }}</span>
                </div>
                <div class="metric-chip">
                  <span class="metric-label">{{ t('retrieval.mrr') }}</span>
                  <span class="metric-value">{{ pct(evalReport.aggregate?.mrr_macro) }}</span>
                </div>
                <div class="metric-chip">
                  <span class="metric-label">{{ t('retrieval.evalHitRate') }}</span>
                  <span class="metric-value">{{ pct(evalReport.aggregate?.hit_rate) }}</span>
                </div>
                <div class="metric-chip">
                  <span class="metric-label">{{ t('retrieval.avgLatency') }}</span>
                  <span class="metric-value">{{ evalReport.aggregate?.latency_avg_ms }}ms</span>
                </div>
                <div class="metric-chip">
                  <span class="metric-label">{{ t('retrieval.p95Latency') }}</span>
                  <span class="metric-value">{{ evalReport.aggregate?.latency_p95_ms }}ms</span>
                </div>
                <div class="metric-chip">
                  <span class="metric-label">{{ t('retrieval.casesStat') }}</span>
                  <span class="metric-value">{{ evalReport.cases_ok }}/{{ evalReport.cases_total }}</span>
                </div>
              </div>
              <div v-for="(c, i) in (evalReport.cases || [])" :key="i" class="eval-case" :class="{ 'eval-case-failed': !c.metrics }">
                <div class="eval-case-header">
                  <span class="eval-case-query">{{ c.query }}</span>
                  <n-tag v-if="!c.metrics" size="tiny" type="error">{{ c.error || 'failed' }}</n-tag>
                  <template v-else-if="c.metrics.has_expect">
                    <n-tag size="tiny" :type="c.metrics.hit ? 'success' : 'warning'">
                      {{ t('retrieval.recall') }} {{ pct(c.metrics.recall) }}
                    </n-tag>
                    <n-tag size="tiny">{{ t('retrieval.precision') }} {{ pct(c.metrics.precision) }}</n-tag>
                    <n-tag size="tiny">F1 {{ pct(c.metrics.f1) }}</n-tag>
                  </template>
                  <n-tag v-else size="tiny">{{ c.count ?? 0 }} {{ t('retrieval.hitsShort') }}</n-tag>
                  <span class="eval-case-latency">{{ c.metrics?.latency_ms ?? '—' }}ms</span>
                </div>
              </div>
            </div>
          </div>
        </n-tab-pane>
      </n-tabs>
    </n-spin>
  </div>
</template>

<style scoped>
.retrieval-view {
  padding: 0 0 24px;
}

.page-header .view-title {
  min-width: 0;
}

.view-title {
  font-family: 'Noto Serif SC', serif;
  margin: 0;
}

.config-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 280px), 1fr));
  gap: 12px;
}

.config-card {
  min-width: 0;
  padding: 14px 16px;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.card-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--dendro);
}

.card-desc {
  font-size: 12px;
  color: var(--moon-dim);
  margin: 0 0 6px;
  line-height: 1.5;
}

.slider-section {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-title {
  font-size: 14px;
  color: var(--dendro);
  margin: 0 0 8px;
}

.slider-row {
  padding: 12px 16px;
  border-radius: 10px;
  background: rgba(15, 31, 23, 0.5);
  border: 1px solid var(--glass-border);
}

.slider-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.slider-label {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--moon);
  flex: 1;
}

.slider-value {
  font-size: 13px;
  color: var(--dendro);
  font-family: 'JetBrains Mono', monospace;
  min-width: 40px;
  text-align: right;
}

.slider-desc {
  font-size: 11.5px;
  color: var(--moon-dim);
  margin: 6px 0 0;
  opacity: 0.7;
}

.number-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 240px), 1fr));
  gap: 12px;
}

.number-card {
  min-width: 0;
  padding: 12px 14px;
}

.number-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.number-label {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--moon);
}

.number-input {
  width: 100%;
}

.number-desc {
  font-size: 11.5px;
  color: var(--moon-dim);
  margin: 6px 0 0;
  opacity: 0.7;
}

.test-section {
  padding: 20px;
}

.test-desc {
  font-size: 12.5px;
  color: var(--moon-dim);
  margin: 0 0 14px;
}

.test-input-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 100px max-content;
  gap: 10px;
  align-items: center;
}

.test-query {
  min-width: 0;
  grid-column: 1 / -1;
}

.test-top-k {
  width: 100px;
  grid-column: 2;
  justify-self: end;
}

.test-run-button {
  min-width: max-content;
  grid-column: 3;
}

.test-error {
  margin-top: 12px;
}

.test-results {
  margin-top: 16px;
}

.results-header {
  font-size: 13px;
  color: var(--dendro);
  margin-bottom: 10px;
  font-weight: 600;
}

.result-item {
  min-width: 0;
  padding: 11px 2px;
  border-bottom: 1px solid var(--glass-border);
}

.result-item:last-child {
  border-bottom: 0;
}

.result-header {
  display: flex;
  min-width: 0;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 8px;
  margin-bottom: 6px;
  font-size: 12px;
}

.result-header :deep(.n-tag) {
  max-width: 100%;
}

.result-header :deep(.n-tag__content) {
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: normal;
}

.result-rank {
  flex-shrink: 0;
  color: var(--dendro);
  font-weight: 700;
  font-family: 'JetBrains Mono', monospace;
}

.result-score {
  min-width: 0;
  color: var(--moon);
  font-family: 'JetBrains Mono', monospace;
  overflow-wrap: anywhere;
}

.result-importance {
  min-width: 0;
  color: var(--moon-dim);
  font-family: 'JetBrains Mono', monospace;
  overflow-wrap: anywhere;
}

.result-summary {
  min-width: 0;
  font-size: 13px;
  color: var(--moon);
  margin: 0;
  line-height: 1.6;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.test-empty {
  text-align: center;
  padding: 40px 0;
  color: var(--moon-dim);
  font-size: 13px;
}

.test-expect-input {
  margin-top: 10px;
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 150px), 1fr));
  gap: 8px;
  margin-top: 14px;
}

.metric-chip {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
  padding: 8px 10px;
  border-left: 2px solid rgba(145, 232, 102, 0.25);
  background: rgba(255, 255, 255, 0.018);
}

.metric-label {
  font-size: 11px;
  color: var(--moon-dim);
}

.metric-value {
  min-width: 0;
  font-size: 14px;
  font-weight: 700;
  color: var(--dendro);
  font-family: 'JetBrains Mono', monospace;
  line-height: 1.35;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.result-matched {
  border-left: 2px solid rgba(102, 187, 106, 0.45);
  padding-left: 10px;
}

.eval-section {
  margin-top: 16px;
}

.eval-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.eval-header .section-title {
  margin: 0;
}

.eval-run-row {
  display: flex;
  margin-top: 10px;
}

.eval-case {
  min-width: 0;
  padding: 10px 2px;
  border-bottom: 1px solid var(--glass-border);
}

.eval-case:last-child {
  border-bottom: 0;
}

.eval-case-failed {
  padding-left: 10px;
  border-left: 2px solid rgba(218, 82, 82, 0.45);
}

.eval-case-header {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.eval-case-header :deep(.n-tag) {
  max-width: 100%;
}

.eval-case-header :deep(.n-tag__content) {
  min-width: 0;
  overflow-wrap: anywhere;
  white-space: normal;
}

.eval-case-query {
  min-width: min(100%, 180px);
  color: var(--moon);
  font-size: 13px;
  font-weight: 600;
  flex: 1 1 220px;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.eval-case-latency {
  max-width: 100%;
  font-size: 12px;
  color: var(--moon-dim);
  font-family: 'JetBrains Mono', monospace;
  overflow-wrap: anywhere;
}

@media (max-width: 768px) {
  .retrieval-view :deep(.n-tabs-nav-scroll-content) {
    min-width: max-content;
  }

  .page-actions > :deep(.n-button),
  .page-actions > :deep(.n-popconfirm) {
    flex: 1 1 140px;
  }

  .page-actions > :deep(.n-popconfirm) .n-button {
    width: 100%;
  }

  .test-section {
    padding: 16px;
  }
}

@media (max-width: 520px) {
  .page-actions {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 6px;
  }

  .page-actions > :deep(.n-button),
  .page-actions > :deep(.n-popconfirm) {
    min-width: 0;
    width: 100%;
    flex-basis: auto;
  }

  .page-actions :deep(.n-button) {
    width: 100%;
    padding-inline: 6px;
  }

  .test-input-row {
    grid-template-columns: minmax(0, 1fr) max-content;
  }

  .test-top-k {
    width: 100px;
    min-width: 0;
    grid-column: 1;
    justify-self: start;
  }

  .test-run-button {
    grid-column: 2;
    align-self: stretch;
  }

  .eval-header {
    align-items: flex-start;
    flex-direction: column;
    gap: 8px;
  }
}
</style>
