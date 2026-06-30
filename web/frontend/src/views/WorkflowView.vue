<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import {
  NButton, NSwitch, NInput, NPopconfirm, NTag, NSpin, NEmpty, useMessage,
} from 'naive-ui'
import { api, type Workflow, type WorkflowNode, type WorkflowSummary } from '../api'
import { useChatStore } from '../stores/chat'
import { t } from '../i18n'

const message = useMessage()
const router = useRouter()
const chatStore = useChatStore()

const workflows = ref<WorkflowSummary[]>([])
const loading = ref(false)
const editing = ref<Workflow | null>(null)
const isCreate = ref(false)
const saving = ref(false)
const testing = ref(false)
const expandedNodeId = ref<string | null>(null)
// 节点参数 JSON 文本草稿（按 node id 索引），避免每次输入都需合法 JSON
const paramsDraft = ref<Record<string, string>>({})

const NODE_TYPES = [
  { type: 'tool', icon: '🔧', label: '工具' },
  { type: 'skill', icon: '📜', label: '技能' },
  { type: 'mcp', icon: '🔌', label: 'MCP' },
  { type: 'agent', icon: '🤖', label: '子智能体' },
  { type: 'model', icon: '🧠', label: '模型' },
  { type: 'step', icon: '📝', label: '步骤说明' },
] as const

const NODE_META: Record<string, { icon: string; label: string }> = {
  tool: { icon: '🔧', label: '工具' },
  skill: { icon: '📜', label: '技能' },
  mcp: { icon: '🔌', label: 'MCP' },
  agent: { icon: '🤖', label: '子智能体' },
  model: { icon: '🧠', label: '模型' },
  step: { icon: '📝', label: '步骤说明' },
}

onMounted(load)

async function load() {
  loading.value = true
  try {
    workflows.value = await api.listWorkflows()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

function newWorkflow() {
  editing.value = {
    id: '',
    name: '',
    description: '',
    version: '1.0.0',
    enabled: true,
    nodes: [],
    edges: [],
    trigger: 'manual',
  }
  isCreate.value = true
  expandedNodeId.value = null
  paramsDraft.value = {}
}

async function editWorkflow(wf: WorkflowSummary) {
  try {
    const full = await api.getWorkflow(wf.id)
    editing.value = full
    isCreate.value = false
    expandedNodeId.value = null
    paramsDraft.value = {}
  } catch (e: any) {
    message.error(e.message)
  }
}

async function deleteWorkflow(wf: WorkflowSummary) {
  try {
    await api.deleteWorkflow(wf.id)
    message.success(t('deleted') + ' ' + wf.name)
    await load()
  } catch (e: any) {
    message.error(e.message)
  }
}

async function toggleEnabled(wf: WorkflowSummary, val: boolean) {
  try {
    const full = await api.getWorkflow(wf.id)
    full.enabled = val
    await api.updateWorkflow(wf.id, full)
    wf.enabled = val
    message.success(val ? t('enabled') : t('disabled'))
  } catch (e: any) {
    message.error(e.message)
  }
}

function cancelEdit() {
  editing.value = null
}

async function save() {
  if (!editing.value) return
  if (!editing.value.name.trim()) {
    message.error('请填写工作流名称')
    return
  }
  saving.value = true
  try {
    // 线性序列：按节点顺序生成 edges
    const wf = editing.value
    wf.edges = []
    for (let i = 0; i < wf.nodes.length - 1; i++) {
      wf.edges.push([wf.nodes[i].id, wf.nodes[i + 1].id])
    }
    if (isCreate.value) {
      const created = await api.createWorkflow(wf)
      message.success('工作流已创建 ✓')
      editing.value = created
      isCreate.value = false
    } else {
      const updated = await api.updateWorkflow(wf.id, wf)
      message.success('已保存 ✓')
      editing.value = updated
    }
    await load()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    saving.value = false
  }
}

async function testWorkflow() {
  if (!editing.value) return
  if (isCreate.value) {
    message.warning('请先保存工作流')
    return
  }
  if (chatStore.isProcessing) {
    message.warning('对话正在处理中，请稍后再试')
    return
  }
  testing.value = true
  try {
    const prompt = await api.previewWorkflow(editing.value.id)
    const text = typeof prompt === 'string' ? prompt : JSON.stringify(prompt)
    chatStore.sendMessage(text)
    router.push('/')
    message.success('已发送到对话窗口')
  } catch (e: any) {
    message.error(e.message)
  } finally {
    testing.value = false
  }
}

// ── 节点操作 ──
function addNode(type: WorkflowNode['type']) {
  if (!editing.value) return
  const meta = NODE_META[type]
  const node: WorkflowNode = {
    id: `n${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    type,
    label: meta.label,
  }
  if (type !== 'step') node.ref = ''
  editing.value.nodes.push(node)
  expandedNodeId.value = node.id
}

function removeNode(id: string) {
  if (!editing.value) return
  editing.value.nodes = editing.value.nodes.filter(n => n.id !== id)
  delete paramsDraft.value[id]
  if (expandedNodeId.value === id) expandedNodeId.value = null
}

function moveNode(idx: number, dir: -1 | 1) {
  if (!editing.value) return
  const nodes = editing.value.nodes
  const newIdx = idx + dir
  if (newIdx < 0 || newIdx >= nodes.length) return
  ;[nodes[idx], nodes[newIdx]] = [nodes[newIdx], nodes[idx]]
}

function toggleExpand(id: string) {
  expandedNodeId.value = expandedNodeId.value === id ? null : id
}

function getParamsDraft(node: WorkflowNode): string {
  if (paramsDraft.value[node.id] === undefined) {
    paramsDraft.value[node.id] = node.params ? JSON.stringify(node.params, null, 2) : ''
  }
  return paramsDraft.value[node.id]
}

function setParamsDraft(node: WorkflowNode, text: string) {
  paramsDraft.value[node.id] = text
  if (text.trim()) {
    try {
      node.params = JSON.parse(text)
    } catch {
      // 草稿允许非法 JSON，保存时再校验
    }
  } else {
    node.params = undefined
  }
}
</script>

<template>
  <div class="workflows-view">
    <!-- 标题栏 -->
    <div class="view-header">
      <h2>🌿 工作流</h2>
      <span class="count">{{ t('total') }} {{ workflows.length }} {{ t('items') }}</span>
    </div>

    <!-- ── 列表模式 ── -->
    <div v-if="!editing" class="list-section">
      <div class="list-toolbar">
        <n-button type="primary" @click="newWorkflow">＋ {{ t('add') }}</n-button>
      </div>
      <n-spin :show="loading">
        <div class="wf-grid">
          <div v-for="wf in workflows" :key="wf.id" class="wf-card glass-panel glass-panel-hover">
            <div class="wf-card-head">
              <span class="wf-name">{{ wf.name }}</span>
              <n-tag size="tiny" :bordered="false">v{{ wf.version }}</n-tag>
            </div>
            <div class="wf-desc">{{ wf.description || '（' + t('empty') + '）' }}</div>
            <div class="wf-card-footer">
              <div class="wf-meta">
                <n-tag size="tiny" :bordered="false">{{ wf.node_count }} 节点</n-tag>
                <n-switch :value="wf.enabled" size="small"
                          @update:value="(v: boolean) => toggleEnabled(wf, v)" />
              </div>
              <div class="wf-card-actions">
                <n-button size="tiny" type="primary" @click="editWorkflow(wf)">{{ t('edit') }}</n-button>
                <n-popconfirm @positive-click="deleteWorkflow(wf)">
                  <template #trigger>
                    <n-button size="tiny" type="error" quaternary>{{ t('delete') }}</n-button>
                  </template>
                  {{ t('confirm') }}「{{ wf.name }}」？
                </n-popconfirm>
              </div>
            </div>
          </div>
          <n-empty v-if="!loading && workflows.length === 0"
                   description="还没有工作流，点「＋」创建一个吧" class="empty-state" />
        </div>
      </n-spin>
    </div>

    <!-- ── 编辑模式 ── -->
    <div v-else class="editor-section">
      <!-- 基本信息 -->
      <div class="basic-info glass-panel">
        <div class="info-row">
          <label class="info-label">名称</label>
          <n-input v-model:value="editing.name" placeholder="工作流名称" style="max-width: 260px" />
          <label class="info-label">版本</label>
          <n-input v-model:value="editing.version" placeholder="1.0.0" style="max-width: 100px" />
          <label class="info-label">{{ t('enabled') }}</label>
          <n-switch v-model:value="editing.enabled" size="small" />
        </div>
        <div class="info-row">
          <label class="info-label">描述</label>
          <n-input v-model:value="editing.description" placeholder="工作流描述" />
        </div>
        <div class="info-row">
          <label class="info-label">触发</label>
          <n-input v-model:value="editing.trigger" placeholder="manual / webhook / schedule" style="max-width: 220px" />
        </div>
      </div>

      <!-- 节点编辑器 -->
      <div class="nodes-section">
        <div v-if="editing.nodes.length === 0" class="nodes-empty glass-panel">
          暂无节点，点击下方工具栏添加
        </div>
        <template v-for="(node, idx) in editing.nodes" :key="node.id">
          <div class="node-card glass-panel" :class="{ expanded: expandedNodeId === node.id }">
            <div class="node-head" @click="toggleExpand(node.id)">
              <span class="node-icon">{{ NODE_META[node.type]?.icon || '•' }}</span>
              <span class="node-type">{{ NODE_META[node.type]?.label || node.type }}</span>
              <span class="node-label">{{ node.label }}</span>
              <span v-if="node.ref" class="node-ref">{{ node.ref }}</span>
              <div class="node-actions" @click.stop>
                <n-button size="tiny" quaternary :disabled="idx === 0" @click="moveNode(idx, -1)">↑</n-button>
                <n-button size="tiny" quaternary :disabled="idx === editing.nodes.length - 1" @click="moveNode(idx, 1)">↓</n-button>
                <n-popconfirm @positive-click="removeNode(node.id)">
                  <template #trigger>
                    <n-button size="tiny" type="error" quaternary>{{ t('delete') }}</n-button>
                  </template>
                  {{ t('confirm') }}{{ t('delete') }}？
                </n-popconfirm>
              </div>
            </div>
            <div v-if="expandedNodeId === node.id" class="node-body" @click.stop>
              <div class="form-row">
                <label>标签</label>
                <n-input v-model:value="node.label" placeholder="节点标签" />
              </div>
              <div v-if="node.type === 'tool'" class="form-row">
                <label>工具名 (ref)</label>
                <n-input v-model:value="node.ref" placeholder="如 web_search" />
              </div>
              <div v-else-if="node.type === 'skill'" class="form-row">
                <label>技能名 (ref)</label>
                <n-input v-model:value="node.ref" placeholder="如 code-review" />
              </div>
              <div v-else-if="node.type === 'mcp'" class="form-row">
                <label>MCP 工具名 (ref)</label>
                <n-input v-model:value="node.ref" placeholder="如 filesystem.read" />
              </div>
              <div v-else-if="node.type === 'agent'" class="form-row">
                <label>子智能体名 (ref)</label>
                <n-input v-model:value="node.ref" placeholder="如 hutao" />
              </div>
              <div v-else-if="node.type === 'model'" class="form-row">
                <label>模型 ID (ref)</label>
                <n-input v-model:value="node.ref" placeholder="如 mimo|mimo-vl-7b" />
              </div>
              <div v-if="node.type === 'tool'" class="form-row">
                <label>参数 (JSON)</label>
                <n-input :value="getParamsDraft(node)" type="textarea" :rows="4"
                         placeholder='{"key": "value"}'
                         @update:value="(v: string) => setParamsDraft(node, v)" />
              </div>
              <div class="form-row">
                <label>操作说明</label>
                <n-input v-model:value="node.note" type="textarea" :rows="2"
                         placeholder="该节点的操作说明" />
              </div>
              <div class="form-row">
                <label>预期结果</label>
                <n-input v-model:value="node.expect" type="textarea" :rows="2"
                         placeholder="预期输出/结果" />
              </div>
            </div>
          </div>
          <div v-if="idx < editing.nodes.length - 1" class="node-arrow">↓</div>
        </template>
      </div>

      <!-- 节点类型工具栏 -->
      <div class="node-toolbar glass-panel">
        <span class="toolbar-label">添加节点：</span>
        <n-button v-for="nt in NODE_TYPES" :key="nt.type" size="small"
                  @click="addNode(nt.type)">
          {{ nt.icon }} {{ nt.label }}
        </n-button>
      </div>

      <!-- 操作按钮 -->
      <div class="action-bar">
        <n-button @click="cancelEdit">{{ t('cancel') }}</n-button>
        <n-button type="primary" :loading="saving" @click="save">{{ t('save') }}</n-button>
        <n-button type="info" :loading="testing" :disabled="isCreate" @click="testWorkflow">
          测试
        </n-button>
      </div>
    </div>
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

.basic-info { padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }
.info-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.info-label {
  font-size: 13px; color: var(--moon-dim); min-width: 48px;
  flex-shrink: 0;
}

.nodes-section { display: flex; flex-direction: column; align-items: stretch; }
.nodes-empty {
  padding: 32px 16px; text-align: center; color: var(--moon-dim); font-size: 13px;
}

.node-card {
  padding: 10px 14px; transition: border-color 0.2s;
}
.node-card.expanded { border-color: rgba(127, 214, 80, 0.5); }

.node-head {
  display: flex; align-items: center; gap: 10px; cursor: pointer;
  user-select: none;
}
.node-head:hover { color: var(--dendro); }
.node-icon { font-size: 18px; flex-shrink: 0; }
.node-type {
  font-size: 11px; color: var(--dendro); padding: 2px 8px;
  border-radius: 10px; background: rgba(127, 214, 80, 0.12);
  flex-shrink: 0;
}
.node-label {
  font-size: 13.5px; font-weight: 600; color: var(--moon); flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.node-ref {
  font-family: 'JetBrains Mono', monospace; font-size: 12px;
  color: var(--moon-dim); flex-shrink: 0;
}
.node-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; }

.node-body {
  margin-top: 10px; padding-top: 10px;
  border-top: 1px solid var(--glass-border);
  display: flex; flex-direction: column; gap: 10px;
}
.form-row { display: flex; flex-direction: column; gap: 4px; }
.form-row > label { font-size: 12px; color: var(--moon-dim); }

.node-arrow {
  text-align: center; color: var(--dendro); font-size: 18px;
  line-height: 1; padding: 4px 0; opacity: 0.7;
}

/* ── 工具栏 ── */
.node-toolbar {
  display: flex; align-items: center; gap: 8px; padding: 10px 14px;
  flex-wrap: wrap;
}
.toolbar-label { font-size: 13px; color: var(--moon-dim); }

/* ── 操作栏 ── */
.action-bar {
  display: flex; justify-content: flex-end; gap: 10px;
  padding-top: 4px;
}

@media (max-width: 768px) {
  .info-row { flex-direction: column; align-items: stretch; }
  .info-label { min-width: 0; }
}
</style>
