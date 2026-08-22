<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import {
  NSwitch, NSlider, NInputNumber, NButton, NInput, NTag, NPopconfirm,
  NTabs, NTabPane, NSpin, useMessage,
} from 'naive-ui'
import { get, put, post } from '../api'
import { t } from '../i18n'

const message = useMessage()
const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const activeTab = ref('switches')

const config = reactive<Record<string, boolean | number>>({})
const defaults = ref<Record<string, boolean | number>>({})
const testQuery = ref('')
const testTopK = ref(5)
const testResults = ref<any[]>([])
const testCount = ref(0)
const testError = ref('')

const boolKeys = [
  { key: 'RERANKER_ENABLED', label: 'Reranker 精排', desc: '启用交叉编码器对召回结果重排序，显著提升相关性。推荐开启，是降噪核心手段' },
  { key: 'QUERY_TRANSFORM_ENABLED', label: '查询改写', desc: '将口语化查询改写为检索关键词，并推断关联词（如"饮食偏好"→补充"香菜 豆浆 川菜"）。实测提升FTS召回4.7%' },
  { key: 'HYDE_ENABLED', label: 'HyDE 假设文档嵌入', desc: '生成假设答案文档与原查询混合检索。实测引入发散噪声、降低精确率，推荐关闭' },
  { key: 'MEMORY_RETRIEVAL_DIFFUSION', label: '检索扩散', desc: '生成额外查询目标+概念图扩散，配合 Reranker 兜底。适合记忆量大的场景' },
  { key: 'RETRIEVAL_SMART_SKIP', label: '智能跳过检索', desc: '闲聊/问候等无需检索的场景自动跳过，减少延迟和API消耗' },
  { key: 'RETRIEVAL_PARALLEL_TRANSFORM', label: '并行查询变换', desc: '查询改写与检索并行执行，降低延迟。关闭则串行执行，改写完成后才检索' },
  { key: 'RETRIEVAL_PARALLEL_SEARCH', label: '并行检索', desc: '多路召回（FTS+向量+KG）并行执行，降低延迟。关闭则串行逐路检索' },
  { key: 'QUERY_CACHE_ENABLED', label: '查询缓存', desc: '相似查询复用缓存结果，减少重复计算。高频对话场景效果显著' },
  { key: 'PARENT_CHILD_CHUNK_ENABLED', label: '父子分块', desc: '小块检索+大块返回上下文，兼顾精度与完整度。长文档场景推荐开启' },
  { key: 'KG_V2_ENABLED', label: '知识图谱 V2', desc: '时序事实演化（如"住北京→搬上海"自动覆盖旧事实）+社区发现。每条记忆额外消耗2~3次LLM调用，免费额度有限时建议关闭' },
  { key: 'CONTEXTUAL_RETRIEVAL_ENABLED', label: '上下文检索', desc: '为分块添加文档级上下文前缀，提升检索精度。推荐开启' },
  { key: 'MEMORY_DISTILL_ENABLED', label: '记忆蒸馏', desc: '定期将超200条的冷记忆蒸馏压缩。当前"写入即蒸馏"架构已覆盖需求，定时蒸馏冗余，建议关闭' },
]

const floatSliders = [
  { key: 'RAG_RERANK_WEIGHT', label: 'Reranker 权重', min: 0, max: 1, step: 0.05, desc: 'Reranker 交叉编码器分数在最终排序中的权重。0.60为实测最优，确保相关性判断主导排序' },
  { key: 'RAG_KG_WEIGHT', label: '知识图谱权重', min: 0, max: 1, step: 0.05, desc: '知识图谱增强分数的权重。0.10为推荐值，KG仅做微调辅助' },
  { key: 'RAG_IMPORTANCE_WEIGHT', label: '重要性权重', min: 0, max: 1, step: 0.05, desc: '记忆重要性分数的权重。0.10为推荐值，过高会导致高重要性但不相关的记忆挤掉相关记忆' },
  { key: 'RAG_MIN_FINAL_SCORE', label: '最低相关分', min: 0, max: 0.5, step: 0.01, desc: '低于此分数的结果视为噪声丢弃。0.08为推荐值，过低会引入弱相关噪声' },
  { key: 'RAG_VEC_MAX_DISTANCE', label: '向量距离阈值', min: 0.5, max: 2.0, step: 0.05, desc: '超过此L2距离的向量软降权保留。1.15为推荐值，过宽会引入远距离噪声向量' },
  { key: 'RAG_VEC_SOFT_PENALTY', label: '距离降权系数', min: 0, max: 1, step: 0.05, desc: '超阈值向量的降权力度。0.6为推荐值，越接近1.0降权越温和，Reranker越有机会捞回' },
  { key: 'QUERY_CACHE_THRESHOLD', label: '缓存相似度阈值', min: 0.5, max: 1, step: 0.01, desc: '查询向量相似度高于此值时复用缓存结果。0.88为推荐值，过低会误命中不相关缓存' },
  { key: 'MEMORY_WARM_VEC_WEIGHT', label: '温记忆向量权重', min: 0, max: 1, step: 0.1, desc: '温记忆检索中向量相似度 vs FTS的权重配比。0.6=向量6:FTS4' },
  { key: 'EMOTION_TRIGGER_THRESHOLD', label: '情绪触发阈值', min: 0, max: 1, step: 0.05, desc: '情绪强度超过此值时触发安慰记忆检索。0.5为推荐值，过低会频繁触发情绪检索' },
]

const intInputs = [
  { key: 'RAG_RECALL_LIMIT', label: '每路召回上限', min: 10, max: 500, desc: '每路召回Top-N候选数量。120为推荐值，过大会增加Reranker压力和延迟' },
  { key: 'RAG_RERANK_LIMIT', label: 'Reranker 精排上限', min: 5, max: 200, desc: '送入Reranker的最大候选数。60为推荐值，过多会降低排序稳定性和速度' },
  { key: 'QUERY_EXPAND_COUNT', label: '多查询扩展数', min: 0, max: 10, desc: '0=关闭（推荐），2=生成2个额外查询。实测引入噪声、降低精确率，不建议开启' },
  { key: 'RERANKER_OVERSAMPLE_RATIO', label: 'Reranker 过采样比', min: 1, max: 10, desc: 'Reranker输入过采样倍数。3为推荐值，从候选池中多取3倍送入初筛' },
  { key: 'QUERY_CACHE_MAX_SIZE', label: '缓存最大条目', min: 10, max: 1000, desc: '查询缓存的最大条目数。256为推荐值，日常对话足够' },
  { key: 'QUERY_CACHE_TTL', label: '缓存 TTL (秒)', min: 30, max: 3600, desc: '缓存条目生存时间。300秒=5分钟，过期后重新检索' },
  { key: 'MEMORY_WARM_MAX', label: '温记忆最大条目', min: 0, max: 100, desc: '温记忆区最大保留条目数。10为推荐值，近期高频记忆快速访问' },
  { key: 'MEMORY_COLD_MAX', label: '冷记忆最大条目', min: 0, max: 10000, desc: '冷记忆区最大保留条目数，0=不限。一般无需限制' },
  { key: 'MEMORY_DISTILL_BATCH', label: '蒸馏批大小', min: 5, max: 100, desc: '每次蒸馏处理的记忆条目数。30为推荐值，平衡效率与LLM调用开销' },
]

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
    const data = await get<any>('/retrieval/config')
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
    const updates: Record<string, any> = {}
    for (const k of Object.keys(defaults.value)) {
      if (config[k] !== undefined) updates[k] = config[k]
    }
    const data = await put('/retrieval/config', { updates })
    Object.keys(data.current || {}).forEach(k => {
      if (k !== '_defaults') config[k] = data.current[k]
    })
    message.success('检索配置已保存并热生效')
  } catch (e: any) {
    message.error(e.message)
  } finally {
    saving.value = false
  }
}

async function resetConfig() {
  try {
    const data = await post('/retrieval/config/reset', {})
    Object.keys(data.current || {}).forEach(k => {
      if (k !== '_defaults') config[k] = data.current[k]
    })
    message.success(`已恢复 ${data.reset_keys?.length || 0} 项默认值`)
  } catch (e: any) {
    message.error(e.message)
  }
}

async function runTest() {
  if (!testQuery.value.trim()) {
    message.warning('请输入测试查询')
    return
  }
  testing.value = true
  testError.value = ''
  testResults.value = []
  try {
    const data = await post('/retrieval/test', { query: testQuery.value, top_k: testTopK.value })
    testResults.value = data.results || []
    testCount.value = data.count || 0
    if (data.error) testError.value = data.error
  } catch (e: any) {
    testError.value = e.message
  } finally {
    testing.value = false
  }
}

function isDefault(key: string): boolean {
  return defaults.value[key] !== undefined && config[key] === defaults.value[key]
}
</script>

<template>
  <div class="retrieval-view">
    <div class="view-header">
      <h2 class="view-title">检索配置</h2>
      <div class="header-actions">
        <n-button :disabled="!isModified" type="primary" :loading="saving" @click="saveConfig">
          保存配置
        </n-button>
        <n-popconfirm @positive-click="resetConfig">
          <template #trigger>
            <n-button :disabled="!isModified" type="warning" ghost>恢复默认</n-button>
          </template>
          确认恢复所有检索配置为默认值？
        </n-popconfirm>
      </div>
    </div>

    <n-spin :show="loading">
      <n-tabs type="line" animated v-model:value="activeTab">
        <!-- 开关 -->
        <n-tab-pane name="switches" tab="功能开关">
          <div class="config-grid">
            <div v-for="item in boolKeys" :key="item.key" class="config-card glass-panel">
              <div class="card-header">
                <span class="card-label">{{ item.label }}</span>
                <n-switch :value="config[item.key] as boolean" @update:value="(v: boolean) => config[item.key] = v" />
              </div>
              <p class="card-desc">{{ item.desc }}</p>
              <n-tag v-if="!isDefault(item.key)" size="tiny" type="warning">已修改</n-tag>
            </div>
          </div>
        </n-tab-pane>

        <!-- 权重与阈值 -->
        <n-tab-pane name="weights" tab="权重与阈值">
          <div class="slider-section">
            <h3 class="section-title">权重调节</h3>
            <div v-for="item in floatSliders" :key="item.key" class="slider-row">
              <div class="slider-header">
                <span class="slider-label">{{ item.label }}</span>
                <span class="slider-value">{{ (config[item.key] as number)?.toFixed(2) }}</span>
                <n-tag v-if="!isDefault(item.key)" size="tiny" type="warning">已修改</n-tag>
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
        <n-tab-pane name="numbers" tab="数值参数">
          <div class="number-grid">
            <div v-for="item in intInputs" :key="item.key" class="number-card glass-panel">
              <div class="number-header">
                <span class="number-label">{{ item.label }}</span>
                <n-tag v-if="!isDefault(item.key)" size="tiny" type="warning">已修改</n-tag>
              </div>
              <n-input-number
                :value="config[item.key] as number"
                @update:value="(v: number | null) => { if (v !== null) config[item.key] = v }"
                :min="item.min" :max="item.max"
                size="small"
                style="width: 100%"
              />
              <p class="number-desc">{{ item.desc }}</p>
            </div>
          </div>
        </n-tab-pane>

        <!-- 召回测试 -->
        <n-tab-pane name="test" tab="召回测试">
          <div class="test-section glass-panel">
            <h3 class="section-title">实时召回测试</h3>
            <p class="test-desc">输入查询语句，测试当前配置下的召回效果。修改配置后需先保存再测试。</p>
            <div class="test-input-row">
              <n-input
                v-model:value="testQuery"
                placeholder="输入测试查询，如：今天天气怎么样"
                style="flex: 1"
                @keyup.enter="runTest"
              />
              <n-input-number
                v-model:value="testTopK"
                :min="1" :max="20"
                size="small"
                style="width: 100px"
                placeholder="Top-K"
              />
              <n-button type="primary" :loading="testing" @click="runTest">测试召回</n-button>
            </div>

            <div v-if="testError" class="test-error">
              <n-tag type="error">{{ testError }}</n-tag>
            </div>

            <div v-if="testResults.length" class="test-results">
              <div class="results-header">
                <span>命中 {{ testCount }} 条结果</span>
              </div>
              <div v-for="(r, i) in testResults" :key="r.id || i" class="result-item">
                <div class="result-header">
                  <span class="result-rank">#{{ i + 1 }}</span>
                  <span class="result-score">分数: {{ (r.score || 0).toFixed(4) }}</span>
                  <span class="result-importance">重要性: {{ r.importance || 0 }}</span>
                  <n-tag v-if="r.emotion_label" size="tiny" type="info">{{ r.emotion_label }}</n-tag>
                  <n-tag v-if="r.source" size="tiny">{{ r.source }}</n-tag>
                </div>
                <p class="result-summary">{{ r.summary }}</p>
              </div>
            </div>
            <div v-else-if="!testing && testQuery && !testError" class="test-empty">
              点击"测试召回"查看结果
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

.view-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.view-title {
  font-family: 'Noto Serif SC', serif;
  margin: 0;
}

.header-actions {
  display: flex;
  gap: 8px;
}

.config-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}

.config-card {
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
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}

.number-card {
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
  display: flex;
  gap: 10px;
  align-items: center;
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
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(10, 20, 14, 0.6);
  border: 1px solid var(--glass-border);
  margin-bottom: 8px;
}

.result-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  font-size: 12px;
}

.result-rank {
  color: var(--dendro);
  font-weight: 700;
  font-family: 'JetBrains Mono', monospace;
}

.result-score {
  color: var(--moon);
  font-family: 'JetBrains Mono', monospace;
}

.result-importance {
  color: var(--moon-dim);
  font-family: 'JetBrains Mono', monospace;
}

.result-summary {
  font-size: 13px;
  color: var(--moon);
  margin: 0;
  line-height: 1.6;
}

.test-empty {
  text-align: center;
  padding: 40px 0;
  color: var(--moon-dim);
  font-size: 13px;
}
</style>