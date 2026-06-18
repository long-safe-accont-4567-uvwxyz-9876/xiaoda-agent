<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import {
  NButton, NSwitch, NModal, NForm, NFormItem, NInput, NInputNumber,
  NSelect, NTabs, NTabPane, NTag, NPopconfirm, NDynamicTags, useMessage,
} from 'naive-ui'
import { get, post, put, del } from '../api'
import { useAgentsStore } from '../stores/agents'
import Tilt3D from '../components/fx/Tilt3D.vue'

const message = useMessage()
const agentsStore = useAgentsStore()

const showEditor = ref(false)
const isCreate = ref(false)
const editing = ref<any>({})
const personality = ref('')
const permissions = ref<any>({ tools: {}, mcp_servers: {}, is_main: false })
const permDirty = ref(false)
const testResult = ref<any>(null)
const testing = ref(false)
const saving = ref(false)
const providerOptions = ref<Array<{ label: string; value: string }>>([])
const wpInput = ref<HTMLInputElement | null>(null)
const uploadingWp = ref(false)

onMounted(() => {
  agentsStore.load().catch((e) => message.error(e.message))
  loadProviders()
})

async function loadProviders() {
  try {
    const ps = await get<any[]>('/models/providers')
    providerOptions.value = ps
      .filter(p => p.enabled !== false)
      .map(p => ({ label: p.label ? `${p.label}（${p.id}）` : p.id, value: p.id }))
  } catch { /* 忽略，保留手输 */ }
}

function pickWallpaper(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 8 * 1024 * 1024) {
    message.error('图片不能超过 8MB')
    input.value = ''
    return
  }
  const reader = new FileReader()
  reader.onload = async () => {
    uploadingWp.value = true
    try {
      const r = await post<any>(`/agents/${editing.value.name}/wallpaper`,
        { data_url: reader.result })
      editing.value.wallpaper = r.wallpaper
      message.success('背景板已更新，切到该 Agent 即可看到 ✓')
      await agentsStore.load()
    } catch (err: any) {
      message.error(err.message)
    } finally {
      uploadingWp.value = false
      input.value = ''
    }
  }
  reader.readAsDataURL(file)
}

const effortOptions = ['low', 'medium', 'high'].map(v => ({ label: v, value: v }))
const permModeOptions = ['default', 'dev', 'strict'].map(v => ({ label: v, value: v }))
const memScopeOptions = ['shared', 'isolated'].map(v => ({ label: v, value: v }))

const isMain = computed(() => editing.value?.name === 'nahida' || editing.value?.is_main === true)

const toolGroups = computed(() => {
  const groups: Record<string, Array<[string, any]>> = {}
  for (const [name, info] of Object.entries<any>(permissions.value.tools || {})) {
    const cat = name.startsWith('mcp_') ? `MCP · ${name.split('_')[1]}` : (toolCategory.value[name] || 'general')
    if (!groups[cat]) groups[cat] = []
    groups[cat].push([name, info])
  }
  return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b))
})

const toolCategory = ref<Record<string, string>>({})

async function loadToolCategories() {
  try {
    const tools = await get<any[]>('/tools')
    toolCategory.value = Object.fromEntries(tools.map(t => [t.name, t.category]))
  } catch { /* 忽略 */ }
}

async function openEditor(agent: any | null) {
  isCreate.value = !agent
  testResult.value = null
  permDirty.value = false
  if (agent) {
    editing.value = JSON.parse(JSON.stringify(agent))
    try {
      const p = await get(`/agents/${agent.name}/personality`)
      personality.value = p.personality || ''
    } catch { personality.value = '' }
    try {
      permissions.value = await get(`/agents/${agent.name}/permissions`)
      loadToolCategories()
    } catch { permissions.value = { tools: {}, mcp_servers: {}, is_main: false } }
  } else {
    editing.value = {
      name: '', display_name: '', provider: 'mimo', model: '',
      base_url: '', api_key_env: '', route_description: '', capabilities: [],
      voice_ref: null, max_turns: 8, effort: 'medium',
      permission_mode: 'default', memory_scope: 'shared', wallpaper: '',
    }
    personality.value = ''
    permissions.value = { tools: {}, mcp_servers: {}, is_main: false }
  }
  showEditor.value = true
}

async function save() {
  saving.value = true
  try {
    const body = { ...editing.value, personality_text: personality.value || undefined }
    delete body.tool_count
    if (isCreate.value) {
      await post('/agents', body)
      message.success(`Agent ${editing.value.display_name || editing.value.name} 已创建并即时生效 ✓`)
    } else {
      await put(`/agents/${editing.value.name}`, body)
      message.success('已保存，下一条消息即用新配置 ✓')
    }
    showEditor.value = false
    await agentsStore.load()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    saving.value = false
  }
}

async function toggleEnabled(agent: any, value: boolean) {
  try {
    await post(`/agents/${agent.name}/${value ? 'enable' : 'disable'}`)
    agent.enabled = value
    message.success(`${agent.display_name} 已${value ? '启用' : '禁用'} ✓`)
  } catch (e: any) {
    message.error(e.message)
  }
}

async function removeAgent(agent: any) {
  try {
    await del(`/agents/${agent.name}`, true)
    message.success(`${agent.display_name} 已删除`)
    await agentsStore.load()
  } catch (e: any) {
    message.error(e.message)
  }
}

function togglePerm(name: string, value: boolean) {
  permissions.value.tools[name].enabled = value
  permDirty.value = true
}

function toggleMcpPerm(name: string, value: boolean) {
  permissions.value.mcp_servers[name].enabled = value
  permDirty.value = true
}

function groupSetAll(group: Array<[string, any]>, value: boolean) {
  for (const [name, info] of group) {
    if (!info.locked) {
      permissions.value.tools[name].enabled = value
      permDirty.value = true
    }
  }
}

async function applyPermissions() {
  try {
    const tools = Object.fromEntries(
      Object.entries<any>(permissions.value.tools)
        .filter(([, v]) => !v.locked)
        .map(([k, v]) => [k, v.enabled]))
    const mcp = Object.fromEntries(
      Object.entries<any>(permissions.value.mcp_servers).map(([k, v]) => [k, v.enabled]))
    const result = await put(`/agents/${editing.value.name}/permissions`,
      { tools, mcp_servers: mcp })
    permissions.value = result
    permDirty.value = false
    const count = Object.values<any>(result.tools).filter(t => t.enabled).length
    message.success(`${editing.value.display_name} 现在拥有 ${count} 个工具 ✓ 即时生效`)
    await agentsStore.load()
  } catch (e: any) {
    message.error(e.message)
  }
}

async function runTest() {
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await post(`/agents/${editing.value.name}/test`)
  } catch (e: any) {
    testResult.value = { ok: false, error: e.message }
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <div class="agents-view">
    <div class="view-header">
      <h2>🧚 Agent 管理</h2>
      <n-button type="primary" @click="openEditor(null)">＋ 新建子 Agent</n-button>
    </div>

    <div class="agent-grid">
      <Tilt3D v-for="a in agentsStore.agents" :key="a.name">
        <div class="agent-card glass-panel glass-panel-hover" @click="openEditor(a)">
          <div class="card-head">
            <span class="agent-avatar"
                  :style="a.wallpaper ? { backgroundImage: `url('${a.wallpaper}')` } : undefined">
              <template v-if="!a.wallpaper">{{ a.display_name.slice(0, 1) }}</template>
            </span>
            <div class="agent-names">
              <span class="agent-display">{{ a.display_name }}</span>
              <span class="agent-id">{{ a.name }}</span>
            </div>
            <n-switch v-if="!a.is_main" size="small" :value="a.enabled"
                      @click.stop @update:value="(v: boolean) => toggleEnabled(a, v)" />
          </div>
          <div class="card-meta">
            <n-tag size="small" :bordered="false" type="success">{{ a.provider }}</n-tag>
            <n-tag size="small" :bordered="false">{{ a.model || '默认' }}</n-tag>
            <n-tag size="small" :bordered="false" :type="a.builtin || a.is_main ? 'warning' : 'info'">
              {{ a.is_main ? '主体' : a.builtin ? '内置' : '自建' }}
            </n-tag>
          </div>
          <div class="card-stats">
            🛠 {{ a.tool_count ?? '—' }} 个工具
            <span v-if="a.mcp_servers?.length"> · 🔌 {{ a.mcp_servers.length }} 个 MCP</span>
          </div>
          <div class="card-desc">{{ a.route_description || '（无路由描述）' }}</div>
          <div class="card-actions" v-if="!a.builtin && !a.is_main">
            <n-popconfirm @positive-click="removeAgent(a)">
              <template #trigger>
                <n-button size="tiny" type="error" quaternary @click.stop>删除</n-button>
              </template>
              确认删除 {{ a.display_name }}？人格与配置文件将一并移除。
            </n-popconfirm>
          </div>
        </div>
      </Tilt3D>
    </div>

    <n-modal v-model:show="showEditor" preset="card" class="agent-modal"
             :title="isCreate ? '新建子 Agent' : `编辑 · ${editing.display_name || editing.name}`"
             style="width: min(860px, 94vw); max-height: 88vh; overflow-y: auto;">
      <n-tabs type="line" animated>
        <n-tab-pane name="base" tab="基本配置">
          <n-form label-placement="left" label-width="130">
            <n-form-item label="标识名 name" v-if="isCreate">
              <n-input v-model:value="editing.name" placeholder="小写字母/数字/下划线，如 hutao" />
            </n-form-item>
            <n-form-item label="显示名">
              <n-input v-model:value="editing.display_name" placeholder="如 胡桃" />
            </n-form-item>
            <n-form-item label="provider" v-if="!isMain">
              <n-select v-if="providerOptions.length" v-model:value="editing.provider"
                        :options="providerOptions" filterable tag
                        placeholder="选择 provider（可输入自建 id）" />
              <n-input v-else v-model:value="editing.provider" placeholder="mimo / agnes / 自建 provider id" />
            </n-form-item>
            <n-form-item label="model" v-if="!isMain">
              <n-input v-model:value="editing.model" placeholder="模型名（留空用 provider 默认）" />
            </n-form-item>
            <n-form-item label="base_url" v-if="!isMain">
              <n-input v-model:value="editing.base_url" placeholder="可选，覆盖 provider 的接口地址" />
            </n-form-item>
            <n-form-item label="api_key_env" v-if="!isMain">
              <n-input v-model:value="editing.api_key_env" placeholder="可选，密钥环境变量名" />
            </n-form-item>
            <n-form-item label="路由描述" v-if="!isMain">
              <n-input v-model:value="editing.route_description" type="textarea" :rows="2"
                       placeholder="自然语言描述何时召唤该 Agent（主体据此自动委托）" />
            </n-form-item>
            <n-form-item label="capabilities" v-if="!isMain">
              <n-dynamic-tags v-model:value="editing.capabilities" />
            </n-form-item>
            <n-form-item label="max_turns" v-if="!isMain">
              <n-input-number v-model:value="editing.max_turns" :min="1" :max="30" />
            </n-form-item>
            <n-form-item label="effort" v-if="!isMain">
              <n-select v-model:value="editing.effort" :options="effortOptions" />
            </n-form-item>
            <n-form-item label="权限模式" v-if="!isMain">
              <n-select v-model:value="editing.permission_mode" :options="permModeOptions" />
            </n-form-item>
            <n-form-item label="记忆隔离" v-if="!isMain">
              <n-select v-model:value="editing.memory_scope" :options="memScopeOptions" />
            </n-form-item>
            <n-form-item label="voice_ref" v-if="!isMain">
              <n-input v-model:value="editing.voice_ref" placeholder="TTS 音色（nahida / keli），自动朗读时使用" />
            </n-form-item>
            <n-form-item label="背景板">
              <div class="wallpaper-field">
                <div class="wallpaper-row">
                  <n-input v-model:value="editing.wallpaper"
                           placeholder="图片 URL（/assets/... 或 https://...），留空用默认" />
                  <n-button v-if="!isCreate" :loading="uploadingWp" @click="wpInput?.click()">
                    上传图片
                  </n-button>
                  <input ref="wpInput" type="file" accept="image/png,image/jpeg,image/webp"
                         style="display: none" @change="pickWallpaper" />
                </div>
                <div v-if="editing.wallpaper" class="wallpaper-preview"
                     :style="{ backgroundImage: `url('${editing.wallpaper}')` }" />
                <span v-else class="wallpaper-hint">该 Agent 接管对话时聊天背景会平滑切换为此图</span>
              </div>
            </n-form-item>
          </n-form>
        </n-tab-pane>

        <n-tab-pane name="perm" tab="权限矩阵" v-if="!isCreate && !permissions.is_main">
          <div class="perm-toolbar">
            <span class="perm-hint">改动暂存，点「应用」一次写入，写完即生效（含 QQ 通道）</span>
            <n-button size="small" type="primary" :disabled="!permDirty" @click="applyPermissions">
              应用权限变更
            </n-button>
          </div>
          <div v-for="[cat, group] in toolGroups" :key="cat" class="perm-group">
            <div class="perm-group-head">
              <span>{{ cat }}</span>
              <span class="group-ops">
                <n-button size="tiny" quaternary @click="groupSetAll(group, true)">全开</n-button>
                <n-button size="tiny" quaternary @click="groupSetAll(group, false)">全关</n-button>
              </span>
            </div>
            <div class="perm-rows">
              <div v-for="[name, info] in group" :key="name" class="perm-row">
                <span class="perm-name" :title="name">{{ name }}</span>
                <span v-if="info.locked" class="perm-lock" :title="info.reason">🔒</span>
                <n-switch v-else size="small" :value="info.enabled"
                          @update:value="(v: boolean) => togglePerm(name, v)" />
              </div>
            </div>
          </div>
          <div v-if="Object.keys(permissions.mcp_servers || {}).length" class="perm-group">
            <div class="perm-group-head"><span>🔌 MCP 服务</span></div>
            <div class="perm-rows">
              <div v-for="(info, name) in permissions.mcp_servers" :key="name" class="perm-row">
                <span class="perm-name">{{ name }}</span>
                <n-switch size="small" :value="info.enabled" :disabled="info.locked"
                          @update:value="(v: boolean) => toggleMcpPerm(String(name), v)" />
              </div>
            </div>
          </div>
        </n-tab-pane>

        <n-tab-pane name="personality" tab="人格设定">
          <n-input v-model:value="personality" type="textarea" :rows="14"
                   placeholder="Markdown 人格全文（保存时写入 *_personality.md 并热重载）" />
        </n-tab-pane>

        <n-tab-pane name="test" tab="测试" v-if="!isCreate">
          <n-button :loading="testing" @click="runTest">对 {{ editing.display_name }} 发送测试语句</n-button>
          <div v-if="testResult" class="test-result glass-panel"
               :class="{ failed: !testResult.ok }">
            <div>{{ testResult.ok ? '✓ 通过' : '✗ 失败' }} · {{ testResult.elapsed_ms }}ms</div>
            <div class="test-reply">{{ testResult.reply || testResult.error }}</div>
          </div>
        </n-tab-pane>
      </n-tabs>

      <template #footer>
        <div class="modal-footer">
          <n-button @click="showEditor = false">取消</n-button>
          <n-button type="primary" :loading="saving" @click="save">
            {{ isCreate ? '创建（即时生效）' : '保存（即时生效）' }}
          </n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.view-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.view-header h2 { color: var(--moon); font-family: 'Noto Serif SC', serif; }

.agent-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
}

.agent-card { padding: 14px 16px; cursor: pointer; }

.card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }

.agent-avatar {
  width: 40px; height: 40px;
  border-radius: 50%;
  background: rgba(127, 214, 80, 0.18) center/cover no-repeat;
  border: 1px solid var(--glass-border);
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; font-weight: 700; color: var(--dendro);
  flex-shrink: 0;
}

.agent-names { display: flex; flex-direction: column; flex: 1; min-width: 0; }
.agent-display { font-weight: 600; }
.agent-id { font-size: 11px; color: var(--moon-dim); font-family: 'JetBrains Mono', monospace; }

.card-meta { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.card-stats { font-size: 12px; color: var(--moon-dim); margin-bottom: 6px; }
.card-desc {
  font-size: 12px; color: var(--moon-dim);
  overflow: hidden; text-overflow: ellipsis;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  min-height: 32px;
}
.card-actions { margin-top: 8px; text-align: right; }

.perm-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 12px; gap: 12px;
}
.perm-hint { font-size: 12px; color: var(--wisdom); }

.perm-group { margin-bottom: 14px; }
.perm-group-head {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 13px; color: var(--dendro); font-weight: 600;
  padding-bottom: 4px; border-bottom: 1px solid var(--glass-border);
  margin-bottom: 6px;
}
.group-ops { display: flex; gap: 4px; }

.perm-rows {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 4px 16px;
}

.perm-row {
  display: flex; align-items: center; gap: 8px;
  padding: 3px 6px; border-radius: 6px;
}
.perm-row:hover { background: rgba(127, 214, 80, 0.06); }
.perm-name {
  flex: 1; font-size: 12.5px;
  font-family: 'JetBrains Mono', monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.perm-lock { cursor: help; }

.test-result {
  margin-top: 12px; padding: 12px 14px; font-size: 13px;
  border-color: rgba(127, 214, 80, 0.4);
}
.test-result.failed { border-color: var(--alert); }
.test-reply { margin-top: 6px; color: var(--moon-dim); white-space: pre-wrap; }

.wallpaper-field { display: flex; flex-direction: column; gap: 8px; width: 100%; }
.wallpaper-row { display: flex; gap: 8px; }
.wallpaper-preview {
  height: 90px;
  border-radius: 10px;
  background: center/cover no-repeat;
  border: 1px solid var(--glass-border);
}
.wallpaper-hint { font-size: 12px; color: var(--moon-dim); }

.modal-footer { display: flex; justify-content: flex-end; gap: 10px; }
</style>
