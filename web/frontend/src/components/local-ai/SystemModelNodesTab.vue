<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { NButton, NTag, NEmpty, useMessage } from 'naive-ui'
import { useLocalAiStore, type ModelNode } from '../../stores/localAi'
import StoragePickerDialog from './StoragePickerDialog.vue'

const store = useLocalAiStore()
const message = useMessage()
const nodes = ref<ModelNode[]>([])
const loading = ref(false)
const saving = ref('')
/** 展开本地模型候选的节点（点击「本地模型」只展开，不切换） */
const localOpen = ref<Record<string, boolean>>({})
/** 正在下载的候选模型 id（用于显示 loading） */
const downloadingModel = ref('')
/** 下载目录选择对话框 */
const showStorage = ref(false)
const pendingModelId = ref('')

// 按用途分组展示（每个分组是一个独立的功能区）
const GROUPS: Array<{ key: string; title: string; sub: string; icon: string; kinds: ModelNode['kind'][] }> = [
  { key: 'encoder', title: '向量编码', icon: '◈', sub: 'RAG 检索链路的本地小模型，负责把文本变成向量、精排结果相关性', kinds: ['encoder'] },
  { key: 'generative', title: '生成改写', icon: '✎', sub: '需要生成文本的功能节点，可改用自己部署的对话小模型，也可走 API 默认', kinds: ['generative'] },
  { key: 'other', title: '语音识别', icon: '♪', sub: '把语音消息转成文字', kinds: ['other'] },
]
const grouped = computed(() => GROUPS.map(group => ({
  ...group,
  items: nodes.value.filter(node => group.kinds.includes(node.kind)),
})).filter(group => group.items.length))

const purposeLabel = (purpose: string) => {
  const map: Record<string, string> = { embedding: '向量嵌入', embed: '向量嵌入', rerank: '语义重排', reranker: '语义重排', chat: '对话', 'text-generation': '对话', llm: '对话', asr: '语音识别', stt: '语音识别', whisper: '语音识别', other: '其他' }
  return map[purpose] ?? purpose
}

function effectiveLabel(node: ModelNode) {
  if (node.backend === 'local') {
    if (node.local_model) return `本地 · ${node.local_model}`
    return node.local_available ? '本地 · 未选模型' : '本地（未就绪）'
  }
  return node.api_configured ? 'API（默认）' : 'API（未配置 Key）'
}
const stateType = (node: ModelNode) => {
  if (node.backend === 'local') return node.local_available ? ('success' as const) : ('warning' as const)
  return node.api_configured ? ('info' as const) : ('warning' as const)
}

async function load() {
  loading.value = true
  try {
    nodes.value = await store.fetchModelNodes()
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    loading.value = false
  }
}

function toggleLocal(node: ModelNode) {
  // 只展开/收起候选，不切换后端
  localOpen.value[node.id] = !localOpen.value[node.id]
}

async function switchApi(node: ModelNode) {
  if (saving.value) return
  saving.value = node.id
  try {
    await store.setModelNodeBackend(node.id, 'api')
    node.backend = 'api'
    localOpen.value[node.id] = false
    message.success(`「${node.name}」已切换到 API（本地推理已停止常驻）`)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    saving.value = ''
  }
}

async function runLocalModel(node: ModelNode, modelId: string) {
  if (saving.value) return
  saving.value = node.id
  try {
    await store.setModelNodeBackend(node.id, 'local', modelId)
    node.backend = 'local'
    node.local_model = modelId
    localOpen.value[node.id] = false
    message.success(`「${node.name}」已运行本地模型 ${modelId}`)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    saving.value = ''
  }
}

// ── 目录候选一键下载（未下载灰 → 下载 → 已安装白） ──
function createRequestId() {
  return store.createRequestId()
}

async function doDownload(modelId: string, destination: string) {
  downloadingModel.value = modelId
  try {
    await store.download({ model_id: modelId, destination, request_id: createRequestId() })
    pendingModelId.value = ''
    message.success(`已开始下载 ${modelId}，完成后会自动进入候选列表`)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    downloadingModel.value = ''
  }
}

async function downloadLocalModel(modelId: string) {
  if (downloadingModel.value) return
  pendingModelId.value = modelId
  if (store.defaultStorage) {
    try {
      const validation = await store.validateStorage(store.defaultStorage, 0)
      if (validation.writable && !validation.error) {
        await doDownload(modelId, validation.path)
        return
      }
      message.warning(validation.error || validation.reason || '默认目录不可用，请重新选择')
    } catch {
      message.warning('默认目录不可用，请重新选择')
    }
  }
  showStorage.value = true
}

function selectStorage(path: string) {
  showStorage.value = false
  if (pendingModelId.value) void doDownload(pendingModelId.value, path)
}

// 下载完成 → 自动刷新节点候选（未下载灰 → 已安装白）
watch(
  () => store.downloads.filter(d => d.state === 'completed').length,
  (count, prev) => {
    if (count > (prev ?? 0)) void load()
  },
)

onMounted(load)
</script>

<template>
  <div class="model-nodes">
    <div class="nodes-header">
      <div>
        <h3>功能节点</h3>
        <p>系统内每个 AI 功能位是一个节点，可单独决定走 API（硅基流动，默认）还是用你自己部署的本地模型。选「本地模型」会展开候选，点具体模型才会切换并常驻运行。</p>
      </div>
      <n-button :loading="loading" @click="load">刷新</n-button>
    </div>

    <template v-if="grouped.length">
      <section v-for="group in grouped" :key="group.key" class="node-group">
        <div class="group-head">
          <span class="group-icon">{{ group.icon }}</span>
          <div>
            <h4>{{ group.title }}</h4>
            <span class="group-sub">{{ group.sub }}</span>
          </div>
        </div>

        <div class="node-card" v-for="node in group.items" :key="node.id">
          <div class="node-head">
            <div class="node-title">
              <span class="node-name">{{ node.name }}</span>
              <n-tag size="small" :type="stateType(node)" :bordered="false">{{ effectiveLabel(node) }}</n-tag>
            </div>
            <div class="node-desc">{{ node.desc }}</div>
          </div>

          <div class="node-body">
            <!-- 后端切换：本地模型（展开候选）/ API -->
            <div class="backend-row">
              <button
                class="backend-btn"
                :class="{ active: node.backend === 'local', open: localOpen[node.id] }"
                :disabled="saving !== '' && saving !== node.id"
                @click="toggleLocal(node)"
              >
                本地模型
              </button>
              <button
                class="backend-btn api"
                :class="{ active: node.backend === 'api' }"
                :disabled="saving !== '' && saving !== node.id"
                @click="switchApi(node)"
              >
                API（硅基流动）
              </button>
            </div>

            <!-- 展开候选：已安装（白，可运行）+ 目录未下载（灰，一键下载） -->
            <div v-if="localOpen[node.id]" class="local-panel">
              <template v-if="node.local_models.length">
                <p class="lm-title">选择模型：已安装可直接运行，灰色未下载可一键下载：</p>
                <div class="local-models">
                  <button
                    v-for="model in node.local_models.filter(m => m.installed)"
                    :key="model.id"
                    class="lm-item"
                    :class="{ active: node.backend === 'local' && node.local_model === model.id }"
                    :disabled="saving === node.id"
                    @click="runLocalModel(node, model.id)"
                  >
                    <span class="lm-name">{{ model.id }}</span>
                    <span class="lm-tags">
                      <n-tag size="tiny" :bordered="false" type="info">{{ purposeLabel(model.purpose) }}</n-tag>
                      <n-tag size="tiny" :bordered="false" type="success">已安装</n-tag>
                      <span v-if="model.ownership === 'bundled'" class="lm-builtin">内置</span>
                    </span>
                  </button>
                  <button
                    v-for="model in node.local_models.filter(m => !m.installed)"
                    :key="model.id"
                    class="lm-item lm-item-pending"
                    :disabled="downloadingModel !== ''"
                    @click="downloadLocalModel(model.id)"
                  >
                    <span class="lm-name">{{ model.id }}</span>
                    <span class="lm-tags">
                      <n-tag size="tiny" :bordered="false" type="info">{{ purposeLabel(model.purpose) }}</n-tag>
                      <n-tag size="tiny" :bordered="false" type="warning">暂未下载</n-tag>
                      <n-tag size="tiny" :bordered="false" type="info">{{ downloadingModel === model.id ? '下载中…' : '点击下载' }}</n-tag>
                    </span>
                  </button>
                </div>
              </template>
              <p v-else class="lm-empty">没有模型候选。</p>
            </div>
          </div>
        </div>
      </section>
    </template>
    <n-empty v-else description="功能节点加载失败或为空" />

    <StoragePickerDialog :show="showStorage" :required-bytes="0" @select="selectStorage" @cancel="showStorage = false" />
  </div>
</template>

<style scoped>
.model-nodes { display: flex; flex-direction: column; gap: 26px; }
.nodes-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.nodes-header h3 { margin: 0; font-family: 'Noto Serif SC', serif; }
.nodes-header p { margin: 4px 0 0; color: var(--moon-dim); font-size: 12px; }

.node-group { display: flex; flex-direction: column; gap: 12px; }
.group-head { display: flex; align-items: center; gap: 12px; }
.group-icon { width: 34px; height: 34px; display: inline-flex; align-items: center; justify-content: center; border-radius: 10px; background: linear-gradient(135deg, rgba(143, 229, 96, 0.16), rgba(70, 180, 120, 0.10)); color: #8fe560; font-size: 16px; flex-shrink: 0; }
.group-head h4 { margin: 0; font-size: 14px; font-weight: 700; }
.group-sub { display: block; margin-top: 2px; color: var(--moon-dim); font-size: 11.5px; }

.node-card { display: flex; flex-direction: column; gap: 12px; padding: 16px 18px; border: 1px solid rgba(128, 128, 128, 0.16); border-radius: 14px; background: rgba(255, 255, 255, 0.03); transition: border-color 0.2s ease, background 0.2s ease; }
.node-card:hover { border-color: rgba(143, 229, 96, 0.32); background: rgba(255, 255, 255, 0.045); }
.node-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.node-name { font-weight: 700; font-size: 15px; }
.node-desc { margin-top: 3px; color: var(--moon-dim); font-size: 12px; }

.node-body { display: flex; flex-direction: column; gap: 12px; }
.backend-row { display: flex; gap: 10px; flex-wrap: wrap; }
.backend-btn { padding: 7px 20px; border-radius: 9px; border: 1px solid rgba(143, 229, 96, 0.35); background: transparent; color: rgba(255, 255, 255, 0.82); font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.18s ease; font-family: inherit; }
.backend-btn:hover { border-color: #8fe560; color: #8fe560; }
.backend-btn.active { background: rgba(143, 229, 96, 0.16); border-color: #8fe560; color: #8fe560; }
.backend-btn.api { border-color: rgba(112, 192, 232, 0.35); }
.backend-btn.api:hover { border-color: #70c0e8; color: #70c0e8; }
.backend-btn.api.active { background: rgba(112, 192, 232, 0.14); border-color: #70c0e8; color: #70c0e8; }
.backend-btn:disabled { opacity: 0.5; cursor: not-allowed; }

.local-panel { padding: 12px 14px; border: 1px dashed rgba(143, 229, 96, 0.35); border-radius: 10px; background: rgba(143, 229, 96, 0.04); }
.lm-title { margin: 0 0 10px; color: var(--moon-dim); font-size: 12px; }
.local-models { display: flex; flex-wrap: wrap; gap: 8px; }
.lm-item { display: inline-flex; flex-direction: column; align-items: flex-start; gap: 4px; min-width: 150px; padding: 8px 12px; border-radius: 10px; border: 1px solid rgba(70, 180, 120, 0.35); background: rgba(255, 255, 255, 0.92); color: #1f2d3d; cursor: pointer; text-align: left; transition: all 0.18s ease; font-family: inherit; }
.lm-item:hover { border-color: rgba(143, 229, 96, 0.8); transform: translateY(-1px); }
.lm-item.active { border-color: #8fe560; background: rgba(143, 229, 96, 0.2); box-shadow: 0 0 0 1px rgba(143, 229, 96, 0.4); }
.lm-item:disabled { opacity: 0.55; cursor: wait; }
.lm-name { font-size: 12.5px; font-weight: 700; word-break: break-all; }
.lm-tags { display: inline-flex; align-items: center; gap: 4px; flex-wrap: wrap; }
.lm-builtin { font-size: 10.5px; color: #8fe560; font-weight: 700; }
.lm-empty { margin: 0; color: var(--moon-dim); font-size: 12.5px; }
.lm-item-pending { opacity: 0.62; background: rgba(255, 255, 255, 0.55); border-style: dashed; }
.lm-item-pending:hover { opacity: 0.9; }
</style>
