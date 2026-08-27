/**
 * Agent 编辑器编排：表单状态 + 保存链路 + 权限矩阵 + 模型热切换。
 * 从 AgentsView.vue 原样迁移（行为不变），由 AgentEditorModal 持有实例。
 */
import { ref, computed, watch, onScopeDispose } from 'vue'
import { useMessage } from 'naive-ui'
import { get, post, put, api } from '../api'
import type { AgentPermissions } from '../api/types'
import { useAgentsStore } from '../stores/agents'
import { getWsClient } from '../api/ws'
import { t } from '../i18n'
import { replaceAgentNames, refreshAgentNames } from '../utils/agentNames'
import { pinyin } from 'pinyin-pro'
import type { AgentEditModel, ToolGroup } from '../components/agents/types'

// 中文转拼音（当编辑时使用）
function translateToEn(zhName: string): string {
  if (!zhName) return ''
  const result = pinyin(zhName, { toneType: 'none', type: 'array' })
  const joined = result.join('')
  return joined.charAt(0).toUpperCase() + joined.slice(1).toLowerCase()
}

export function useAgentEditor() {
  const message = useMessage()
  const agentsStore = useAgentsStore()
  const ws = getWsClient()

  const showEditor = ref(false)
  const isCreate = ref(false)
  /** save() 每失败一次自增；动效外壳 watch 它做失败震颤，成功不变动 */
  const saveFailedTick = ref(0)
  const editing = ref<AgentEditModel>({})
  const personality = ref('')
  const permissions = ref<AgentPermissions>({ tools: {}, mcp_servers: {}, is_main: false })
  const permDirty = ref(false)
  const saving = ref(false)
  const discoveredModels = ref<Array<{ provider: string; label?: string; models: Array<{ id: string; display_name: string; free: boolean }> }>>([])
  const advancedTouched = ref(false)
  const switchingModel = ref(false)

  // 自动翻译显示名为英文（当显示名变化时）
  watch(() => editing.value?.display_name, (newName?: string) => {
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

  ws.on('config_changed', onConfigChanged)
  onScopeDispose(() => {
    ws.off('config_changed', onConfigChanged)
  })

  async function loadDiscoveredModels() {
    try {
      const data = await get<Array<{ provider: string; label?: string; models: Array<{ id: string; display_name: string; free: boolean }> }>>('/models/discover')
      discoveredModels.value = data || []
    } catch { /* 忽略，保留手输 */ }
  }

  loadDiscoveredModels()

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

  const isMain = computed(() => editing.value?.name === 'xiaoda' || editing.value?.is_main === true)

  const toolGroups = computed<Array<ToolGroup>>(() => {
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

  async function open(agent: AgentEditModel | null) {
    isCreate.value = !agent
    permDirty.value = false
    advancedTouched.value = false
    if (agent) {
      editing.value = JSON.parse(JSON.stringify(agent))
      try {
        const p = await get<{ personality?: string }>(`/agents/${agent.name}/personality`)
        personality.value = p.personality || ''
      } catch { personality.value = '' }
      try {
        permissions.value = await get<AgentPermissions>(`/agents/${agent.name}/permissions`)
        loadToolCategories()
      } catch { permissions.value = { tools: {}, mcp_servers: {}, is_main: false } }
    } else {
      editing.value = {
        name: '', display_name: '', provider: 'mimo', model: '',
        base_url: '', api_key_env: '', route_description: '', capabilities: [],
        voice_ref: null, max_turns: 8, effort: 'medium',
        permission_mode: 'default', memory_scope: 'shared', wallpaper: '',
        ack_messages: [],
      }
      personality.value = ''
      permissions.value = { tools: {}, mcp_servers: {}, is_main: false }
    }
    showEditor.value = true
  }

  async function save() {
    saving.value = true
    try {
      const body: AgentEditModel & { personality_text: string } = {
        ...editing.value,
        personality_text: personality.value,
      }
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
      // 保存失败信号（纯状态计数）：外壳层可据此做震颤等差异化反馈
      saveFailedTick.value++
    } finally {
      saving.value = false
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
      const result = await put<AgentPermissions>(`/agents/${editing.value.name}/permissions`,
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

  return {
    showEditor,
    isCreate,
    saveFailedTick,
    editing,
    personality,
    permissions,
    permDirty,
    saving,
    isMain,
    toolGroups,
    modelOptions,
    selectedModel,
    switchingModel,
    open,
    save,
    close: () => { showEditor.value = false },
    onModelChange,
    onAdvancedInput,
    togglePerm,
    toggleMcpPerm,
    groupSetAll,
    applyPermissions,
  }
}

export type AgentEditor = ReturnType<typeof useAgentEditor>
