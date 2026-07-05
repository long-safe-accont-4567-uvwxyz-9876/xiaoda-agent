<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed, watch } from 'vue'
import {
  NButton, NSwitch, NModal, NForm, NFormItem, NInput, NInputNumber,
  NSelect, NTabs, NTabPane, NTag, NPopconfirm, NDynamicTags, NCollapse,
  NCollapseItem, NImage, NEmpty, NSpin, useMessage,
} from 'naive-ui'
import { get, post, put, del, api } from '../api'
import { useAgentsStore } from '../stores/agents'
import { getWsClient } from '../api/ws'
import { t } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'
import { replaceAgentNames, refreshAgentNames } from '../utils/agentNames'
import { pinyin } from 'pinyin-pro'

const message = useMessage()

// 中文转拼音（当编辑时使用）
function translateToEn(zhName: string): string {
  if (!zhName) return ''
  const result = pinyin(zhName, { toneType: 'none', type: 'array' })
  const joined = result.join('')
  return joined.charAt(0).toUpperCase() + joined.slice(1).toLowerCase()
}
const agentsStore = useAgentsStore()
const ws = getWsClient()

const showEditor = ref(false)
const isCreate = ref(false)
const editing = ref<any>({})
const personality = ref('')
const permissions = ref<any>({ tools: {}, mcp_servers: {}, is_main: false })
const permDirty = ref(false)
const testResult = ref<any>(null)
const testing = ref(false)
const saving = ref(false)
const wpInput = ref<HTMLInputElement | null>(null)
const token = localStorage.getItem('token') || ''
const uploadingWp = ref(false)
const discoveredModels = ref<Array<{ provider: string; label?: string; models: Array<{ id: string; display_name: string; free: boolean }> }>>([])
const advancedTouched = ref(false)
const switchingModel = ref(false)

// ── 表情包管理 ──
const stickerList = ref<Array<{ name: string; description: string; emotion: string; url: string }>>([])
const stickerEmotions = ref<string[]>([])
const stickerLoading = ref(false)
const stickerUploading = ref(false)
const stickerFile = ref<File | null>(null)
const stickerDesc = ref('')
const stickerEmotion = ref('happy')
const stickerInput = ref<HTMLInputElement | null>(null)

// ── 参考音频管理 ──
const voiceGroups = ref<Record<string, Array<{ name: string; voice_ref: string }>>>({})
const voiceUploading = ref(false)
const voiceFile = ref<File | null>(null)
const voiceInputEl = ref<HTMLInputElement | null>(null)
const voiceOptions = computed(() => {
  const opts: Array<{ label: string; value: string | null }> = [{ label: t('agentsView.noVoice'), value: null }]
  const agentName = editing.value?.name
  if (agentName && voiceGroups.value[agentName]) {
    voiceGroups.value[agentName].forEach(v => {
      opts.push({ label: replaceAgentNames(v.name), value: v.voice_ref })
    })
  }
  return opts
})

/** 当前 voice_ref 的显示名（用于在 n-select 旁显示替换后的名称） */
const voiceRefDisplayName = computed(() => {
  const vr = editing.value?.voice_ref
  if (!vr) return ''
  const agentName = editing.value?.name
  if (agentName && voiceGroups.value[agentName]) {
    const found = voiceGroups.value[agentName].find(v => v.voice_ref === vr)
    if (found) return replaceAgentNames(found.name)
  }
  return replaceAgentNames(vr)
})

const createObjectURL = (f: File) => URL.createObjectURL(f)

// 自动翻译显示名为英文（当显示名变化时）
watch(() => editing.value?.display_name, (newName: string) => {
  if (newName && editing.value) {
    editing.value.display_name_en = translateToEn(newName)
  }
})

function onConfigChanged(e: any) {
  const payload = e.payload as { type?: string } | undefined
  if (payload?.type === 'chat_model') {
    loadDiscoveredModels()
  }
  // Provider 排序/增删 → 刷新模型选项列表
  if (e.domain === 'models') {
    loadDiscoveredModels()
  }
  // Agent 模型变更 → 刷新 Agent 卡片（含子 Agent 模型标签）+ 全局名称映射
  if (e.domain === 'agents') {
    agentsStore.load()
    refreshAgentNames()  // 刷新全局名称映射
  }
}

onMounted(() => {
  agentsStore.load().catch((e) => message.error(e.message))
  loadDiscoveredModels()
  ws.on('config_changed', onConfigChanged)
})

onBeforeUnmount(() => {
  ws.off('config_changed', onConfigChanged)
})

async function loadDiscoveredModels() {
  try {
    const data = await get<Array<{ provider: string; label?: string; models: Array<{ id: string; display_name: string; free: boolean }> }>>('/models/discover')
    discoveredModels.value = data || []
  } catch { /* 忽略，保留手输 */ }
}

const modelOptions = computed(() => {
  return discoveredModels.value.map(pg => ({
    type: 'group' as const,
    label: pg.provider,
    key: pg.provider,
    children: pg.models.map(m => ({
      label: `${pg.provider} - ${m.display_name}`,
      value: `${pg.provider}|${m.id}`,
    })),
  }))
})

const selectedModel = computed<string | null>({
  get: () => {
    const p = editing.value?.provider
    const m = editing.value?.model
    if (!p || !m) return null
    return `${p}|${m}`
  },
  set: () => { /* 由 onModelChange 处理 */ },
})

async function onModelChange(val: string | null) {
  if (!val) {
    editing.value.provider = ''
    editing.value.model = ''
    return
  }
  const sepIdx = val.indexOf('|')
  const provider = val.slice(0, sepIdx)
  const model_id = val.slice(sepIdx + 1)
  editing.value.provider = provider
  editing.value.model = model_id
  // 选择新模型后，后端会自动解析 base_url / api_key_env，清空本地高级配置避免覆盖
  editing.value.base_url = ''
  editing.value.api_key_env = ''
  advancedTouched.value = false

  // 仅在编辑已存在的 Agent 时即时调用后端热重载
  if (!isCreate.value && editing.value.name) {
    switchingModel.value = true
    try {
      await api.setAgentModel(editing.value.name, provider, model_id)
      message.success(t('agentsView.modelSwitched') + ` ${provider} / ${model_id} ✓`)
      await agentsStore.load()
    } catch (e: any) {
      message.error(e.message || t('agentsView.switchModelFailed'))
    } finally {
      switchingModel.value = false
    }
  }
}

function onAdvancedInput() {
  advancedTouched.value = true
}

function pickWallpaper(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 8 * 1024 * 1024) {
    message.error(t('agentsView.imgTooLarge'))
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
      message.success(t('agentsView.wallpaperUpdated'))
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
  advancedTouched.value = false
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
    loadStickers()
    loadVoices()
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
    // 仅当用户手动编辑过高级配置时才下发 base_url / api_key_env，
    // 否则保留后端通过 /agents/{name}/model 自动解析的值
    if (!advancedTouched.value) {
      delete body.base_url
      delete body.api_key_env
    }
    if (isCreate.value) {
      await post('/agents', body)
      message.success(`Agent ${editing.value.display_name || editing.value.name} ` + t('agentsView.createdActive'))
    } else {
      await put(`/agents/${editing.value.name}`, body)
      // 如果权限有改动，自动同步保存
      if (permDirty.value) {
        await applyPermissions()
      }
      message.success(t('agentsView.saved'))
    }
    showEditor.value = false
    await agentsStore.load()
    await refreshAgentNames()  // 刷新全局名称映射
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
    message.success(`${agent.display_name} ` + t(value ? 'agentsView.enabled' : 'agentsView.disabled'))
  } catch (e: any) {
    message.error(e.message)
  }
}

async function removeAgent(agent: any) {
  try {
    await del(`/agents/${agent.name}`, true)
    message.success(`${agent.display_name} ` + t('agentsView.agentDeleted'))
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
    const count = Object.values<any>(result.tools).filter(x => x.enabled).length
    message.success(`${editing.value.display_name} ` + t('agentsView.hasTools') + ` ${count} ` + t('agentsView.toolsUnit') + ' ✓ ' + t('agentsView.instantEffect'))
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

// ── 表情包管理函数 ──
async function loadStickers() {
  if (!editing.value?.name) return
  stickerLoading.value = true
  try {
    const data = await api.listStickers(editing.value.name)
    stickerList.value = data.stickers || []
    stickerEmotions.value = data.emotions || []
  } catch {
    stickerList.value = []
  } finally {
    stickerLoading.value = false
  }
}

function onStickerFilePick(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 8 * 1024 * 1024) {
    message.error(t('agentsView.imgTooLarge'))
    input.value = ''
    return
  }
  stickerFile.value = file
}

async function uploadSticker() {
  if (!stickerFile.value || !stickerDesc.value.trim()) {
    message.warning(t('agentsView.stickerWarn'))
    return
  }
  stickerUploading.value = true
  try {
    await api.uploadSticker(editing.value.name, stickerFile.value, stickerDesc.value.trim(), stickerEmotion.value)
    message.success(t('agentsView.stickerAdded'))
    stickerFile.value = null
    stickerDesc.value = ''
    if (stickerInput.value) stickerInput.value.value = ''
    await loadStickers()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    stickerUploading.value = false
  }
}

async function removeSticker(filename: string) {
  try {
    await api.deleteSticker(editing.value.name, filename)
    message.success(t('agentsView.stickerDeleted'))
    await loadStickers()
  } catch (e: any) {
    message.error(e.message)
  }
}

// ── 参考音频 ──
async function loadVoices() {
  try {
    const v = await get('/media/tts/voices')
    voiceGroups.value = v.groups || {}
  } catch { /* */ }
}

function onVoiceFilePick(e: Event) {
  const input = e.target as HTMLInputElement
  voiceFile.value = input.files?.[0] || null
}

async function uploadVoiceForAgent() {
  if (!voiceFile.value || !editing.value?.name) return
  const agentName = editing.value.name
  const voiceName = `${agentName}_${Date.now().toString(36)}`
  voiceUploading.value = true
  try {
    const formData = new FormData()
    formData.append('name', voiceName)
    formData.append('file', voiceFile.value)
    const result = await api.uploadVoiceRef(agentName, formData)
    message.success(t('agentsView.voiceUploaded'))
    voiceFile.value = null
    if (voiceInputEl.value) voiceInputEl.value.value = ''
    editing.value.voice_ref = result.voice_ref
    await loadVoices()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    voiceUploading.value = false
  }
}
</script>

<template>
  <div class="agents-view">
    <div class="view-header">
      <h2>🧚 {{ t('agentsView.title') }}</h2>
      <n-button type="primary" @click="openEditor(null)">＋ {{ t('agentsView.createSub') }}</n-button>
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
              <span class="agent-id">{{ a.display_name_en }}</span>
            </div>
            <n-switch v-if="!a.is_main" size="small" :value="a.enabled"
                      @click.stop @update:value="(v: boolean) => toggleEnabled(a, v)" />
          </div>
          <div class="card-meta">
            <n-tag size="small" :bordered="false" type="success">{{ a.provider }}</n-tag>
            <n-tag size="small" :bordered="false">{{ a.model || t('agentsView.default') }}</n-tag>
            <n-tag size="small" :bordered="false" :type="a.builtin || a.is_main ? 'warning' : 'info'">
              {{ a.is_main ? t('agentsView.main') : a.builtin ? t('agentsView.builtin') : t('agentsView.custom') }}
            </n-tag>
            <n-tag v-if="a.degraded" size="small" :bordered="false" type="warning">{{ t('agentsView.degraded') }}</n-tag>
          </div>
          <div class="card-stats">
            🛠 {{ a.tool_count ?? '—' }} {{ t('agentsView.toolsUnit') }}
            <span v-if="a.mcp_servers?.length"> · 🔌 {{ a.mcp_servers.length }} {{ t('agentsView.mcpUnit') }}</span>
          </div>
          <div class="card-desc">{{ a.route_description || t('agentsView.noRouteDesc') }}</div>
          <div class="card-actions" v-if="!a.builtin && !a.is_main">
            <n-popconfirm @positive-click="removeAgent(a)">
              <template #trigger>
                <n-button size="tiny" type="error" quaternary @click.stop>{{ t('agentsView.delete') }}</n-button>
              </template>
              {{ t('agentsView.deleteConfirm') }} {{ a.display_name }}？
            </n-popconfirm>
          </div>
        </div>
      </Tilt3D>
    </div>

    <n-modal v-model:show="showEditor" preset="card" class="agent-modal"
             :title="isCreate ? t('agentsView.createSub') : `${t('agentsView.editDot')}${editing.display_name || editing.name}`"
             style="width: min(860px, 94vw); max-height: 88vh; overflow-y: auto;">
      <n-tabs type="line" animated>
        <n-tab-pane name="base" :tab="t('agentsView.basicConfig')">
          <n-form label-placement="left" label-width="130">
            <n-form-item :label="t('agentsView.name')" v-if="isCreate">
              <n-input v-model:value="editing.name" :placeholder="t('agentsView.namePlaceholder')" />
            </n-form-item>
            <n-form-item :label="t('agentsView.displayName')">
              <n-input v-model:value="editing.display_name" :placeholder="t('agentsView.displayNamePh')" />
            </n-form-item>
            <n-form-item label="English Name" v-if="!isMain">
              <n-input :value="editing.display_name_en" disabled placeholder="Auto-translated" />
            </n-form-item>
            <n-form-item :label="t('agentsView.model')" v-if="!isMain">
              <n-select v-model:value="selectedModel" :options="modelOptions"
                        :loading="switchingModel" filterable tag
                        :placeholder="t('agentsView.modelPh')"
                        @update:value="(v: string | null) => onModelChange(v)" />
            </n-form-item>
            <n-form-item :label="t('agentsView.advanced')" v-if="!isMain">
              <n-collapse :default-expanded-names="[]">
                <n-collapse-item :title="t('agentsView.advancedTitle')" name="advanced">
                  <n-form label-placement="left" label-width="130" style="margin-top: 4px">
                    <n-form-item label="base_url">
                      <n-input v-model:value="editing.base_url"
                               :placeholder="t('agentsView.baseUrlPh')"
                               @update:value="onAdvancedInput" />
                    </n-form-item>
                    <n-form-item label="api_key_env">
                      <n-input v-model:value="editing.api_key_env"
                               :placeholder="t('agentsView.apiKeyPh')"
                               @update:value="onAdvancedInput" />
                    </n-form-item>
                  </n-form>
                </n-collapse-item>
              </n-collapse>
            </n-form-item>
            <n-form-item :label="t('agentsView.routeDesc')" v-if="!isMain">
              <n-input v-model:value="editing.route_description" type="textarea" :rows="2"
                       :placeholder="t('agentsView.routeDescPh')" />
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
            <n-form-item :label="t('agentsView.permMode')" v-if="!isMain">
              <n-select v-model:value="editing.permission_mode" :options="permModeOptions" />
            </n-form-item>
            <n-form-item :label="t('agentsView.memoryScope')" v-if="!isMain">
              <n-select v-model:value="editing.memory_scope" :options="memScopeOptions" />
            </n-form-item>
            <n-form-item label="voice_ref">
              <div class="voice-ref-field">
                <n-select v-model:value="editing.voice_ref" :options="voiceOptions"
                          :render-label="(opt: any) => replaceAgentNames(opt.label || opt.value || '')"
                          :placeholder="t('agentsView.voiceRefPh')" style="flex: 1" />
                <input ref="voiceInputEl" type="file" accept="audio/mpeg,audio/wav"
                       style="display: none" @change="onVoiceFilePick" />
                <n-button size="small" @click="voiceInputEl?.click()" :loading="voiceUploading">
                  {{ voiceFile ? voiceFile.name : t('agentsView.uploadVoice') }}
                </n-button>
                <n-button size="small" type="primary" :disabled="!voiceFile" @click="uploadVoiceForAgent">
                  {{ t('agentsView.upload') }}
                </n-button>
              </div>
            </n-form-item>
            <n-form-item :label="t('agentsView.backdrop')">
              <div class="wallpaper-field">
                <div class="wallpaper-row">
                  <n-input v-model:value="editing.wallpaper"
                           :placeholder="t('agentsView.wallpaperPh')" />
                  <n-button v-if="!isCreate" :loading="uploadingWp" @click="wpInput?.click()">
                    {{ t('agentsView.uploadImage') }}
                  </n-button>
                  <input ref="wpInput" type="file" accept="image/png,image/jpeg,image/webp"
                         style="display: none" @change="pickWallpaper" />
                </div>
                <div v-if="editing.wallpaper" class="wallpaper-preview"
                     :style="{ backgroundImage: `url('${editing.wallpaper}')` }" />
                <span v-else class="wallpaper-hint">{{ t('agentsView.wallpaperHint') }}</span>
              </div>
            </n-form-item>
          </n-form>
        </n-tab-pane>

        <n-tab-pane name="perm" :tab="t('agentsView.permissions')" v-if="!isCreate">
          <div class="perm-toolbar">
            <span class="perm-hint">{{ t('agentsView.permHint') }}</span>
            <n-button size="small" type="primary" :disabled="!permDirty" @click="applyPermissions">
              {{ t('agentsView.applyPerms') }}
            </n-button>
          </div>
          <div v-for="[cat, group] in toolGroups" :key="cat" class="perm-group">
            <div class="perm-group-head">
              <span>{{ cat }}</span>
              <span class="group-ops">
                <n-button size="tiny" quaternary @click="groupSetAll(group, true)">{{ t('agentsView.allOn') }}</n-button>
                <n-button size="tiny" quaternary @click="groupSetAll(group, false)">{{ t('agentsView.allOff') }}</n-button>
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
            <div class="perm-group-head"><span>🔌 {{ t('agentsView.mcpServices') }}</span></div>
            <div class="perm-rows">
              <div v-for="(info, name) in permissions.mcp_servers" :key="name" class="perm-row">
                <span class="perm-name">{{ name }}</span>
                <n-switch size="small" :value="info.enabled" :disabled="info.locked"
                          @update:value="(v: boolean) => toggleMcpPerm(String(name), v)" />
              </div>
            </div>
          </div>
        </n-tab-pane>

        <n-tab-pane name="personality" :tab="t('agentsView.personality')">
          <n-input v-model:value="personality" type="textarea" :rows="14"
                   :placeholder="t('agentsView.personalityPh')" />
        </n-tab-pane>

        <n-tab-pane name="test" :tab="t('agentsView.test')" v-if="!isCreate">
          <n-button :loading="testing" @click="runTest">{{ t('agentsView.testPrompt') }} {{ editing.display_name }} {{ t('agentsView.sendTest') }}</n-button>
          <div v-if="testResult" class="test-result glass-panel"
               :class="{ failed: !testResult.ok }">
            <div>{{ testResult.ok ? t('agentsView.testPass') : t('agentsView.testFail') }} · {{ testResult.elapsed_ms }}ms</div>
            <div class="test-reply">{{ testResult.reply || testResult.error }}</div>
          </div>
        </n-tab-pane>

        <n-tab-pane name="stickers" :tab="t('agentsView.stickers')" v-if="!isCreate">
          <div class="sticker-section">
            <!-- 上传区域 -->
            <div class="sticker-upload glass-panel">
              <div class="sticker-upload-title">{{ t('agentsView.addSticker') }}</div>
              <div class="sticker-upload-row">
                <input ref="stickerInput" type="file" accept="image/png,image/jpeg,image/gif,image/webp"
                       style="display: none" @change="onStickerFilePick" />
                <n-button size="small" @click="stickerInput?.click()">
                  {{ stickerFile ? stickerFile.name : t('agentsView.selectImage') }}
                </n-button>
                <n-input v-model:value="stickerDesc" size="small" :placeholder="t('agentsView.stickerDescPh')"
                         style="flex: 1; min-width: 120px;" />
                <n-select v-model:value="stickerEmotion" size="small" style="width: 120px"
                          :options="(stickerEmotions.length ? stickerEmotions : ['happy','sad','angry','curious','shy','thinking','neutral','greeting','fear']).map(e => ({ label: e, value: e }))" />
                <n-button type="primary" size="small" :loading="stickerUploading" :disabled="!stickerFile || !stickerDesc.trim()"
                          @click="uploadSticker">
                  {{ t('agentsView.upload') }}
                </n-button>
              </div>
              <div v-if="stickerFile" class="sticker-upload-preview">
                <img :src="createObjectURL(stickerFile)" alt="preview" />
                <span class="sticker-preview-info">{{ stickerDesc || t('agentsView.noDesc') }} · {{ stickerEmotion }}</span>
              </div>
            </div>

            <!-- 表情包列表 -->
            <n-spin :show="stickerLoading">
              <div v-if="stickerList.length" class="sticker-grid">
                <div v-for="s in stickerList" :key="s.name" class="sticker-card">
                  <n-image :src="s.url + '?token=' + token" width="100" height="100" object-fit="cover"
                           :fallback-src="''" lazy class="sticker-img" />
                  <div class="sticker-info">
                    <span class="sticker-desc">{{ s.description }}</span>
                    <n-tag size="tiny" :bordered="false">{{ s.emotion }}</n-tag>
                  </div>
                  <n-popconfirm @positive-click="removeSticker(s.name)">
                    <template #trigger>
                      <n-button size="tiny" type="error" quaternary class="sticker-del">{{ t('agentsView.delete') }}</n-button>
                    </template>
                    {{ t('agentsView.stickerDeleteConfirm') }}
                  </n-popconfirm>
                </div>
              </div>
              <n-empty v-else :description="t('agentsView.stickerEmpty')" style="padding: 32px 0" />
            </n-spin>
          </div>
        </n-tab-pane>
      </n-tabs>

      <template #footer>
        <div class="modal-footer">
          <n-button @click="showEditor = false">{{ t('cancel') }}</n-button>
          <n-button type="primary" :loading="saving" @click="save">
            {{ isCreate ? t('agentsView.create') : t('save') }}
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
.voice-ref-field { display: flex; gap: 8px; align-items: center; width: 100%; flex-wrap: wrap; }
.wallpaper-preview {
  height: 90px;
  border-radius: 10px;
  background: center/cover no-repeat;
  border: 1px solid var(--glass-border);
}
.wallpaper-hint { font-size: 12px; color: var(--moon-dim); }

.modal-footer { display: flex; justify-content: flex-end; gap: 10px; }

/* ── 表情包管理 ── */
.sticker-section { display: flex; flex-direction: column; gap: 14px; }
.sticker-upload { padding: 12px 14px; }
.sticker-upload-title { font-size: 13px; font-weight: 600; color: var(--dendro); margin-bottom: 8px; }
.sticker-upload-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.sticker-upload-preview {
  display: flex; align-items: center; gap: 10px; margin-top: 10px;
}
.sticker-upload-preview img {
  width: 64px; height: 64px; object-fit: cover; border-radius: 8px;
  border: 1px solid var(--glass-border);
}
.sticker-preview-info { font-size: 12px; color: var(--moon-dim); }

.sticker-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
}
.sticker-card {
  display: flex; flex-direction: column; align-items: center;
  padding: 8px; border-radius: 10px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--glass-border);
  position: relative;
}
.sticker-card:hover { border-color: rgba(127, 214, 80, 0.3); }
.sticker-img { border-radius: 8px; overflow: hidden; }
.sticker-info {
  display: flex; flex-direction: column; align-items: center;
  gap: 4px; margin-top: 6px; width: 100%;
}
.sticker-desc {
  font-size: 11px; color: var(--moon); text-align: center;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 120px;
}
.sticker-del { margin-top: 4px; }
</style>
