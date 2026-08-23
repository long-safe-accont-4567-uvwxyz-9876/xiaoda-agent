<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import SumeruIcon from '../components/fx/SumeruIcon.vue'
import ViewTitleIcon from '../components/fx/ViewTitleIcon.vue'
import {
  NButton, NModal, NForm, NFormItem, NInput, NTag, NPopconfirm,
  NDynamicInput, NSwitch, NSpace, NSpin, useMessage, NTabs, NTabPane, NEmpty, NTooltip,
} from 'naive-ui'
import { get, post, put, del } from '../api'
import { t, tf } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'

const message = useMessage()
const servers = ref<any[]>([])
const showForm = ref(false)
const isCreate = ref(true)
const form = ref<any>({})
const busy = ref('')
const showImport = ref(false)
const importJson = ref('')
const importing = ref(false)

const IMPORT_PLACEHOLDER = computed(() => `${t('mcpView.importPlaceholder')}
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
      "env": { "KEY": "value" }
    }
  }
}`)

async function runImport() {
  let parsed: any
  try {
    parsed = JSON.parse(importJson.value)
  } catch {
    message.error(t('mcpView.jsonParseFailed'))
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
    message.error(t('mcpView.unrecognizedStruct'))
    return
  }
  const bad = entries.find(([, v]) => !v || typeof v.command !== 'string')
  if (bad) {
    message.error(tf('mcpView.missingCommand', bad[0]))
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
  if (ok) message.success(tf('mcpView.importSuccess', ok))
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
      const data = await post<{ status: string; tool_names: string[]; last_error?: string }>('/mcp/servers', body)
      message.success(data.status === 'running'
        ? tf('mcpView.startedWithTools', data.tool_names.length)
        : tf('mcpView.savedButFailed', data.last_error))
    } else {
      await put(`/mcp/servers/${form.value.name}`, body)
      message.success(t('mcpView.updatedRestarted'))
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
    message.success(tf('mcpView.lifecycleDone', name, action))
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
    message.success(t('mcpView.deleted'))
    await load()
  } catch (e: any) {
    message.error(e.message)
  }
}

const statusType: Record<string, any> = { running: 'success', stopped: 'default', error: 'error' }

// ── 模板功能 ──────────────────────────────────────────────
const showTemplates = ref(false)
const backendTemplates = ref<any[]>([])
const templatesLoading = ref(false)
const TEMPLATES = computed(() => {
  if (backendTemplates.value.length) return backendTemplates.value
  return [
    { name: 'filesystem', command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem', t('mcpView.selectDir')], desc: t('mcpView.templateFs') },
    { name: 'fetch', command: 'npx', args: ['-y', '@modelcontextprotocol/server-fetch'], desc: t('mcpView.templateHttp') },
    { name: 'memory', command: 'npx', args: ['-y', '@modelcontextprotocol/server-memory'], desc: t('mcpView.templateKg') },
    { name: 'brave-search', command: 'npx', args: ['-y', '@modelcontextprotocol/server-brave-search'], desc: t('mcpView.templateBrave'), env: { BRAVE_API_KEY: '' } },
    { name: 'sqlite', command: 'uvx', args: ['mcp-server-sqlite', '--db-path', t('mcpView.selectDb')], desc: t('mcpView.templateSqlite') },
    { name: 'github', command: 'npx', args: ['-y', '@modelcontextprotocol/server-github'], desc: t('mcpView.templateGithub'), env: { GITHUB_TOKEN: '' } },
  ]
})

async function loadBackendTemplates() {
  templatesLoading.value = true
  try {
    const data = await get<any[]>('/mcp/templates')
    if (data?.length) backendTemplates.value = data
  } catch { /* fallback to hardcoded */ } finally {
    templatesLoading.value = false
  }
}

function openTemplates() {
  showTemplates.value = true
  loadBackendTemplates()
}

async function applyTemplate(tpl: any) {
  try {
    await post('/mcp/servers', {
      name: tpl.name,
      command: tpl.command,
      args: tpl.args,
      env: tpl.env || {},
    })
    message.success(tf('mcpView.templateCreated', tpl.name))
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
    message.success(tf('mcpView.toolToggled', toolName, enabled))
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

// 仅本地资源（相对路径 / data URI）走 <img> 加载，外链 http(s) 图标直接用首字母 fallback，
// 避免外站不可达时浏览器打出大量 net::ERR_* 控制台错误
function isLocalIcon(icon: any): boolean {
  if (typeof icon !== 'string' || !icon) return false
  return icon.startsWith('/') || icon.startsWith('data:') || icon.startsWith('assets/')
}

async function loadMcpMarket() {
  if (mcpItems.value.length > 0) return
  mcpLoading.value = true
  try {
    const data = await get<any>('/market/mcp')
    mcpItems.value = data.items || []
  } catch (e: any) {
    message.error(tf('mcpView.marketLoadFailed', e.message))
  } finally {
    mcpLoading.value = false
  }
}

async function installFromMcp(item: any) {
  installingMcp.value[item.name] = true
  try {
    const result = await post('/market/mcp/install', item)
    message.success(tf('mcpView.installSuccess', item.name))
  } catch (e: any) {
    message.error(tf('mcpView.installFailed', e.message))
  } finally {
    installingMcp.value[item.name] = false
  }
}

async function uninstallFromMcp(item: any) {
  uninstallingMcp.value[item.name] = true
  try {
    await post('/market/mcp/uninstall', { name: item.name })
    message.success(tf('mcpView.uninstalled', item.name))
  } catch (e: any) {
    message.error(tf('mcpView.uninstallFailed', e.message))
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
    <div class="page-header">
      <h2 class="view-title-icon"><ViewTitleIcon name="mcp" /> {{ t('mcpView.title') }}</h2>
      <div class="page-actions">
        <n-button @click="openTemplates">📦 {{ t('mcpView.template') }}</n-button>
        <n-button type="primary" @click="showImport = true">📋 {{ t('mcpView.importJson') }}</n-button>
        <n-button @click="openForm(null)"><SumeruIcon name="plus" :size="14" variant="duo" tone="add" interactive /> {{ t('mcpView.addManual') }}</n-button>
      </div>
    </div>

    <n-tabs v-model:value="mcpTab" type="line" animated @update:value="onMcpTabChange">
      <n-tab-pane name="installed" :tab="t('installed')">
        <p class="mcp-hint">
          {{ t('mcpView.addServerHint') }}
        </p>

        <div class="server-grid">
          <Tilt3D v-for="s in servers" :key="s.name">
          <div class="server-card glass-panel glass-panel-hover">
            <div class="server-head">
              <n-space align="center" :size="6">
                <span class="health-dot"
                      :style="{ background: healthDotColor(s.name, s.status) }"
                      :title="s.status === 'running' ? (healthMap[s.name] || t('mcpView.notChecked')) : s.status"
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
                <span class="tool-name" :class="{ disabled: (s.disabled_tools || []).includes(t) }">{{ t }}</span>
              </div>
              <span v-if="(s.tool_names || []).length > 8" class="more">
                +{{ s.tool_names.length - 8 }}
              </span>
              <span v-if="!s.tool_names?.length" class="more">（{{ t('mcpView.noTools') }}）</span>
            </div>
            <div class="server-ops">
              <n-button v-if="s.status !== 'running'" size="tiny" type="primary" secondary
                        :loading="busy === `${s.name}:start`" @click="lifecycle(s.name, 'start')">{{ t('mcpView.start') }}</n-button>
              <n-button v-else size="tiny" :loading="busy === `${s.name}:stop`"
                        @click="lifecycle(s.name, 'stop')">{{ t('mcpView.stop') }}</n-button>
              <n-button size="tiny" :loading="busy === `${s.name}:restart`"
                        @click="lifecycle(s.name, 'restart')">{{ t('mcpView.restart') }}</n-button>
              <n-button v-if="s.managed_by_webui" size="tiny" @click="openForm(s)">{{ t('mcpView.edit') }}</n-button>
              <n-popconfirm v-if="s.managed_by_webui" @positive-click="remove(s.name)">
                <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('mcpView.delete') }}</n-button></template>
                {{ t('mcpView.confirmDelete') }}
              </n-popconfirm>
            </div>
          </div>
          </Tilt3D>
          <Tilt3D v-if="!servers.length" :max-x="4" :max-y="6" class="empty-state glass-panel">
            <p>{{ t('mcpView.noMcp') }}</p>
          </Tilt3D>
        </div>

        <n-modal v-model:show="showForm" preset="card"
                 :title="isCreate ? t('mcpView.newServer') : `${t('mcpView.editServer')} · ${form.name}`"
                 style="width: min(580px, 94vw)">
          <n-form class="mcp-form" label-placement="left" label-width="90">
            <n-form-item label="name" v-if="isCreate">
              <n-input v-model:value="form.name" :placeholder="t('mcpView.serverNamePh')" />
            </n-form-item>
            <n-form-item label="command">
              <n-input v-model:value="form.command" :placeholder="t('mcpView.commandPh')" />
            </n-form-item>
            <n-form-item label="args">
              <n-dynamic-input v-model:value="form.args" :placeholder="t('mcpView.argsPh')" />
            </n-form-item>
            <n-form-item label="env">
              <n-dynamic-input v-model:value="form.env" preset="pair"
                               :key-placeholder="t('mcpView.envKeyPh')" :value-placeholder="t('mcpView.envValuePh')" />
            </n-form-item>
          </n-form>
          <template #footer>
            <div class="modal-actions">
              <n-button @click="showForm = false">{{ t('cancel') }}</n-button>
              <n-button type="primary" @click="save">{{ t('mcpView.saveStart') }}</n-button>
            </div>
          </template>
        </n-modal>

        <n-modal v-model:show="showImport" preset="card" :title="t('mcpView.importTitle')"
                 style="width: min(640px, 94vw)">
          <n-input v-model:value="importJson" type="textarea" :rows="14"
                   class="mono" :placeholder="IMPORT_PLACEHOLDER" />
          <template #footer>
            <div class="modal-actions">
              <n-button @click="showImport = false">{{ t('cancel') }}</n-button>
              <n-button type="primary" :loading="importing" :disabled="!importJson.trim()"
                        @click="runImport">{{ t('mcpView.importStart') }}</n-button>
            </div>
          </template>
        </n-modal>

        <n-modal v-model:show="showTemplates" preset="card" :title="t('mcpView.templateTitle')"
                 style="width: min(580px, 94vw)">
          <p class="template-intro">
            {{ t('mcpView.templateDesc') }}
          </p>
          <div class="template-list">
            <div v-for="tpl in TEMPLATES" :key="tpl.name" class="template-item">
              <div class="tpl-info">
                <span class="tpl-name">{{ tpl.name }}</span>
                <span class="tpl-desc">{{ tpl.desc }}</span>
                <span class="tpl-cmd mono">{{ tpl.command }} {{ tpl.args.join(' ') }}</span>
              </div>
              <n-button size="tiny" type="primary" @click="applyTemplate(tpl)">{{ t('mcpView.createOne') }}</n-button>
            </div>
          </div>
        </n-modal>
      </n-tab-pane>

      <n-tab-pane name="market-mcp" :tab="t('mcpView.market')">
        <div class="market-toolbar">
          <n-input v-model:value="mcpSearch" :placeholder="t('mcpView.marketSearchPlaceholder')" clearable class="market-search" />
        </div>
        <n-spin :show="mcpLoading">
          <div class="mcp-grid">
            <Tilt3D v-for="item in filteredMcpMarket" :key="item.id">
            <div class="market-card glass-panel glass-panel-hover">
              <div class="card-header">
                <img v-if="isLocalIcon(item.icon) && !item._iconErr" :src="item.icon" :alt="item.name" class="card-icon" @error="item._iconErr=true" />
                <div v-else class="card-icon-placeholder">{{ (item.name||'?')[0].toUpperCase() }}</div>
                <div class="card-title-area">
                  <h4 class="card-title">{{ item.name }}</h4>
                  <span class="card-author">{{ item.author || t('mcpView.unknown') }}</span>
                </div>
              </div>
              <p class="card-desc">{{ item.description || t('mcpView.noDesc') }}</p>
              <div class="card-footer">
                <span class="card-downloads">{{ item.use_count ?? 0 }} {{ t('mcpView.uses') }}</span>
                <div class="card-actions">
                  <n-button size="small" type="error" secondary
                    :loading="uninstallingMcp[item.name]"
                    @click="uninstallFromMcp(item)">
                    {{ t('uninstall') }}
                  </n-button>
                  <n-button size="small" type="success" secondary
                    :loading="installingMcp[item.name]"
                    @click="installFromMcp(item)">
                    {{ t('install') }}
                  </n-button>
                </div>
              </div>
            </div>
            </Tilt3D>
          </div>
          <n-empty v-if="!mcpLoading && filteredMcpMarket.length === 0"
            :description="mcpSearch ? t('mcpView.marketEmptyMatched') : t('mcpView.marketEmpty')" />
        </n-spin>
      </n-tab-pane>
    </n-tabs>
  </div>
</template>

<style scoped>
.page-header h2 { margin: 0; font-family: 'Noto Serif SC', serif; }
.mcp-hint { font-size: 12.5px; color: var(--moon-dim); margin-bottom: 14px; }

.server-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr));
  gap: 14px;
}

.server-card { min-width: 0; padding: 14px 16px; }
.server-head { display: flex; min-width: 0; align-items: flex-start; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
.server-head :deep(.n-space) { min-width: 0; }
.server-head :deep(.n-tag) { flex-shrink: 0; }
.server-name { min-width: 0; font-weight: 600; font-size: 15px; overflow-wrap: anywhere; word-break: break-word; }

.server-cmd {
  min-width: 0;
  margin-bottom: 8px;
  color: var(--moon-dim);
  font-size: 12px;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.mono { font-family: 'JetBrains Mono', monospace; }

.server-error {
  min-width: 0;
  margin-bottom: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  background: rgba(217, 106, 95, 0.08);
  color: var(--alert);
  font-size: 12px;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.server-tools { display: flex; min-width: 0; min-height: 24px; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.tool-toggle {
  display: inline-flex;
  min-width: 0;
  max-width: 100%;
  align-items: center;
  gap: 5px;
  padding: 3px 7px;
  border: 1px solid var(--glass-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.025);
}
.tool-toggle :deep(.n-switch) { flex-shrink: 0; }
.tool-name { min-width: 0; color: var(--moon); font-size: 11px; line-height: 1.35; overflow-wrap: anywhere; word-break: break-word; }
.tool-name.disabled { color: var(--moon-dim); }
.more { font-size: 11px; color: var(--moon-dim); }

.health-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  cursor: pointer; transition: background 0.2s;
}

.template-intro { margin: 0 0 8px; color: var(--moon-dim); font-size: 13px; }
.template-list { display: flex; flex-direction: column; }
.template-item {
  display: flex;
  min-width: 0;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 2px;
  border-bottom: 1px solid var(--glass-border);
}
.template-item:last-child { border-bottom: 0; }
.template-item > :deep(.n-button) { flex-shrink: 0; }
.tpl-info { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.tpl-name { font-weight: 600; font-size: 14px; overflow-wrap: anywhere; }
.tpl-desc { font-size: 12px; color: var(--moon-dim); overflow-wrap: anywhere; }
.tpl-cmd { font-size: 11px; color: var(--moon-dim); overflow-wrap: anywhere; word-break: break-word; }

.server-ops { display: flex; flex-wrap: wrap; gap: 6px; padding-top: 10px; border-top: 1px solid var(--glass-border); }
.modal-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 10px; }

.empty-state { padding: 40px; text-align: center; color: var(--moon-dim); grid-column: 1 / -1; }

.mcp-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 300px), 1fr));
  gap: 14px;
  margin-bottom: 16px;
}
.market-card { min-width: 0; padding: 14px; }
.card-header { display: flex; min-width: 0; align-items: center; gap: 10px; margin-bottom: 8px; }
.card-icon { width: 36px; height: 36px; border-radius: 6px; object-fit: cover; }
.card-icon-placeholder { display: flex; width: 36px; height: 36px; flex-shrink: 0; align-items: center; justify-content: center; border-radius: 6px; background: rgba(232,213,163,0.1); font-size: 20px; }
.card-title-area { min-width: 0; flex: 1; }
.card-title { margin: 0 0 2px; overflow: hidden; font-size: 15px; font-weight: 600; text-overflow: ellipsis; white-space: nowrap; }
.card-author { font-size: 12px; color: var(--moon-dim); overflow-wrap: anywhere; }
.card-desc { display: -webkit-box; margin: 0 0 10px; overflow: hidden; color: var(--moon-secondary, #aaa); font-size: 13px; line-height: 1.5; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.card-footer { display: flex; min-width: 0; align-items: center; justify-content: space-between; gap: 10px; }
.card-downloads { min-width: 0; font-size: 12px; color: var(--moon-dim); overflow-wrap: anywhere; }
.card-actions { display: flex; flex-shrink: 0; gap: 6px; }
.market-toolbar { margin-bottom: 16px; }
.market-search { max-width: 400px; }

@media (max-width: 600px) {
  .page-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 6px;
  }

  .page-actions > :deep(.n-button) {
    min-width: 0;
    width: 100%;
    padding-inline: 6px;
  }

  .page-actions > :deep(.n-button):last-child {
    grid-column: 1 / -1;
  }

  .mcp-form :deep(.n-form-item.n-form-item--left-labelled) {
    grid-template-areas: "label" "blank" "feedback";
    grid-template-columns: minmax(0, 100%);
    grid-template-rows: auto 1fr;
  }

  .mcp-form :deep(.n-form-item.n-form-item--left-labelled .n-form-item-label) {
    display: flex;
    width: auto !important;
    min-height: 0;
    justify-content: flex-start;
    padding: 0 0 6px !important;
    text-align: left;
  }

  .market-search { max-width: none; }
}

@media (max-width: 420px) {
  .template-item { align-items: flex-start; flex-direction: column; }
  .template-item > :deep(.n-button) { align-self: flex-end; }
  .card-footer { align-items: flex-start; flex-direction: column; }
  .card-actions { align-self: flex-end; }
}
</style>
