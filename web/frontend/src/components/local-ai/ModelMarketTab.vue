<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NAlert, NButton, NEmpty, NInput, NSelect, NSpin, NTabPane, NTabs, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore, type HubCategory, type HubSearchResult, type HubSource, type RemoteInspection } from '../../stores/localAi'
import StoragePickerDialog from './StoragePickerDialog.vue'

const store = useLocalAiStore()
const message = useMessage()

// ── 已安装模型：匹配 catalog_id 或安装来源 repository，已下载好的标「已安装」不再提供下载 ──
// 本地安装 id 带前缀（builtin:/local:），市场 id 是 owner/name 完整路径，
// 因此按末尾模型名归一化匹配，避免已下载的 bge-small-zh-v1.5 在市场里仍可反复下载。
const installedKeys = computed(() => {
  const keys = new Set<string>()
  for (const model of store.models) {
    keys.add(model.catalog_id)
    const repository = model.metadata?.repository
    if (typeof repository === 'string' && repository) keys.add(repository)
    const base = String(model.catalog_id || '').split(/[:/]/).pop()
    if (base) keys.add(base)
  }
  return keys
})
const isInstalled = (id: string) => {
  if (installedKeys.value.has(id)) return true
  const base = String(id || '').split('/').pop()
  return !!base && installedKeys.value.has(base)
}

// ── 分类节点：从后端能力映射动态加载（非前端写死），失败时用内置兜底 ──
const CATEGORY_FALLBACK: HubCategory[] = [
  { key: 'all', label: '全部', desc: '功能节点（已过滤对话大模型）', pipelines: [] },
  { key: 'embedding', label: '向量嵌入', desc: 'Embedding 小模型', pipelines: [] },
  { key: 'rerank', label: '语义重排', desc: 'Rerank 小模型', pipelines: [] },
  { key: 'chat', label: '对话', desc: '大模型，本机通常无法运行，慎用', pipelines: [] },
  { key: 'other', label: '其他', desc: '分类 / 翻译 / 语音等', pipelines: [] },
]
const CATEGORIES = ref<HubCategory[]>(CATEGORY_FALLBACK)
const hubCategory = ref('all')

async function loadCategories() {
  try {
    const list = await store.hubCategories()
    if (Array.isArray(list) && list.length) CATEGORIES.value = list
  } catch {
    /* 后端不可用或旧版本时保留内置兜底 */
  }
}

// ── 在线获取：双源并发检索，跨源同 id 合并为一行（来源标注全部源） ──
// 逐步加载：一次拉取较多（100，后端上限），前端先显示 20 条，
// 用户滚动到底部每次再显示 10 条，直到全部展示。
const MARKET_FETCH = 100
const MARKET_FIRST = 20
const MARKET_STEP = 10
const hubQuery = ref('')
const hubSource = ref<HubSource>('all')
const hubSourceOptions = [
  { label: '全部来源', value: 'all' },
  { label: 'HuggingFace 镜像', value: 'hf-mirror' },
  { label: 'ModelScope', value: 'modelscope' },
]
const hubResults = ref<HubSearchResult[]>([])
const hubErrors = ref<string[]>([])
const hubSearched = ref(false)
const hubSearching = ref(false)
const visibleCount = ref(MARKET_FIRST)

// 逐步显示：滚动到底部时每次多显示 MARKET_STEP 条
const displayRows = computed(() => marketRows.value.slice(0, visibleCount.value))
const hasMore = computed(() => visibleCount.value < marketRows.value.length)

function loadMore() {
  if (hasMore.value && !hubSearching.value) {
    visibleCount.value = Math.min(visibleCount.value + MARKET_STEP, marketRows.value.length)
  }
}

// 无限滚动：底部哨兵进入视口（含 240px 预载）时自动加载下一批。
// visibleCount/hasMore 变化后重新挂观察器，保证列表不足一屏时也能自动补足。
const sentinel = ref<HTMLElement | null>(null)
let observer: IntersectionObserver | null = null

function observeSentinel() {
  observer?.disconnect()
  observer = null
  if (!sentinel.value) return
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) loadMore()
    },
    { rootMargin: '240px 0px' },
  )
  observer.observe(sentinel.value)
}

async function searchHub(keyword?: string) {
  const query = (keyword ?? hubQuery.value).trim()
  hubQuery.value = query
  hubSearching.value = true
  hubResults.value = []
  hubErrors.value = []
  visibleCount.value = MARKET_FIRST
  clearInspection()
  try {
    const response = await store.searchHub(query, hubSource.value, MARKET_FETCH, hubCategory.value)
    hubResults.value = response.results
    hubErrors.value = response.errors
    hubSearched.value = true
  } catch (error) {
    hubSearched.value = true
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    hubSearching.value = false
  }
}

// 分类切换 → 自动重新获取对应分类模型
watch(hubCategory, () => {
  void searchHub('')
})

// 进入页面：加载分类节点配置并自动获取热门模型（实时更新，无需手动搜索）
onMounted(() => {
  void loadCategories()
  void searchHub('')
})

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

// ── 展示映射（统一中文） ──
const PIPELINE_TEXT: Record<string, string> = {
  'text-embedding': '向量嵌入', 'feature-extraction': '特征提取', 'sentence-similarity': '句向量',
  rerank: '语义重排', 'semantic-rerank': '语义重排',
  'text-generation': '对话生成', 'text2text-generation': '文本生成',
  'text-classification': '文本分类', 'token-classification': '词元标注',
  'question-answering': '问答', 'fill-mask': '完形填空', translation: '翻译',
  'text-to-speech': '语音合成', 'automatic-speech-recognition': '语音识别', 'auto-speech-recognition': '语音识别',
  'image-classification': '图像分类', 'image-to-text': '图像描述', 'object-detection': '目标检测',
}
const PURPOSE_TEXT: Record<string, string> = { chat: '对话', embedding: '向量嵌入', reranker: '语义重排' }
const pipelineText = (tag?: string | null) => (tag ? (PIPELINE_TEXT[tag] ?? tag) : '')

// 来源标注：同一行可能来自双源（跨源同 id 已合并）
const SOURCE_LABEL: Record<string, string> = { 'hf-mirror': 'HF 镜像', modelscope: 'ModelScope' }
const sourceLabel = (source: string) => SOURCE_LABEL[source] ?? source
const sourceText = (row: HubSearchResult) => {
  const list = row.sources?.length ? row.sources : [row.source]
  return list.map(sourceLabel).join(' + ')
}

// 行信息（搜索阶段统一：id / 用途 / 来源 / 下载量 / 是否已安装）
const marketRows = computed(() => hubResults.value.map(item => ({
  key: `${item.id}:${(item.sources ?? [item.source]).join(',')}`,
  id: item.id,
  source: item.source,
  purpose: pipelineText(item.pipeline_tag) || '未标注用途',
  downloads: item.downloads,
  installed: isInstalled(item.id),
  item,
})))

// 无限滚动观察器：marketRows 定义后再注册 watch（displayRows/hasMore 依赖 marketRows，
// watch 建立依赖追踪时会读取 hasMore.value → 触发 marketRows 求值，必须在定义后才能访问）
watch([hasMore, visibleCount], () => void nextTick(observeSentinel))

// ── 查看解析（在线模型）：远程仓库详细解析，按首选源走同一契约 ──
const inspectingId = ref('')
const inspectingSource = ref('')
const hubInspection = ref<RemoteInspection | null>(null)
const inspecting = ref(false)
const hubDownloading = ref(false)

function clearInspection() {
  inspectingId.value = ''
  inspectingSource.value = ''
  hubInspection.value = null
}

const isExpanded = (row: { id: string; source: string }) =>
  !!hubInspection.value && row.id === inspectingId.value && row.source === inspectingSource.value

async function toggleInspect(row: { id: string; source: string; item: HubSearchResult }) {
  if (isExpanded(row)) {
    clearInspection()
    return
  }
  // 首选源：带不可变 hash 的源（跨源合并时后端已选好）
  let revision = (row.source === 'hf-mirror' ? row.item.sha : row.item.revision) || ''
  // ModelScope 搜索阶段跳过了 revision 解析（提速），检视时按需解析单个仓库
  if (!/^[0-9a-fA-F]{7,64}$/.test(revision) && row.source === 'modelscope') {
    clearInspection()
    inspecting.value = true
    try {
      const resolved = await store.resolveHubRevision(row.id, row.source)
      revision = resolved.revision || ''
      if (!/^[0-9a-fA-F]{7,64}$/.test(revision)) {
        message.warning('该仓库没有可用的不可变 commit hash，请换一个仓库')
        return
      }
      row.item.revision = revision
    } catch (error) {
      message.error(error instanceof Error ? error.message : String(error))
      return
    } finally {
      inspecting.value = false
    }
  } else if (!/^[0-9a-fA-F]{7,64}$/.test(revision)) {
    message.warning('该仓库没有可用的不可变 commit hash，请换一个仓库')
    return
  }
  clearInspection()
  inspecting.value = true
  try {
    hubInspection.value = await store.inspectRemote(row.id, revision, row.source)
    inspectingId.value = row.id
    inspectingSource.value = row.source
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    inspecting.value = false
  }
}

const hubBytes = computed(() => (hubInspection.value ? hubInspection.value.files.reduce((sum, file) => sum + file.size, 0) : 0))
const storageBytes = computed(() => (hubInspection.value ? hubBytes.value : undefined))

// ── 下载（统一流程：检视 → 选择目录 → 下载，按来源分发） ──
const destination = ref('')
const showStorage = ref(false)

async function downloadHub() {
  if (!hubInspection.value || !destination.value) return
  hubDownloading.value = true
  try {
    await store.downloadHubRepository(
      hubInspection.value.repository,
      hubInspection.value.revision,
      destination.value,
      store.createRequestId(),
      inspectingSource.value,
    )
    message.success('下载任务已创建，可在「下载任务」中查看')
    destination.value = ''
    clearInspection()
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    hubDownloading.value = false
  }
}

function selectStorage(path: string) {
  destination.value = path
  showStorage.value = false
  void downloadHub()
}

// ── 下载入口：已保存默认目录可写则直接复用（每次下载前重新校验），否则打开目录选择 ──
let chooseGeneration = 0

async function choose() {
  const generation = ++chooseGeneration
  if (!hubInspection.value) return
  if (store.defaultStorage) {
    try {
      const validation = await store.validateStorage(store.defaultStorage, hubBytes.value)
      if (generation !== chooseGeneration) return
      if (validation.writable && !validation.error) {
        destination.value = validation.path
        void downloadHub()
        return
      }
      message.warning(validation.error || validation.reason || '默认目录不可用，请重新选择')
    } catch (error) {
      if (generation !== chooseGeneration) return
      message.warning(error instanceof Error ? error.message : String(error))
    }
  }
  if (generation !== chooseGeneration) return
  showStorage.value = true
}

// ── 展示工具 ──
function formatCount(value: number) {
  if (value >= 10000) return `${(value / 10000).toFixed(1)}w`
  return String(value)
}
function formatFileSize(size: number) {
  if (size >= 1024 * 1024 * 1024) return `${(size / 1024 / 1024 / 1024).toFixed(2)} GB`
  return `${(size / 1024 / 1024).toFixed(1)} MB`
}
const STATE_TEXT: Record<string, string> = { ready: '可运行', error: '检视失败', requires_configuration: '需要配置' }
const stateText = (state: string) => STATE_TEXT[state] ?? state
const stateType = (state: string) => (state === 'ready' ? 'success' : state === 'requires_configuration' ? 'warning' : 'error')
// 缺失文件中文映射（后端缺项文案是英文契约，前端统一转为中文）
const MISSING_TEXT: Record<string, string> = {
  'onnx model file (.onnx / .onnx_data / .onnx_weights)': 'ONNX 模型文件（本机需 ONNX 布局才能运行）',
  'recognized config file (genai_config.json / config.json / sentence_bert_config.json)': '可识别的配置文件（genai_config.json / config.json / sentence_bert_config.json）',
}
const missingText = (item: string) => MISSING_TEXT[item] ?? item
</script>

<template>
  <div class="market-wrap">
    <div class="market-head">
      <h3>模型获取</h3>
      <span class="market-sub">统一获取 HuggingFace 镜像与 ModelScope 的功能节点 · 查看解析后下载到本地 · 已下载好的在「已安装」中管理</span>
    </div>

    <!-- 分类 Tab：切换即自动获取对应分类 -->
    <n-tabs v-model:value="hubCategory" type="segment" size="small">
      <n-tab-pane v-for="cat in CATEGORIES" :key="cat.key" :name="cat.key" :tab="cat.label" />
    </n-tabs>
    <div class="category-desc">{{ CATEGORIES.find(c => c.key === hubCategory)?.desc }}</div>

    <!-- 搜索行：仅一个搜索框 -->
    <div class="search-row">
      <n-input v-model:value="hubQuery" clearable placeholder="搜索要获取的小模型，如 bge、rerank、whisper…（留空获取热门）" @keyup.enter="searchHub()" />
      <n-select v-model:value="hubSource" :options="hubSourceOptions" class="source-select" @update:value="searchHub()" />
      <n-button type="primary" :loading="hubSearching" @click="searchHub()">获取</n-button>
    </div>

    <!-- 单源失败提示：不阻断另一源，明确告知 -->
    <n-alert v-if="hubErrors.length" type="warning" :show-icon="false" class="source-error">
      <template #default>
        <div v-for="error in hubErrors" :key="error">{{ error }}，仅显示可用的来源结果</div>
      </template>
    </n-alert>

    <!-- 统一模型列表（HF 镜像 / ModelScope 合并展示，跨源同 id 合并为一行） -->
    <div v-if="hubSearching" class="load-line"><n-spin size="small" />正在获取模型…</div>
    <n-empty v-else-if="hubSearched && !marketRows.length" description="这个分类下没有匹配的仓库，换个关键词试试" />
    <div v-else-if="marketRows.length" class="model-list">
      <div class="list-count">已显示 {{ displayRows.length }} / 共 {{ marketRows.length }} 个模型</div>

      <article
        v-for="row in displayRows"
        :key="row.key"
        class="glass-panel model-row"
        :class="{ expanded: isExpanded(row) }"
      >
        <!-- 行主体：两源结构完全一致 -->
        <div class="row-main">
          <div class="row-id">
            <strong :title="row.id">{{ row.id }}</strong>
            <span>{{ row.purpose }}</span>
          </div>

          <div class="row-meta">
            <n-tag v-if="row.installed" size="small" round type="success">已安装</n-tag>
            <template v-else>
              <n-tag v-for="source in (row.item.sources?.length ? row.item.sources : [row.item.source])" :key="source" size="small" round type="info">{{ sourceLabel(source) }}</n-tag>
            </template>
            <span>下载 {{ formatCount(row.downloads) }}</span>
          </div>

          <div class="row-actions">
            <n-button v-if="row.installed" size="small" disabled>已安装</n-button>
            <n-button
              v-else
              size="small"
              :loading="inspecting && inspectingId === row.id && inspectingSource === row.item.source"
              @click="toggleInspect(row)"
            >
              {{ isExpanded(row) ? '收起解析' : '查看解析' }}
            </n-button>
          </div>
        </div>

        <!-- 详细解析（两源统一展开） -->
        <div v-if="isExpanded(row) && hubInspection" class="row-inspect">
          <div class="inspect-grid">
            <span>来源 <b>{{ sourceText(row.item) }}</b></span>
            <span>检视源 <b>{{ sourceLabel(row.item.source) }}</b></span>
            <span>版本 <b>{{ hubInspection.revision }}</b></span>
            <span>文件 <b>{{ hubInspection.files.length }} 个</b></span>
            <span>总大小 <b>{{ (hubBytes / 1024 / 1024).toFixed(1) }} MB</b></span>
            <span>用途 <b>{{ hubInspection.purpose ? (PURPOSE_TEXT[hubInspection.purpose] ?? hubInspection.purpose) : '未识别' }}</b></span>
            <span>检视 <n-tag size="small" :type="stateType(hubInspection.state)">{{ stateText(hubInspection.state) }}</n-tag></span>
            <span>本机 <n-tag size="small" :type="hubInspection.runnable ? 'success' : 'warning'">{{ hubInspection.runnable ? '可运行' : '不可运行' }}</n-tag></span>
          </div>
          <div v-if="hubInspection.missing.length" class="inspect-missing">
            <span>该仓库不是本机可运行的 ONNX 布局，无法下载部署：</span>
            {{ hubInspection.missing.map(missingText).join('；') }}
          </div>

          <div class="inspect-files">
            <div class="files-title">仓库文件解析</div>
            <div v-for="file in hubInspection.files.slice(0, 30)" :key="file.path" class="file-row">
              <span class="file-path" :title="file.path">{{ file.path }}</span>
              <span class="file-size">{{ formatFileSize(file.size) }}</span>
            </div>
            <div v-if="hubInspection.files.length > 30" class="files-more">…共 {{ hubInspection.files.length }} 个文件，安装时按需校验</div>
          </div>

          <div v-if="!hubInspection.missing.length" class="download-row">
            <span v-if="destination" class="dest">存储：{{ destination }}</span>
            <n-button size="small" type="primary" :loading="hubDownloading" @click="choose">选择目录并下载</n-button>
          </div>
        </div>
      </article>

      <div v-if="hasMore" ref="sentinel" class="load-sentinel">
        <n-spin size="small" /> 下拉加载更多（已显示 {{ displayRows.length }} / {{ marketRows.length }}）
      </div>
    </div>

    <StoragePickerDialog :show="showStorage" :required-bytes="storageBytes" @select="selectStorage" @cancel="showStorage = false" />
  </div>
</template>

<style scoped>
.market-wrap { display: flex; flex-direction: column; gap: 16px; }
.market-head { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.market-head h3 { margin: 0; font-size: 15px; font-weight: 600; }
.market-sub { color: var(--moon-dim); font-size: 12px; }
.category-desc { color: var(--moon-dim); font-size: 12px; margin-top: -10px; }

.search-row { display: grid; grid-template-columns: minmax(180px, 1fr) 170px auto; gap: 10px; align-items: center; }
.source-select { min-width: 0; }
.source-error { margin-top: -4px; }

.load-line { display: flex; align-items: center; justify-content: center; gap: 8px; padding: 28px 0; color: var(--moon-dim); font-size: 13px; }
.list-count { color: var(--moon-dim); font-size: 12px; }
.load-sentinel { display: flex; align-items: center; justify-content: center; gap: 8px; padding: 14px 0; color: var(--moon-dim); font-size: 12px; }

.model-list { display: flex; flex-direction: column; gap: 10px; }
.model-row { padding: 12px 16px; border-radius: 12px; transition: border-color .2s; }
.model-row.expanded { border-color: var(--moon-leaf); }

.row-main { display: flex; align-items: center; gap: 16px; }
.row-id { flex: 1; min-width: 0; }
.row-id strong, .row-id span { display: block; }
.row-id span { margin-top: 3px; color: var(--moon-dim); font-size: 12px; }
.row-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; color: var(--moon-dim); font-size: 12px; white-space: nowrap; }
.row-actions { flex-shrink: 0; display: flex; gap: 8px; }

/* 详细解析 */
.row-inspect { border-top: 1px solid var(--moon-line); margin-top: 12px; padding-top: 12px; display: flex; flex-direction: column; gap: 10px; }
.inspect-grid { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; color: var(--moon-dim); font-size: 12px; }
.inspect-grid b { color: inherit; font-weight: 600; }
.inspect-missing { color: #d03050; font-size: 12px; }
.inspect-files { display: flex; flex-direction: column; gap: 4px; max-height: 220px; overflow: auto; border: 1px solid var(--moon-line); border-radius: 8px; padding: 8px 10px; }
.files-title { color: var(--moon-dim); font-size: 12px; font-weight: 600; margin-bottom: 2px; }
.file-row { display: flex; justify-content: space-between; gap: 12px; font-size: 12px; color: var(--moon-dim); }
.file-path { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.file-size { flex-shrink: 0; }
.files-more { color: var(--moon-dim); font-size: 12px; }
.download-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.dest { color: var(--moon-dim); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

@media (max-width: 640px) {
  .search-row { grid-template-columns: 1fr; }
  .row-main { flex-wrap: wrap; gap: 10px; }
}
</style>
