<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import {
  NButton, NSwitch, NInput, NSelect, NPopconfirm, NTag, NSpin, NEmpty, useMessage,
} from 'naive-ui'
import { api, type Workflow, type WorkflowNode, type WorkflowSummary } from '../api'
import { useChatStore } from '../stores/chat'
import { t } from '../i18n'

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
  tool:  { icon: '🔧', label: '工具',     color: '#7fd650' },
  skill: { icon: '📜', label: '技能',     color: '#e8d5a3' },
  mcp:   { icon: '🔌', label: 'MCP',      color: '#5fb3d9' },
  agent: { icon: '🤖', label: '子智能体',  color: '#d97fd9' },
  model: { icon: '🧠', label: '模型',     color: '#5fd9c4' },
  step:  { icon: '📝', label: '步骤说明',  color: '#d96a5f' },
}

onMounted(() => {
  load()
  loadResources()
})

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

async function loadResources() {
  try {
    const [tools, skills, mcpServers, agents, providers] = await Promise.all([
      api.getTools().catch(() => []),
      api.getSkills().catch(() => []),
      api.getMcpServers().catch(() => []),
      api.getAgents().catch(() => []),
      api.getProviders().catch(() => []),
    ])

    resourceOptions.value.tools = tools
      .filter((t: any) => t.enabled)
      .map((t: any) => ({ label: `${t.name} — ${t.description || ''}`, value: t.name }))

    resourceOptions.value.skills = skills.map((s: any) => ({ label: s.name, value: s.name }))

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

    resourceOptions.value.models = providers
      .filter((p: any) => p.enabled)
      .map((p: any) => ({ label: `${p.label} (${p.default_model || p.id})`, value: p.id }))
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
    message.success('已删除 ' + wf.name)
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
  if (!editing.value.name.trim()) { message.error('请填写工作流名称'); return }
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
    message.success('已保存 ✓')
    await load()
  } catch (e: any) { message.error(e.message) }
  finally { saving.value = false }
}

async function testWorkflow() {
  if (!editing.value || isCreate.value) { message.warning('请先保存'); return }
  if (chatStore.isProcessing) { message.warning('对话正在处理中'); return }
  testing.value = true
  try {
    const prompt = await api.previewWorkflow(editing.value.id)
    chatStore.sendMessage(typeof prompt === 'string' ? prompt : JSON.stringify(prompt))
    router.push('/')
    message.success('已发送到对话窗口')
  } catch (e: any) { message.error(e.message) }
  finally { testing.value = false }
}

// ── 节点操作 ──
function addNode(type: WorkflowNode['type']) {
  if (!editing.value) return
  const meta = NODE_META[type]
  const node: WorkflowNode = {
    id: `n${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
    type, label: meta.label,
  }
  if (type !== 'step') node.ref = ''
  editing.value.nodes.push(node)
}

function removeNode(id: string) {
  if (!editing.value) return
  editing.value.nodes = editing.value.nodes.filter(n => n.id !== id)
}

function moveNode(idx: number, dir: -1 | 1) {
  if (!editing.value) return
  const nodes = editing.value.nodes
  const newIdx = idx + dir
  if (newIdx < 0 || newIdx >= nodes.length) return
  ;[nodes[idx], nodes[newIdx]] = [nodes[newIdx], nodes[idx]]
}

// 节点选择资源时，自动更新 label
function onNodeSelect(node: WorkflowNode, value: string) {
  node.ref = value
  const opts = getOptions(node.type)
  const found = opts.find(o => o.value === value)
  if (found) node.label = found.label.split(' — ')[0].split(' / ').pop() || found.label
}
</script>

<template>
  <div class="workflows-view">
    <div class="view-header">
      <h2>🌿 工作流</h2>
      <span class="count">共 {{ workflows.length }} 个</span>
    </div>

    <!-- ── 列表模式 ── -->
    <div v-if="!editing" class="list-section">
      <div class="list-toolbar">
        <n-button type="primary" @click="newWorkflow">＋ 新建工作流</n-button>
      </div>
      <n-spin :show="loading">
        <div class="wf-grid">
          <div v-for="wf in workflows" :key="wf.id" class="wf-card glass-panel glass-panel-hover">
            <div class="wf-card-head">
              <span class="wf-name">{{ wf.name }}</span>
              <n-tag size="tiny" :bordered="false">v{{ wf.version }}</n-tag>
            </div>
            <div class="wf-desc">{{ wf.description || '（无描述）' }}</div>
            <div class="wf-card-footer">
              <div class="wf-meta">
                <n-tag size="tiny" :bordered="false">{{ wf.node_count }} 步</n-tag>
                <n-switch :value="wf.enabled" size="small"
                          @update:value="(v: boolean) => toggleEnabled(wf, v)" />
              </div>
              <div class="wf-card-actions">
                <n-button size="tiny" type="primary" @click="editWorkflow(wf)">编辑</n-button>
                <n-popconfirm @positive-click="deleteWorkflow(wf)">
                  <template #trigger>
                    <n-button size="tiny" type="error" quaternary>删除</n-button>
                  </template>
                  确认删除「{{ wf.name }}」？
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
      <!-- 基本信息（简化） -->
      <div class="basic-info glass-panel">
        <div class="info-row">
          <n-input v-model:value="editing.name" placeholder="工作流名称（如：邮箱配置）" style="flex:1" />
          <n-switch v-model:value="editing.enabled" size="small" />
          <span class="enable-label">{{ editing.enabled ? '启用' : '禁用' }}</span>
        </div>
        <n-input v-model:value="editing.description" placeholder="简单描述这个工作流做什么（可选）" />
      </div>

      <!-- 节点链 -->
      <div class="nodes-section">
        <div v-if="editing.nodes.length === 0" class="nodes-empty glass-panel">
          👇 点击下方按钮添加步骤
        </div>

        <template v-for="(node, idx) in editing.nodes" :key="node.id">
          <!-- 节点卡片 -->
          <div class="node-card glass-panel">
            <!-- 节点头部 -->
            <div class="node-head">
              <span class="node-num">{{ idx + 1 }}</span>
              <span class="node-icon">{{ NODE_META[node.type]?.icon }}</span>
              <span class="node-type" :style="{ color: NODE_META[node.type]?.color }">
                {{ NODE_META[node.type]?.label }}
              </span>
              <!-- step 类型：直接输入说明文本 -->
              <n-input v-if="node.type === 'step'"
                       v-model:value="node.note"
                       placeholder="输入操作说明…"
                       size="small"
                       style="flex:1; min-width: 200px" />
              <!-- 其他类型：下拉选择已配置的资源 -->
              <n-select v-else
                        :value="node.ref"
                        :options="getOptions(node.type)"
                        :placeholder="`选择${NODE_META[node.type]?.label}…`"
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
                     placeholder="备注（可选）：这个步骤有什么注意事项？"
                     size="small"
                     class="node-note" />
          </div>
          <!-- 连线箭头 -->
          <div v-if="idx < editing.nodes.length - 1" class="node-arrow">↓</div>
        </template>
      </div>

      <!-- 添加节点工具栏 -->
      <div class="node-toolbar glass-panel">
        <span class="toolbar-label">添加步骤：</span>
        <n-button v-for="(meta, key) in NODE_META" :key="key" size="small"
                  @click="addNode(key as WorkflowNode['type'])">
          {{ meta.icon }} {{ meta.label }}
        </n-button>
      </div>

      <!-- 操作按钮 -->
      <div class="action-bar">
        <n-button @click="cancelEdit">返回</n-button>
        <n-button type="info" :loading="testing" :disabled="isCreate" @click="testWorkflow">测试</n-button>
        <n-button type="primary" :loading="saving" @click="save">保存</n-button>
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

@media (max-width: 768px) {
  .info-row { flex-direction: column; align-items: stretch; }
}
</style>
