<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import {
  NButton, NModal, NForm, NFormItem, NInput, NTag, NPopconfirm,
  NDynamicInput, NSwitch, NSpace, useMessage, NTabs, NTabPane, NEmpty, NTooltip,
} from 'naive-ui'
import { get, post, put, del } from '../api'

const message = useMessage()
const servers = ref<any[]>([])
const showForm = ref(false)
const isCreate = ref(true)
const form = ref<any>({})
const busy = ref('')
const showImport = ref(false)
const importJson = ref('')
const importing = ref(false)

const IMPORT_PLACEHOLDER = `支持标准 mcpServers 格式，直接从文档复制粘贴：
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
      "env": { "KEY": "value" }
    }
  }
}`

async function runImport() {
  let parsed: any
  try {
    parsed = JSON.parse(importJson.value)
  } catch {
    message.error('JSON 解析失败，请检查格式')
    return
  }
  // 兼容 {mcpServers: {...}} / 裸 {name: {command...}} / 单个 {name, command}
  let entries: Array<[string, any]>
  if (parsed.mcpServers && typeof parsed.mcpServers === 'object') {
    entries = Object.entries(parsed.mcpServers)
  } else if (parsed.command && parsed.name) {
    entries = [[parsed.name, parsed]]
  } else if (typeof parsed === 'object' && !Array.isArray(parsed)) {
    entries = Object.entries(parsed)
  } else {
    message.error('未识别的结构：需要 mcpServers 对象或 {name: {command, ...}}')
    return
  }
  const bad = entries.find(([, v]) => !v || typeof v.command !== 'string')
  if (bad) {
    message.error(`"${bad[0]}" 缺少 command 字段`)
    return
  }
  importing.value = true
  let ok = 0
  const errors: string[] = []
  for (const [name, v] of entries) {
    try {
      await post('/mcp/servers', {
        name,
        command: v.command,
        args: (v.args || []).map(String),
        env: v.env || {},
      })
      ok++
    } catch (e: any) {
      errors.push(`${name}: ${e.message}`)
    }
  }
  importing.value = false
  if (ok) message.success(`成功导入 ${ok} 个 MCP server ✓`)
  for (const err of errors) message.error(err)
  if (ok && !errors.length) {
    showImport.value = false
    importJson.value = ''
  }
  await load()
}

onMounted(load)

async function load() {
  try {
    servers.value = await get<any[]>('/mcp/servers')
  } catch (e: any) {
    message.error(e.message)
  }
}

function openForm(server: any | null) {
  isCreate.value = !server
  form.value = server
    ? {
        name: server.name,
        command: server.command,
        args: [...(server.args || [])],
        env: Object.entries(server.env_keys || []).map(() => ({ key: '', value: '' })),
      }
    : { name: '', command: '', args: [], env: [] }
  showForm.value = true
}

async function save() {
  const body = {
    name: form.value.name,
    command: form.value.command,
    args: (form.value.args || []).filter((a: string) => a),
    env: Object.fromEntries(
      (form.value.env || []).filter((e: any) => e?.key).map((e: any) => [e.key, e.value])),
  }
  try {
    if (isCreate.value) {
      const data = await post('/mcp/servers', body)
      message.success(data.status === 'running'
        ? `已启动，发现 ${data.tool_names.length} 个工具 ✓`
        : `已保存但启动失败：${data.last_error}`)
    } else {
      await put(`/mcp/servers/${form.value.name}`, body)
      message.success('已更新并重启 ✓')
    }
    showForm.value = false
    await load()
  } catch (e: any) {
    message.error(e.message)
  }
}

async function lifecycle(name: string, action: 'start' | 'stop' | 'restart') {
  busy.value = `${name}:${action}`
  try {
    await post(`/mcp/servers/${name}/${action}`)
    message.success(`${name} ${action} 完成 ✓`)
    await load()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    busy.value = ''
  }
}

async function remove(name: string) {
  try {
    await del(`/mcp/servers/${name}`, true)
    message.success('已删除')
    await load()
  } catch (e: any) {
    message.error(e.message)
  }
}

const statusType: Record<string, any> = { running: 'success', stopped: 'default', error: 'error' }

// ── 模板功能 ──────────────────────────────────────────────
const showTemplates = ref(false)
const TEMPLATES = [
  { name: 'filesystem', command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem', '选择目录'], desc: '文件系统读写' },
  { name: 'fetch', command: 'npx', args: ['-y', '@modelcontextprotocol/server-fetch'], desc: 'HTTP 请求抓取网页' },
  { name: 'memory', command: 'npx', args: ['-y', '@modelcontextprotocol/server-memory'], desc: '知识图谱记忆' },
  { name: 'brave-search', command: 'npx', args: ['-y', '@modelcontextprotocol/server-brave-search'], desc: 'Brave 搜索（需 BRAVE_API_KEY）', env: { BRAVE_API_KEY: '' } },
  { name: 'sqlite', command: 'uvx', args: ['mcp-server-sqlite', '--db-path', '选择数据库路径'], desc: 'SQLite 数据库' },
  { name: 'github', command: 'npx', args: ['-y', '@modelcontextprotocol/server-github'], desc: 'GitHub 操作（需 GITHUB_TOKEN）', env: { GITHUB_TOKEN: '' } },
]

async function applyTemplate(tpl: typeof TEMPLATES[number]) {
  try {
    await post('/mcp/servers', {
      name: tpl.name,
      command: tpl.command,
      args: tpl.args,
      env: tpl.env || {},
    })
    message.success(`模板「${tpl.name}」已创建并启动 ✓`)
    showTemplates.value = false
    await load()
  } catch (e: any) {
    message.error(e.message)
  }
}

// ── 健康检查 ──────────────────────────────────────────────
const healthMap = ref<Record<string, string>>({})

async function checkHealth(name: string) {
  try {
    const res = await get<any>(`/mcp/servers/${name}/health`)
    healthMap.value[name] = res.connected ? 'healthy' : 'unhealthy'
  } catch {
    healthMap.value[name] = 'unhealthy'
  }
}

function healthDotColor(name: string, serverStatus: string) {
  if (serverStatus !== 'running') return 'var(--moon-dim)'
  const h = healthMap.value[name]
  if (h === 'healthy') return '#7fd650'
  if (h === 'unhealthy') return '#d96a5f'
  return '#e8d5a3' // unknown
}

// ── 工具级开关 ──────────────────────────────────────────────
async function toggleTool(serverName: string, toolName: string, enabled: boolean) {
  try {
    await put(`/mcp/servers/${serverName}/tools/${toolName}/enabled`, { enabled })
    message.success(`${toolName} 已${enabled ? '启用' : '禁用'} ✓`)
    await load()
  } catch (e: any) {
    message.error(e.message)
  }
}

// ── MCP 市场 ────────────────────────────────────────
const mcpTab = ref('installed')
const mcpItems = ref<any[]>([])
const mcpLoading = ref(false)
const mcpSearch = ref('')
const installingMcp = ref<Record<string, boolean>>({})
const uninstallingMcp = ref<Record<string, boolean>>({})

async function loadMcpMarket() {
  if (mcpItems.value.length > 0) return
  mcpLoading.value = true
  try {
    mcpItems.value = await get<any[]>('/market/mcp')
  } catch (e: any) {
    message.error(`MCP 市场加载失败: ${e.message}`)
  } finally {
    mcpLoading.value = false
  }
}

async function installFromMcp(item: any) {
  installingMcp.value[item.name] = true
  try {
    const result = await post('/market/mcp/install', item)
    message.success(`${item.name} 安装成功`)
  } catch (e: any) {
    message.error(`安装失败: ${e.message}`)
  } finally {
    installingMcp.value[item.name] = false
  }
}

async function uninstallFromMcp(item: any) {
  uninstallingMcp.value[item.name] = true
  try {
    await post('/market/mcp/uninstall', { name: item.name })
    message.success(`${item.name} 已卸载`)
  } catch (e: any) {
    message.error(`卸载失败: ${e.message}`)
  } finally {
    uninstallingMcp.value[item.name] = false
  }
}

const filteredMcpMarket = computed(() => {
  const keyword = mcpSearch.value.toLowerCase().trim()
  if (!keyword) return mcpItems.value
  return mcpItems.value.filter((item: any) =>
    item.name.toLowerCase().includes(keyword) ||
    item.description?.toLowerCase().includes(keyword)
  )
})

function onMcpTabChange(tab: string) {
  if (tab === 'market-mcp') loadMcpMarket()
}
</script>

<template>
  <div class="mcp-view">
    <n-tabs v-model:value="mcpTab" type="line" animated @update:value="onMcpTabChange">
      <n-tab-pane name="installed" tab="已安装">
        <div class="view-header">
          <h2>🔌 MCP 服务</h2>
          <div style="display:flex; gap:8px">
            <n-button @click="showTemplates = true">📦 模板</n-button>
            <n-button type="primary" @click="showImport = true">📋 粘贴 JSON 导入</n-button>
            <n-button @click="openForm(null)">＋ 手动新增</n-button>
          </div>
        </div>

        <p class="mcp-hint">
          新增 server → 启动握手 → 工具自动注册（source=mcp:名称）→ 出现在工具页与各 Agent 权限矩阵中。
        </p>

        <div class="server-grid">
          <div v-for="s in servers" :key="s.name" class="server-card glass-panel glass-panel-hover">
            <div class="server-head">
              <n-space align="center" :size="6">
                <span class="health-dot"
                      :style="{ background: healthDotColor(s.name, s.status) }"
                      :title="s.status === 'running' ? (healthMap[s.name] || '未检查') : s.status"
                      @click="s.status === 'running' && checkHealth(s.name)"></span>
                <span class="server-name">{{ s.name }}</span>
              </n-space>
              <n-tag size="small" :type="statusType[s.status]" :bordered="false">{{ s.status }}</n-tag>
            </div>
            <div class="server-cmd mono">{{ s.command }} {{ (s.args || []).join(' ') }}</div>
            <div v-if="s.last_error" class="server-error">{{ s.last_error }}</div>
            <div class="server-tools">
              <div v-for="t in (s.tool_names || []).slice(0, 8)" :key="t" class="tool-toggle">
                <n-switch size="small" :value="!(s.disabled_tools || []).includes(t)"
                          @update:value="(v: boolean) => toggleTool(s.name, t, v)" />
                <n-tag size="tiny" :bordered="false" :type="(s.disabled_tools || []).includes(t) ? 'default' : 'success'">{{ t }}</n-tag>
              </div>
              <span v-if="(s.tool_names || []).length > 8" class="more">
                +{{ s.tool_names.length - 8 }}
              </span>
              <span v-if="!s.tool_names?.length" class="more">（未发现工具）</span>
            </div>
            <div class="server-ops">
              <n-button v-if="s.status !== 'running'" size="tiny" type="primary" secondary
                        :loading="busy === `${s.name}:start`" @click="lifecycle(s.name, 'start')">启动</n-button>
              <n-button v-else size="tiny" :loading="busy === `${s.name}:stop`"
                        @click="lifecycle(s.name, 'stop')">停止</n-button>
              <n-button size="tiny" :loading="busy === `${s.name}:restart`"
                        @click="lifecycle(s.name, 'restart')">重启</n-button>
              <n-button v-if="s.managed_by_webui" size="tiny" @click="openForm(s)">编辑</n-button>
              <n-popconfirm v-if="s.managed_by_webui" @positive-click="remove(s.name)">
                <template #trigger><n-button size="tiny" type="error" quaternary>删</n-button></template>
                删除前会先停止该 server，确认？
              </n-popconfirm>
            </div>
          </div>
          <div v-if="!servers.length" class="empty-state glass-panel">
            <p>还没有接入 MCP 服务哦～点右上角「新增」试试（例如 filesystem server）</p>
          </div>
        </div>

        <n-modal v-model:show="showForm" preset="card"
                 :title="isCreate ? '新增 MCP Server' : `编辑 · ${form.name}`"
                 style="width: min(580px, 94vw)">
          <n-form label-placement="left" label-width="90">
            <n-form-item label="name" v-if="isCreate">
              <n-input v-model:value="form.name" placeholder="如 filesystem" />
            </n-form-item>
            <n-form-item label="command">
              <n-input v-model:value="form.command" placeholder="如 /usr/bin/npx 或 uvx" />
            </n-form-item>
            <n-form-item label="args">
              <n-dynamic-input v-model:value="form.args" placeholder="逐行一个参数" />
            </n-form-item>
            <n-form-item label="env">
              <n-dynamic-input v-model:value="form.env" preset="pair"
                               key-placeholder="变量名" value-placeholder="值（保密处理）" />
            </n-form-item>
          </n-form>
          <template #footer>
            <div style="display:flex; justify-content:flex-end; gap:10px">
              <n-button @click="showForm = false">取消</n-button>
              <n-button type="primary" @click="save">保存并启动</n-button>
            </div>
          </template>
        </n-modal>

        <n-modal v-model:show="showImport" preset="card" title="粘贴 JSON 导入 MCP Server"
                 style="width: min(640px, 94vw)">
          <n-input v-model:value="importJson" type="textarea" :rows="14"
                   class="mono" :placeholder="IMPORT_PLACEHOLDER" />
          <template #footer>
            <div style="display:flex; justify-content:flex-end; gap:10px">
              <n-button @click="showImport = false">取消</n-button>
              <n-button type="primary" :loading="importing" :disabled="!importJson.trim()"
                        @click="runImport">导入并启动</n-button>
            </div>
          </template>
        </n-modal>

        <n-modal v-model:show="showTemplates" preset="card" title="📦 MCP Server 模板"
                 style="width: min(580px, 94vw)">
          <p style="font-size:13px; color:var(--moon-dim); margin-bottom:12px">
            选择预设模板，一键创建并启动 MCP Server。部分模板需要配置环境变量。
          </p>
          <div class="template-list">
            <div v-for="tpl in TEMPLATES" :key="tpl.name" class="template-item glass-panel">
              <div class="tpl-info">
                <span class="tpl-name">{{ tpl.name }}</span>
                <span class="tpl-desc">{{ tpl.desc }}</span>
                <span class="tpl-cmd mono">{{ tpl.command }} {{ tpl.args.join(' ') }}</span>
              </div>
              <n-button size="tiny" type="primary" @click="applyTemplate(tpl)">一键创建</n-button>
            </div>
          </div>
        </n-modal>
      </n-tab-pane>

      <n-tab-pane name="market-mcp" tab="MCP 市场">
        <div class="market-toolbar" style="margin-bottom: 16px;">
          <n-input v-model:value="mcpSearch" placeholder="搜索 MCP 服务..." clearable style="max-width: 400px" />
        </div>
        <n-spin :show="mcpLoading">
          <div class="mcp-grid">
            <div v-for="item in filteredMcpMarket" :key="item.id" class="market-card glass-panel glass-panel-hover">
              <div class="card-header">
                <img v-if="item.icon" :src="item.icon" :alt="item.name" class="card-icon" />
                <div v-else class="card-icon-placeholder">🔌</div>
                <div class="card-title-area">
                  <h4 class="card-title">{{ item.name }}</h4>
                  <span class="card-author">{{ item.author || '未知' }}</span>
                </div>
              </div>
              <p class="card-desc">{{ item.description || '暂无描述' }}</p>
              <div class="card-footer">
                <span class="card-downloads">{{ item.use_count ?? 0 }} 次使用</span>
                <div style="display: flex; gap: 6px;">
                  <n-button size="small" type="error" secondary
                    :loading="uninstallingMcp[item.name]"
                    @click="uninstallFromMcp(item)">
                    卸载
                  </n-button>
                  <n-button size="small" type="success" secondary
                    :loading="installingMcp[item.name]"
                    @click="installFromMcp(item)">
                    安装
                  </n-button>
                </div>
              </div>
            </div>
          </div>
          <n-empty v-if="!mcpLoading && filteredMcpMarket.length === 0"
            :description="mcpSearch ? '未找到匹配的 MCP 服务' : '暂无可用的 MCP 服务'" />
        </n-spin>
      </n-tab-pane>
    </n-tabs>
  </div>
</template>

<style scoped>
.view-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.view-header h2 { font-family: 'Noto Serif SC', serif; }
.mcp-hint { font-size: 12.5px; color: var(--moon-dim); margin-bottom: 14px; }

.server-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
}

.server-card { padding: 14px 16px; }
.server-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.server-name { font-weight: 600; font-size: 15px; }

.server-cmd {
  font-size: 12px; color: var(--moon-dim);
  word-break: break-all; margin-bottom: 8px;
}
.mono { font-family: 'JetBrains Mono', monospace; }

.server-error {
  font-size: 12px; color: var(--alert);
  background: rgba(217, 106, 95, 0.08);
  border-radius: 6px; padding: 4px 8px; margin-bottom: 8px;
}

.server-tools { display: flex; flex-wrap: wrap; gap: 4px; margin-bottom: 10px; min-height: 22px; align-items: center; }
.tool-toggle { display: flex; align-items: center; gap: 3px; }
.more { font-size: 11px; color: var(--moon-dim); }

.health-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  cursor: pointer; transition: background 0.2s;
}

.template-list { display: flex; flex-direction: column; gap: 10px; }
.template-item {
  display: flex; align-items: center; justify-content: space-between;
  padding: 10px 14px; gap: 12px;
}
.tpl-info { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.tpl-name { font-weight: 600; font-size: 14px; }
.tpl-desc { font-size: 12px; color: var(--moon-dim); }
.tpl-cmd { font-size: 11px; color: var(--moon-dim); word-break: break-all; }

.server-ops { display: flex; gap: 6px; flex-wrap: wrap; }

.empty-state { padding: 40px; text-align: center; color: var(--moon-dim); grid-column: 1 / -1; }

.mcp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
  margin-bottom: 16px;
}
.market-card { padding: 14px; }
.card-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.card-icon { width: 36px; height: 36px; border-radius: 6px; object-fit: cover; }
.card-icon-placeholder { width: 36px; height: 36px; border-radius: 6px; background: rgba(232,213,163,0.1); display: flex; align-items: center; justify-content: center; font-size: 20px; }
.card-title-area { min-width: 0; flex: 1; }
.card-title { font-size: 15px; font-weight: 600; margin: 0 0 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.card-author { font-size: 12px; color: var(--moon-dim); }
.card-desc { font-size: 13px; color: var(--moon-secondary, #aaa); margin: 0 0 10px; line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.card-footer { display: flex; align-items: center; justify-content: space-between; }
.card-downloads { font-size: 12px; color: var(--moon-dim); }
</style>
