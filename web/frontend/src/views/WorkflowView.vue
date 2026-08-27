<script setup lang="ts">
import { ref, nextTick, onMounted, onUnmounted, watch } from 'vue'
import SumeruIcon from '../components/fx/SumeruIcon.vue'
import { useRouter } from 'vue-router'
import {
  NButton, NSwitch, NInput, NSelect, NPopconfirm, NTag, NSpin, NEmpty, NModal, NTooltip, useMessage,
} from 'naive-ui'
import { api, type Workflow, type WorkflowNode, type WorkflowSummary } from '../api'
import { useChatStore } from '../stores/chat'
import { flipCapture } from '../utils/gsapMotion'
import { t, tf } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'
import ViewTitleIcon from '../components/fx/ViewTitleIcon.vue'

const message = useMessage()
const router = useRouter()
const chatStore = useChatStore()

// ── 工作流列表 ──
const workflows = ref<WorkflowSummary[]>([])
const loading = ref(false)
const editing = ref<Workflow | null>(null)
const isCreate = ref(false)
const saving = ref(false)
const testing = ref(false)

// ── 可选资源（从已配置的获取） ──
const resourceOptions = ref<{
  tools: Array<{ label: string; value: string }>
  skills: Array<{ label: string; value: string }>
  mcpTools: Array<{ label: string; value: string }>
  agents: Array<{ label: string; value: string }>
  models: Array<{ label: string; value: string }>
}>({ tools: [], skills: [], mcpTools: [], agents: [], models: [] })

const NODE_META: Record<string, { icon: string; label: string; color: string }> = {
  tool:  { icon: 'tools', label: '工具',     color: '#7fd650' },
  skill: { icon: 'note', label: '技能',     color: '#e8d5a3' },
  mcp:   { icon: 'mcp', label: 'MCP',      color: '#5fb3d9' },
  agent: { icon: 'agents', label: '子智能体',  color: '#d97fd9' },
  model: { icon: 'models', label: '模型',     color: '#5fd9c4' },
  step:  { icon: 'note', label: '步骤说明',  color: '#d96a5f' },
}

onMounted(() => {
  load()
  loadResources()
})

async function load() {
  loading.value = true
  try {
    workflows.value = await api.listWorkflows()
    await loadV2Status(workflows.value.map(w => w.id))
  } catch (e: any) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

async function loadResources() {
  try {
    const [tools, skills, mcpServers, agents, discoverResult] = await Promise.all([
      api.getTools().catch(() => []),
      api.getSkills().catch(() => []),
      api.getMcpServers().catch(() => []),
      api.getAgents().catch(() => []),
      api.discoverModels().catch(() => []),
    ])

    const toolOpts = tools
      .filter((t: any) => t.enabled)
      .map((t: any) => ({ label: `${t.name} — ${t.description || ''}`, value: t.name }))

    const skillOpts = skills.map((s: any) => ({ label: s.name, value: s.name }))

    // 工具和技能合并展示，用户不需要区分
    resourceOptions.value.tools = toolOpts
    resourceOptions.value.skills = [...toolOpts, ...skillOpts]

    const mcpOpts: Array<{ label: string; value: string }> = []
    for (const srv of mcpServers) {
      for (const tn of srv.tool_names || []) {
        mcpOpts.push({ label: `${srv.name} / ${tn}`, value: `mcp_${srv.name}_${tn}` })
      }
    }
    resourceOptions.value.mcpTools = mcpOpts

    resourceOptions.value.agents = agents
      .filter((a: any) => !a.is_main && a.enabled !== false)
      .map((a: any) => ({ label: `${a.display_name || a.name}`, value: a.name }))

    // 模型：从 discover API 获取具体模型 ID（而非供应商列表）
    const modelOpts: Array<{ label: string; value: string }> = []
    for (const entry of discoverResult) {
      const providerLabel = entry.label || entry.provider
      // MiMo 特殊处理：直接在 entry.models 中
      const models = entry.models || []
      for (const m of models) {
        const modelId = m.id || m.model_id || ''
        if (!modelId) continue
        const displayName = m.display_name || m.name || modelId
        modelOpts.push({ label: `${providerLabel} / ${displayName}`, value: `${entry.provider}/${modelId}` })
      }
    }
    resourceOptions.value.models = modelOpts
  } catch {
    // 静默失败，下拉框为空即可
  }
}

// ── 获取某类型节点的可选列表 ──
function getOptions(type: string) {
  switch (type) {
    case 'tool':  return resourceOptions.value.tools
    case 'skill': return resourceOptions.value.skills
    case 'mcp':   return resourceOptions.value.mcpTools
    case 'agent': return resourceOptions.value.agents
    case 'model': return resourceOptions.value.models
    default: return []
  }
}

// ── 工作流操作 ──
// M3 灰度：记录每个工作流的 v2 可用性（全局开关或缺省白名单），
// 未开放时隐藏「启动」并给占位文案；失败视为可用（点击时报错兜底）。
const v2Status = ref<Record<string, { enabled: boolean }>>({})

function v2Enabled(wfId: string): boolean {
  return v2Status.value[wfId]?.enabled ?? true
}

async function loadV2Status(wfIds: string[]) {
  const entries = await Promise.all(wfIds.map(async id => {
    const s = await api.getWorkflowV2Status(id).catch(() => null)
    return [id, s] as const
  }))
  v2Status.value = Object.fromEntries(entries.filter(([, s]) => s) as any)
}

function newWorkflow() {
  editing.value = {
    id: '', name: '', description: '', version: '1.0.0',
    enabled: true, nodes: [], edges: [], trigger: 'manual',
  }
  isCreate.value = true
}

async function editWorkflow(wf: WorkflowSummary) {
  try {
    editing.value = await api.getWorkflow(wf.id)
    isCreate.value = false
  } catch (e: any) { message.error(e.message) }
}

async function deleteWorkflow(wf: WorkflowSummary) {
  try {
    await api.deleteWorkflow(wf.id)
    message.success(t('workflowView.deleted') + ' ' + wf.name)
    await load()
  } catch (e: any) { message.error(e.message) }
}

async function toggleEnabled(wf: WorkflowSummary, val: boolean) {
  try {
    const full = await api.getWorkflow(wf.id)
    full.enabled = val
    await api.updateWorkflow(wf.id, full)
    wf.enabled = val
  } catch (e: any) { message.error(e.message) }
}

function cancelEdit() { editing.value = null }

async function save() {
  if (!editing.value) return
  if (!editing.value.name.trim()) { message.error(t('workflowView.nameRequired')); return }
  saving.value = true
  try {
    const wf = editing.value
    // 线性序列：按节点顺序生成 edges
    wf.edges = []
    for (let i = 0; i < wf.nodes.length - 1; i++) {
      wf.edges.push([wf.nodes[i].id, wf.nodes[i + 1].id])
    }
    if (isCreate.value) {
      editing.value = await api.createWorkflow(wf)
      isCreate.value = false
    } else {
      editing.value = await api.updateWorkflow(wf.id, wf)
    }
    message.success(t('workflowView.saved'))
    await load()
  } catch (e: any) { message.error(e.message) }
  finally { saving.value = false }
}

async function testWorkflow() {
  if (!editing.value || isCreate.value) { message.warning(t('workflowView.saveFirst')); return }
  if (chatStore.isProcessing) { message.warning(t('workflowView.chatBusy')); return }
  testing.value = true
  try {
    const result = await api.previewWorkflow(editing.value.id)
    const sendResult = chatStore.sendMessage({
      text: result.prompt || JSON.stringify(result),
      search: false,
      think: false,
      attachments: [],
    })
    if (!sendResult.ok) {
      message.warning(t('workflowView.chatSendFailed'))
      return
    }
    router.push('/')
    message.success(t('workflowView.sentToChat'))
  } catch (e: any) { message.error(e.message) }
  finally { testing.value = false }
}

// ── 节点操作 ──
function addNode(type: WorkflowNode['type']) {
  if (!editing.value) return
  const meta = NODE_META[type]
  const node: WorkflowNode = {
    id: `n${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    type, label: t('workflowView.nodeType.' + type),
  }
  if (type !== 'step') node.ref = ''
  editing.value.nodes.push(node)
}

function removeNode(id: string) {
  const wf = editing.value
  if (!wf) return
  replayNodesLayout(async () => {
    wf.nodes = wf.nodes.filter(n => n.id !== id)
  })
}

function moveNode(idx: number, dir: -1 | 1) {
  const wf = editing.value
  if (!wf) return
  const nodes = wf.nodes
  const newIdx = idx + dir
  if (newIdx < 0 || newIdx >= nodes.length) return
  replayNodesLayout(() => {
    ;[nodes[idx], nodes[newIdx]] = [nodes[newIdx], nodes[idx]]
  })
}

// 节点增删/排序的 FLIP 平滑重排：捕获旧位置 → 数据变更 → nextTick 后补间到新位置。
// 护栏（low-gpu / reduced-motion）或 Flip 加载失败时无动画，列表行为不变
const nodesSectionEl = ref<HTMLElement | null>(null)

function replayNodesLayout(mutate: () => void) {
  void (async () => {
    const finish = await flipCapture(nodesSectionEl.value)
    mutate()
    await nextTick()
    finish()
  })()
}

// 节点选择资源时，自动更新 label
function onNodeSelect(node: WorkflowNode, value: string) {
  node.ref = value
  const opts = getOptions(node.type)
  const found = opts.find(o => o.value === value)
  if (found) node.label = found.label.split(' — ')[0].split(' / ').pop() || found.label
}

const showRunsModal = ref(false)
const runsWfId = ref('')
const runsWfName = ref('')
const runsList = ref<any[]>([])
const runsLoading = ref(false)

const showRevisionsModal = ref(false)
const revisionsWfId = ref('')
const revisionsWfName = ref('')
const revisionsList = ref<any[]>([])
const revisionsLoading = ref(false)

const publishing = ref('')
const starting = ref('')

async function startWorkflow(wf: WorkflowSummary) {
  starting.value = wf.id
  try {
    await api.runWorkflow(wf.id)
    message.success('已启动运行')
    await openRuns(wf.id, wf.name)
  } catch (e: any) { message.error(e.message) } finally {
    starting.value = ''
  }
}

async function openRuns(wfId: string, wfName: string) {
  runsWfId.value = wfId
  runsWfName.value = wfName
  runsLoading.value = true
  showRunsModal.value = true
  try {
    runsList.value = await api.listWorkflowRuns(wfId)
    // 打开即预拉待审审批单（waiting_input 的 run）
    for (const run of runsList.value) {
      if (run.status === 'waiting_input') await loadReviews(run.run_id)
    }
    startRunsPollingIfActive()
  } catch (e: any) { message.error(e.message) } finally {
    runsLoading.value = false
  }
}

// 运行弹窗自动轮询：存在非终态 run 时每 2.5s 刷新，全部结束后停止
let runsPollTimer: ReturnType<typeof setInterval> | null = null

function hasQueuedRuns(): boolean {
  return runsList.value.some(r =>
    ['queued', 'running', 'waiting_input', 'paused', 'cancelling'].includes(r.status))
}

function stopRunsPolling() {
  if (runsPollTimer) { clearInterval(runsPollTimer); runsPollTimer = null }
}

function startRunsPollingIfActive() {
  stopRunsPolling()
  if (!hasQueuedRuns() || !runsWfId.value) return
  runsPollTimer = setInterval(async () => {
    try {
      runsList.value = await api.listWorkflowRuns(runsWfId.value)
      // waiting_input 的 run 同步拉审批单（新进入等待的 run 补卡）
      for (const run of runsList.value) {
        if (run.status === 'waiting_input' && !reviewsMap.value[run.run_id]) {
          await loadReviews(run.run_id)
        }
      }
      if (!hasQueuedRuns()) stopRunsPolling()
    } catch { stopRunsPolling() }
  }, 2500)
}

watch(() => showRunsModal.value, v => { if (!v) stopRunsPolling() })

// 组件卸载兜底停表：弹窗开着直接切路由会把 2.5s 轮询带去坟场（内存泄漏）
onUnmounted(stopRunsPolling)

async function cancelRun(runId: string) {
  try {
    await api.cancelWorkflowRun(runId)
    message.success('运行已取消')
    runsList.value = await api.listWorkflowRuns(runsWfId.value)
  } catch (e: any) { message.error(e.message) }
}

// ── REVIEW 审批（M5）：waiting_input 的 run 拉取审批单，批准/拒绝即续跑或停流 ──
const reviewsMap = ref<Record<string, any[]>>({})
const deciding = ref('')
const noteDetail = ref<Record<string, string>>({})

async function loadReviews(runId: string) {
  try {
    const list = await api.listWorkflowReviews(runId)
    reviewsMap.value = { ...reviewsMap.value, [runId]: list }
  } catch (e: any) { message.error(e.message) }
}

async function decideReview(run: any, review: any, decision: 'approve' | 'reject') {
  deciding.value = review.review_id
  try {
    await api.decideWorkflowReview(run.run_id, review.review_id, decision,
                                   noteDetail.value[review.review_id] || undefined)
    message.success(decision === 'approve' ? '已批准，流程继续' : '已拒绝，流程停止')
    if (noteDetail.value[review.review_id]) {
      noteDetail.value = { ...noteDetail.value, [review.review_id]: '' }
    }
    // 决策后刷新：批准→run 续跑、拒绝→run FAILED；审批单随之下架/销单
    runsList.value = await api.listWorkflowRuns(runsWfId.value)
    await loadReviews(run.run_id)
    if (!hasQueuedRuns()) stopRunsPolling()
  } catch (e: any) { message.error(e.message) } finally {
    deciding.value = ''
  }
}

async function openRevisions(wfId: string, wfName: string) {
  revisionsWfId.value = wfId
  revisionsWfName.value = wfName
  revisionsLoading.value = true
  showRevisionsModal.value = true
  try {
    revisionsList.value = await api.listWorkflowRevisions(wfId)
  } catch (e: any) { message.error(e.message) } finally {
    revisionsLoading.value = false
  }
}

async function publishWorkflow(wfId: string) {
  publishing.value = wfId
  try {
    await api.publishWorkflow(wfId)
    message.success('工作流已发布')
    await load()
    if (showRevisionsModal.value && revisionsWfId.value === wfId) {
      await openRevisions(revisionsWfId.value, revisionsWfName.value)
    }
  } catch (e: any) { message.error(e.message) } finally {
    publishing.value = ''
  }
}

const rollingBack = ref('')

async function rollbackRevision(rev: any) {
  rollingBack.value = rev.revision_id
  try {
    await api.rollbackWorkflowRevision(revisionsWfId.value, rev.revision_id, rev.etag)
    message.success('已回滚——运行将使用该版本编排')
    await openRevisions(revisionsWfId.value, revisionsWfName.value)
    await load()
  } catch (e: any) { message.error(e.message) } finally {
    rollingBack.value = ''
  }
}
</script>

<template>
  <div class="workflows-view">
    <div class="view-header">
      <h2 class="view-title view-title-icon"><ViewTitleIcon name="workflow" /> {{ t('workflowView.title').replace(/^🌿\s*/, '') }}</h2>
      <span class="count">{{ t('workflowView.count') }} {{ workflows.length }} {{ t('workflowView.items') }}</span>
    </div>

    <!-- ── 列表模式 ── -->
    <div v-if="!editing" class="list-section">
      <div class="list-toolbar">
        <n-button type="primary" @click="newWorkflow">{{ t('workflowView.create') }}</n-button>
      </div>
      <n-spin :show="loading">
        <div class="wf-grid">
          <Tilt3D v-for="wf in workflows" :key="wf.id"><div class="wf-card glass-panel glass-panel-hover">
            <div class="wf-card-head">
              <span class="wf-name">{{ wf.name }}</span>
              <n-tag size="tiny" :bordered="false">v{{ wf.version }}</n-tag>
            </div>
            <div class="wf-desc">{{ wf.description || t('workflowView.noDesc') }}</div>
            <div class="wf-card-footer">
              <div class="wf-meta">
                <n-tag size="tiny" :bordered="false">{{ wf.node_count }} {{ t('workflowView.stepsUnit') }}</n-tag>
                <n-switch :value="wf.enabled" size="small"
                          @update:value="(v: boolean) => toggleEnabled(wf, v)" />
              </div>
              <div class="wf-card-actions">
                <n-button size="tiny" type="primary" @click="editWorkflow(wf)">{{ t('workflowView.edit') }}</n-button>
                <n-tooltip trigger="hover">
                  <template #trigger>
                    <n-button size="tiny" :loading="starting === wf.id" :disabled="!v2Enabled(wf.id)" @click="startWorkflow(wf)"><SumeruIcon name="sprout" :size="11" variant="duo" tone="add" interactive /> 启动</n-button>
                  </template>
                  {{ v2Enabled(wf.id) ? '首次运行将自动把当前定义发布为第一个版本；之后可在「版本」中回滚' : '工作流 v2 未开放灰度（全局开关关闭且不在试点白名单），加入白名单后可用' }}
                </n-tooltip>
                <n-button size="tiny" quaternary @click="openRuns(wf.id, wf.name)"><SumeruIcon name="chart" :size="11" variant="duo" tone="view" interactive /> 记录</n-button>
                <n-button size="tiny" quaternary @click="openRevisions(wf.id, wf.name)"><SumeruIcon name="note" :size="11" variant="duo" tone="edit" interactive /> 版本</n-button>
                <n-popconfirm @positive-click="publishWorkflow(wf.id)">
                  <template #trigger>
                    <n-button size="tiny" quaternary :loading="publishing === wf.id"><SumeruIcon name="rocket" :size="12" variant="duo" tone="add" interactive /> 发布</n-button>
                  </template>
                  将当前定义固化为新版本并设为运行版本？
                </n-popconfirm>
                <n-popconfirm @positive-click="deleteWorkflow(wf)">
                  <template #trigger>
                    <n-button size="tiny" type="error" quaternary>{{ t('workflowView.delete') }}</n-button>
                  </template>
                  {{ tf('workflowView.deleteConfirm', wf.name) }}
                </n-popconfirm>
              </div>
            </div>
          </div></Tilt3D>
          <n-empty v-if="!loading && workflows.length === 0"
                   :description="t('workflowView.emptyHint')" class="empty-state" />
        </div>
      </n-spin>
    </div>

    <!-- ── 编辑模式 ── -->
    <div v-else class="editor-section">
      <!-- 基本信息（简化） -->
      <Tilt3D :max-x="4" :max-y="6"><div class="basic-info glass-panel">
        <div class="info-row">
          <n-input v-model:value="editing.name" :placeholder="t('workflowView.namePh')" style="flex:1" />
          <n-switch v-model:value="editing.enabled" size="small" />
          <span class="enable-label">{{ editing.enabled ? t('workflowView.enabled') : t('workflowView.disabled') }}</span>
        </div>
        <n-input v-model:value="editing.description" :placeholder="t('workflowView.descPh')" />
      </div></Tilt3D>

      <!-- 节点链 -->
      <div ref="nodesSectionEl" class="nodes-section">
        <Tilt3D v-if="editing.nodes.length === 0" :max-x="4" :max-y="6"><div class="nodes-empty glass-panel">
          {{ t('workflowView.addStepHint') }}
        </div></Tilt3D>

        <template v-for="(node, idx) in editing.nodes" :key="node.id">
          <!-- 节点卡片 -->
          <Tilt3D><div class="node-card glass-panel">
            <!-- 节点头部 -->
            <div class="node-head">
              <span class="node-num">{{ idx + 1 }}</span>
              <span class="node-icon"><SumeruIcon :name="NODE_META[node.type]?.icon || 'note'" :size="14" /></span>
              <span class="node-type" :style="{ color: NODE_META[node.type]?.color }">
                {{ t('workflowView.nodeType.' + node.type) }}
              </span>
              <!-- step 类型：直接输入说明文本 -->
              <n-input v-if="node.type === 'step'"
                       v-model:value="node.note"
                       :placeholder="t('workflowView.stepNotePh')"
                       size="small"
                       style="flex:1; min-width: 200px" />
              <!-- 其他类型：下拉选择已配置的资源 -->
              <n-select v-else
                        :value="node.ref"
                        :options="getOptions(node.type)"
                        :placeholder="tf('workflowView.selectNodePh', t('workflowView.nodeType.' + node.type))"
                        size="small"
                        filterable
                        style="flex:1; min-width: 200px"
                        @update:value="(v: string) => onNodeSelect(node, v)" />
              <div class="node-actions">
                <n-button size="tiny" quaternary :disabled="idx === 0" @click="moveNode(idx, -1)">↑</n-button>
                <n-button size="tiny" quaternary :disabled="idx === editing.nodes.length - 1" @click="moveNode(idx, 1)">↓</n-button>
                <n-button size="tiny" type="error" quaternary @click="removeNode(node.id)">✕</n-button>
              </div>
            </div>
            <!-- 可选备注 -->
            <n-input v-if="node.type !== 'step'"
                     v-model:value="node.note"
                     :placeholder="t('workflowView.nodeNotePh')"
                     size="small"
                     class="node-note" />
          </div></Tilt3D>
          <!-- 连线箭头 -->
          <div v-if="idx < editing.nodes.length - 1" class="node-arrow">↓</div>
        </template>
      </div>

      <!-- 添加节点工具栏 -->
      <div class="node-toolbar glass-panel">
        <span class="toolbar-label">{{ t('workflowView.addStepLabel') }}</span>
        <n-button v-for="(meta, key) in NODE_META" :key="key" size="small"
                  @click="addNode(key as WorkflowNode['type'])">
          {{ meta.icon }} {{ t('workflowView.nodeType.' + key) }}
        </n-button>
      </div>

      <!-- 操作按钮 -->
      <div class="action-bar">
        <n-button @click="cancelEdit">{{ t('workflowView.back') }}</n-button>
        <n-button type="info" :loading="testing" :disabled="isCreate" @click="testWorkflow">{{ t('workflowView.test') }}</n-button>
        <n-button type="primary" :loading="saving" @click="save">{{ t('workflowView.save') }}</n-button>
      </div>
    </div>

    <n-modal v-model:show="showRunsModal" preset="card" :title="`${runsWfName} — 运行记录`" style="width: min(640px, 94vw)">
      <n-spin :show="runsLoading">
        <div v-if="runsList.length" class="runs-list">
          <div v-for="run in runsList" :key="run.run_id" class="run-item">
            <div class="run-head">
              <n-tag size="small" :type="run.status === 'succeeded' ? 'success' : (run.status === 'failed' || run.status === 'cancelled') ? 'error' : 'info'" :bordered="false">{{ run.status }}</n-tag>
              <span class="run-id mono">{{ run.run_id.slice(0, 8) }}</span>
              <span class="run-time">{{ run.created_at ? new Date(run.created_at * 1000).toLocaleString('zh-CN') : '—' }}</span>
            </div>
            <div v-if="run.output?.error_message" class="run-error">{{ run.output.error_message }}</div>
            <div v-if="run.status === 'waiting_input'" class="run-review-cards">
              <div v-for="review in (reviewsMap[run.run_id] || []).filter(r => r.status === 'pending')"
                   :key="review.review_id" class="review-card">
                <div class="review-title">审批：{{ review.title || review.node_id }}</div>
                <div v-if="review.note" class="review-note">{{ review.note }}</div>
                <n-input v-model:value="noteDetail[review.review_id]" type="textarea" :rows="1"
                         placeholder="决策备注（可选）" size="small" class="review-note-input" />
                <div class="review-actions">
                  <n-button size="small" type="success" :loading="deciding === review.review_id"
                            @click="decideReview(run, review, 'approve')">批准</n-button>
                  <n-button size="small" type="error" :loading="deciding === review.review_id"
                             @click="decideReview(run, review, 'reject')">拒绝</n-button>
                </div>
              </div>
            </div>
            <div class="run-actions">
              <n-button v-if="['queued', 'running', 'waiting_input', 'paused', 'cancelling'].includes(run.status)" size="tiny" type="warning" @click="cancelRun(run.run_id)">取消</n-button>
            </div>
          </div>
        </div>
        <n-empty v-else description="暂无运行记录" />
      </n-spin>
    </n-modal>

    <n-modal v-model:show="showRevisionsModal" preset="card" :title="`${revisionsWfName} — 版本历史`" style="width: min(580px, 94vw)">
      <n-spin :show="revisionsLoading">
        <div v-if="revisionsList.length" class="revisions-list">
          <div v-for="(rev, idx) in revisionsList" :key="rev.revision_id" class="rev-item">
            <div class="rev-head">
              <n-tag size="small" :bordered="false">v{{ revisionsList.length - idx }}</n-tag>
              <span class="rev-time">{{ rev.created_at ? new Date(rev.created_at * 1000).toLocaleString('zh-CN') : '—' }}</span>
              <span class="rev-hash mono">{{ (rev.content_hash || '').slice(0, 8) }}</span>
              <n-tag v-if="rev.current" size="tiny" type="success" :bordered="false">当前版本</n-tag>
            </div>
            <div v-if="!rev.current" class="rev-actions">
              <n-popconfirm @positive-click="rollbackRevision(rev)">
                <template #trigger>
                  <n-button size="tiny" :loading="rollingBack === rev.revision_id">回滚</n-button>
                </template>
                回滚后，运行将使用该版本编排（不可撤销）？
              </n-popconfirm>
            </div>
          </div>
        </div>
        <n-empty v-else description="暂无版本——点击「发布」生成第一个版本" />
      </n-spin>
    </n-modal>
  </div>
</template>

<style scoped>
.workflows-view { display: flex; flex-direction: column; gap: 14px; }

.view-header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 6px; }
.view-header h2 { font-family: 'Noto Serif SC', serif; }
.count { color: var(--moon-dim); font-size: 13px; }

/* ── 列表 ── */
.list-toolbar { display: flex; justify-content: flex-end; margin-bottom: 10px; }
.wf-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}
.wf-card { padding: 12px 14px; display: flex; flex-direction: column; gap: 6px; }
.wf-card-head { display: flex; align-items: center; gap: 8px; }
.wf-name { font-weight: 600; font-size: 14px; color: var(--moon); flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wf-desc {
  font-size: 12.5px; color: var(--moon-dim); min-height: 18px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.wf-card-footer { display: flex; align-items: center; justify-content: space-between; margin-top: 4px; }
.wf-meta { display: flex; align-items: center; gap: 8px; }
.wf-card-actions { display: flex; gap: 6px; }
.empty-state { grid-column: 1 / -1; padding: 40px 0; }

/* ── 编辑器 ── */
.editor-section { display: flex; flex-direction: column; gap: 14px; }

.basic-info { padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; }
.info-row { display: flex; align-items: center; gap: 10px; }
.enable-label { font-size: 12px; color: var(--moon-dim); flex-shrink: 0; }

.nodes-section { display: flex; flex-direction: column; align-items: stretch; }
.nodes-empty {
  padding: 32px 16px; text-align: center; color: var(--moon-dim); font-size: 14px;
}

/* ── 节点卡片 ── */
.node-card {
  padding: 10px 14px; display: flex; flex-direction: column; gap: 8px;
  transition: border-color 0.2s;
}
.node-head { display: flex; align-items: center; gap: 8px; }
.node-num {
  width: 22px; height: 22px; border-radius: 50%;
  background: rgba(127, 214, 80, 0.15); color: var(--dendro);
  font-size: 12px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.node-icon { font-size: 16px; flex-shrink: 0; }
.node-type {
  font-size: 11px; font-weight: 600; flex-shrink: 0;
  padding: 2px 8px; border-radius: 10px; background: rgba(255,255,255,0.06);
}
.node-actions { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
.node-note { opacity: 0.7; }

.node-arrow {
  text-align: center; color: var(--dendro); font-size: 18px;
  line-height: 1; padding: 2px 0; opacity: 0.5;
}

/* ── 工具栏 ── */
.node-toolbar {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  flex-wrap: wrap;
}
.toolbar-label { font-size: 13px; color: var(--moon-dim); }

/* ── 操作栏 ── */
.action-bar { display: flex; justify-content: flex-end; gap: 10px; padding-top: 4px; }

.runs-list, .revisions-list { display: flex; flex-direction: column; gap: 8px; }
.run-item, .rev-item { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--glass-border); }
.run-head, .rev-head { display: flex; align-items: center; gap: 8px; }
.run-id { font-size: 12px; }
.run-time, .rev-time { font-size: 12px; color: var(--moon-dim); flex: 1; }
.rev-hash { font-size: 11px; color: var(--moon-dim); font-family: var(--mono, monospace); }
.run-error { font-size: 12px; color: var(--alert); margin-top: 4px; }
.run-actions, .rev-actions { margin-top: 4px; }
.rev-desc { font-size: 12px; color: var(--moon-dim); margin-top: 4px; }

/* REVIEW 审批卡片（M5）：waiting_input 的 run 内嵌待批单 */
.run-review-cards { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
.review-card { padding: 8px 10px; border-radius: 8px; border: 1px dashed var(--glass-border); background: var(--glass-bg); }
.review-title { font-size: 13px; font-weight: 600; }
.review-note { font-size: 12px; color: var(--moon-dim); margin-top: 2px; white-space: pre-wrap; }
.review-note-input { margin-top: 6px; }
.review-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 6px; }

@media (max-width: 768px) {
  .info-row { flex-direction: column; align-items: stretch; }
}
</style>