<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, computed } from 'vue'
import SumeruIcon from '../components/fx/SumeruIcon.vue'
import ViewTitleIcon from '../components/fx/ViewTitleIcon.vue'
import {
  NButton, NSwitch, NInputNumber, NSelect, NTag, NPopconfirm, NSlider, NInput, NModal, useMessage,
} from 'naive-ui'
import { api, get, put, post } from '../api'
import { providerApi } from '../api/providers'
import type { CapabilityReport, ProviderDefinition } from '../api/providers'
import type { CredentialStatus, ModelRouteInfo, UsageSummary } from '../api/types'
import ProviderWizard from '../components/models/ProviderWizard.vue'
import { useProvidersStore } from '../stores/providers'
import { useStaggerEntrance } from '../composables/useStaggerEntrance'
import { t } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const message = useMessage()

const providersStore = useProvidersStore()
const providers = computed(() => providersStore.providers)
const routes = ref<Record<string, ModelRouteInfo>>({})
const fallback = ref<Record<string, string>>({})
const credentials = ref<CredentialStatus[]>([])
const usage = ref<UsageSummary>({ days: 0, series: [], total: {} })
const providerWizardOpen = ref(false)
const editingProvider = ref<ProviderDefinition | null>(null)
const providerTestResults = ref<Record<string, CapabilityReport>>({})
const routeTestResults = ref<Record<string, { ok: boolean; error?: string }>>({})
const testingId = ref('')
const chartEl = ref<HTMLElement | null>(null)
let usageChart: echarts.ECharts | null = null
let usageChartResizeObserver: ResizeObserver | null = null
// 已发现的模型列表（按 provider 分组），用于路由表下拉选择
const discoveredModels = ref<any[]>([])

const providerOptions = computed(() =>
  providers.value.map(p => ({ label: `${p.label} (${p.id})`, value: p.id })))

// 路由表 model 下拉选项：按 provider 分组，和对话页面 ModelSelector 一样
const modelSelectOptions = computed(() => {
  return discoveredModels.value
    .filter(pg => pg.models && pg.models.length)
    .map(pg => ({
      type: 'group' as const,
      label: pg.label || pg.provider,
      key: pg.provider,
      children: pg.models.map((m: any) => ({
        label: m.display_name || m.id,
        value: m.id,
      })),
    }))
})

// 路由表选择模型时自动同步 provider
function onRouteModelChange(r: any, modelId: string) {
  r.model = modelId
  // 找到该模型属于哪个 provider，自动同步
  for (const pg of discoveredModels.value) {
    if ((pg.models || []).some((m: any) => m.id === modelId)) {
      r.provider = pg.provider
      break
    }
  }
}

const builtinProviders = computed(() => providersStore.builtinProviders)
const customProviders = computed(() => providersStore.customProviders)

// 供应商行"首次加载 → 内容"stagger 入场（内置+自定义同容器，统一编排）
const providerListEl = ref<HTMLElement | null>(null)
useStaggerEntrance(providerListEl, providers, { distance: 12, staggerEach: 0.045 })

onMounted(() => {
  void loadAll()
  if (chartEl.value) {
    usageChartResizeObserver = new ResizeObserver(() => usageChart?.resize())
    usageChartResizeObserver.observe(chartEl.value)
  }
})

onBeforeUnmount(() => {
  usageChartResizeObserver?.disconnect(); usageChartResizeObserver = null
  usageChart?.dispose(); usageChart = null
  if (_tempSaveTimer) clearTimeout(_tempSaveTimer)
})

async function loadAll() {
  try {
    // discover 涉及外部 API 聚合（冷启动 2.6s），不并入主 Promise.all——
    // 页面先用核心数据渲染，discover 到货后单独更新（stale-while-revalidate 下通常 <20ms）
    const [p, r, c, u] = await Promise.all([
      providersStore.loadProviders(),
      get<{ routes: Record<string, ModelRouteInfo>; fallback: Record<string, string> }>('/models/routes'),
      get<CredentialStatus[]>('/models/credentials/status'),
      get<UsageSummary>('/models/usage?days=7'),
    ])
    void p
    routes.value = r.routes
    fallback.value = r.fallback
    credentials.value = c
    usage.value = u
    renderChart()
    loadTemperature()
    loadFreqPenalty()
    loadPresPenalty()
    get<any[]>('/models/discover')
      .then(dm => { discoveredModels.value = dm })
      .catch(() => {})
  } catch (e: any) {
    message.error(e.message)
  }
}

function renderChart() {
  if (!chartEl.value) return
  const days = [...new Set(usage.value.series.map(s => s.day))].sort()
  const models = [...new Set(usage.value.series.map(s => s.model))]
  const series = models.map(m => ({
    name: m, type: 'bar', stack: 'tokens',
    data: days.map(d => {
      const row = usage.value.series.find(s => s.day === d && s.model === m)
      return row ? ((row.prompt_tokens || 0) + (row.completion_tokens || 0)) : 0
    }),
  }))
  if (!usageChart) usageChart = echarts.init(chartEl.value)
  usageChart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: '#f2f7ee' }, type: 'scroll' },
    grid: { left: 60, right: 20, top: 40, bottom: 24 },
    xAxis: { type: 'category', data: days, axisLabel: { color: '#f2f7ee' } },
    yAxis: { type: 'value', axisLabel: { color: '#f2f7ee' }, splitLine: { lineStyle: { color: 'rgba(127,214,80,.1)' } } },
    series,
  })
}

function openProviderForm(provider: ProviderDefinition | null) {
  editingProvider.value = provider
  providerWizardOpen.value = true
}

async function removeProvider(id: string) {
  try {
    await providersStore.deleteProvider(id)
    message.success(t('modelsView.deleted'))
    await loadAll()
  } catch (e: any) {
    message.error(e.message)
  }
}

async function testProvider(id: string) {
  testingId.value = id
  try {
    providerTestResults.value[id] = await providersStore.loadCapabilities(id)
  } catch (e: any) {
    providerTestResults.value[id] = { available: false, capabilities: { tools: false, vision: false, streaming: false, model_discovery: false, json_mode: false }, models: [], error: e.message }
  } finally {
    testingId.value = ''
  }
}

function onRouteProviderChange(r: any, pid: string) {
  const p = providers.value.find(x => x.id === pid)
  if (p?.default_model) r.model = p.default_model
}

async function saveRoute(task: string) {
  const r = routes.value[task]
  try {
    await put(`/models/routes/${task}`, {
      model: r.model, provider: r.provider,
      max_tokens: r.max_tokens, thinking: r.thinking, timeout: r.timeout,
    })
    message.success(t('modelsView.routeLabel') + ` ${task} ` + t('modelsView.updatedActive'))
  } catch (e: any) {
    message.error(e.message)
    await loadAll()
  }
}

async function testRoute(task: string) {
  testingId.value = `route:${task}`
  try {
    routeTestResults.value[task] = await api.testModelRoute(task)
  } catch (e: any) {
    routeTestResults.value[task] = { ok: false, error: e.message }
  } finally {
    testingId.value = ''
  }
}

const stateColor: Record<string, string> = { ok: 'success', exhausted: 'warning', dead: 'error' }

// Temperature 控制
const temperature = ref(0.7)
const tempSource = ref<'override' | 'config'>('config')
const tempLoading = ref(false)
let _tempSaveTimer: ReturnType<typeof setTimeout> | null = null

const tempPresets = [
  { label: '精准', value: 0.0, desc: '确定性最高' },
  { label: '保守', value: 0.3, desc: '偏向稳定' },
  { label: '平衡', value: 0.7, desc: '默认推荐' },
  { label: '创意', value: 1.0, desc: '更有想象力' },
  { label: '狂野', value: 1.5, desc: '最大随机性' },
]

async function loadTemperature() {
  try {
    const res = await get<any>('/models/temperature')
    temperature.value = res.temperature ?? 0.7
    tempSource.value = res.source ?? 'config'
  } catch { /* use default */ }
  await loadDedup()
}

async function _doSaveTemperature() {
  tempLoading.value = true
  try {
    const res = await put<any>('/models/temperature', { temperature: temperature.value })
    temperature.value = res.temperature
    tempSource.value = 'override'
  } catch (e: any) {
    message.error(e.message)
  } finally {
    tempLoading.value = false
  }
}

function onTempChange() {
  if (_tempSaveTimer) clearTimeout(_tempSaveTimer)
  _tempSaveTimer = setTimeout(_doSaveTemperature, 400)
}

function setTempPreset(val: number) {
  temperature.value = val
  _doSaveTemperature()
}

// 跨对话回复去重开关（跟随温度一起保存，热生效）
const dedupEnabled = ref(true)
const dedupLoading = ref(false)
let _dedupSaveTimer: ReturnType<typeof setTimeout> | null = null

async function loadDedup() {
  try {
    const res = await get<any>('/models/reply_dedup')
    dedupEnabled.value = res.enabled ?? true
  } catch { /* use default */ }
}

async function _doSaveDedup() {
  dedupLoading.value = true
  try {
    await put<any>('/models/reply_dedup', { enabled: dedupEnabled.value })
  } catch (e: any) {
    message.error(e.message)
  } finally {
    dedupLoading.value = false
  }
}

function onDedupChange() {
  if (_dedupSaveTimer) clearTimeout(_dedupSaveTimer)
  _dedupSaveTimer = setTimeout(_doSaveDedup, 400)
}

// Frequency Penalty 控制
const freqPenalty = ref(1.0)
const freqSource = ref<'override' | 'default'>('default')
const freqLoading = ref(false)
let _freqSaveTimer: ReturnType<typeof setTimeout> | null = null

const freqPresets = [
  { label: '关闭', value: 0.0, desc: '不惩罚重复' },
  { label: '轻度', value: 0.3, desc: '轻微惩罚' },
  { label: '标准', value: 1.0, desc: '默认推荐' },
  { label: '强力', value: 1.5, desc: '强惩罚重复' },
  { label: '极限', value: 2.0, desc: '最大惩罚' },
]

async function loadFreqPenalty() {
  try {
    const res = await get<any>('/models/frequency_penalty')
    freqPenalty.value = res.frequency_penalty ?? 1.0
    freqSource.value = res.source ?? 'default'
  } catch { /* use default */ }
}

async function _doSaveFreqPenalty() {
  freqLoading.value = true
  try {
    const res = await put<any>('/models/frequency_penalty', { frequency_penalty: freqPenalty.value })
    freqPenalty.value = res.frequency_penalty
    freqSource.value = 'override'
  } catch (e: any) {
    message.error(e.message)
  } finally {
    freqLoading.value = false
  }
}

function onFreqChange() {
  if (_freqSaveTimer) clearTimeout(_freqSaveTimer)
  _freqSaveTimer = setTimeout(_doSaveFreqPenalty, 400)
}

function setFreqPreset(val: number) {
  freqPenalty.value = val
  _doSaveFreqPenalty()
}

// Presence Penalty 控制
const presPenalty = ref(1.0)
const presSource = ref<'override' | 'default'>('default')
const presLoading = ref(false)
let _presSaveTimer: ReturnType<typeof setTimeout> | null = null

const presPresets = [
  { label: '关闭', value: 0.0, desc: '不惩罚新 token' },
  { label: '轻度', value: 0.3, desc: '轻微惩罚' },
  { label: '标准', value: 1.0, desc: '默认推荐' },
  { label: '强力', value: 1.5, desc: '强惩罚' },
  { label: '极限', value: 2.0, desc: '最大惩罚' },
]

async function loadPresPenalty() {
  try {
    const res = await get<any>('/models/presence_penalty')
    presPenalty.value = res.presence_penalty ?? 1.0
    presSource.value = res.source ?? 'default'
  } catch { /* use default */ }
}

async function _doSavePresPenalty() {
  presLoading.value = true
  try {
    const res = await put<any>('/models/presence_penalty', { presence_penalty: presPenalty.value })
    presPenalty.value = res.presence_penalty
    presSource.value = 'override'
  } catch (e: any) {
    message.error(e.message)
  } finally {
    presLoading.value = false
  }
}

function onPresChange() {
  if (_presSaveTimer) clearTimeout(_presSaveTimer)
  _presSaveTimer = setTimeout(_doSavePresPenalty, 400)
}

function setPresPreset(val: number) {
  presPenalty.value = val
  _doSavePresPenalty()
}

const showKeyModal = ref(false)
const keyProviderId = ref('')
const keyProviderLabel = ref('')
const keyValue = ref('')
const keySaving = ref(false)
const keyResult = ref('')

function openKeyModal(pid: string, label: string) {
  keyProviderId.value = pid
  keyProviderLabel.value = label
  keyValue.value = ''
  keyResult.value = ''
  showKeyModal.value = true
}

async function saveKey() {
  keySaving.value = true
  try {
    const res = await providerApi.setKey(keyProviderId.value, keyValue.value)
    keyResult.value = `已保存：${res.key_masked}`
    message.success('API Key 已更新')
    await loadAll()
  } catch (e: any) {
    keyResult.value = e.message
    message.error(e.message)
  } finally {
    keySaving.value = false
  }
}

async function reorderProviders() {
  const order = providers.value.map(p => p.id)
  try {
    await providerApi.reorder(order)
    message.success('Provider 顺序已保存')
  } catch (e: any) {
    message.error(e.message)
  }
}

async function moveProvider(pid: string, dir: -1 | 1) {
  const list = [...providers.value]
  const idx = list.findIndex(p => p.id === pid)
  if (idx < 0) return
  const target = idx + dir
  if (target < 0 || target >= list.length) return
  ;[list[idx], list[target]] = [list[target], list[idx]]
  try {
    await providerApi.reorder(list.map(p => p.id))
    await loadAll()
    message.success('顺序已调整')
  } catch (e: any) {
    message.error(e.message)
  }
}
</script>

<template>
  <div class="models-view">
    <div class="view-header page-header">
      <h2 class="view-title-icon"><ViewTitleIcon name="models" /> {{ t('modelsView.title') }}</h2>
      <div class="page-actions">
        <n-button type="primary" @click="openProviderForm(null)"><SumeruIcon name="plus" :size="14" variant="duo" tone="add" interactive /> {{ t('modelsView.customProvider') }}</n-button>
      </div>
    </div>

    <Tilt3D :max-x="4" :max-y="6">
    <section class="glass-panel section">
      <h3>{{ t('modelsView.providerList') }}</h3>
      <div ref="providerListEl" class="provider-list">
        <div v-for="p in builtinProviders" :key="p.id" class="provider-row">
          <div class="provider-info">
            <span class="p-label">{{ p.label }}</span>
            <n-tag size="small" :type="p.protocol === 'anthropic' ? 'warning' : 'info'" :bordered="false">
              {{ p.protocol }}
            </n-tag>
            <n-tag v-if="p.builtin" size="small" :bordered="false">{{ t('modelsView.builtin') }}</n-tag>
            <span class="p-url">{{ p.base_url }}</span>
          </div>
          <div class="provider-ops">
            <span v-if="providerTestResults[p.id]" class="test-badge"
                  :class="{ ok: providerTestResults[p.id].available }">
              {{ providerTestResults[p.id].available ? `✓ ${providerTestResults[p.id].models.length} 个模型` : `✗ ${providerTestResults[p.id].error?.slice(0, 60)}` }}
            </span>
            <n-button size="tiny" :loading="testingId === p.id" @click="testProvider(p.id)">{{ t('modelsView.test') }}</n-button>
            <n-button size="tiny" quaternary @click="openKeyModal(p.id, p.label)">🔑 Key</n-button>
            <n-button size="tiny" quaternary @click="moveProvider(p.id, -1)" :disabled="builtinProviders.indexOf(p) === 0">↑</n-button>
            <n-button size="tiny" quaternary @click="moveProvider(p.id, 1)" :disabled="builtinProviders.indexOf(p) === builtinProviders.length - 1">↓</n-button>
          </div>
        </div>
        <div v-for="p in customProviders" :key="p.id" class="provider-row">
              <div class="provider-info">
                <span class="p-label">{{ p.label }}</span>
                <n-tag size="small" :type="p.protocol === 'anthropic' ? 'warning' : 'info'" :bordered="false">
                  {{ p.protocol }}
                </n-tag>
                <span class="p-url">{{ p.base_url }}</span>
              </div>
              <div class="provider-ops">
                <span v-if="providerTestResults[p.id]" class="test-badge"
                      :class="{ ok: providerTestResults[p.id].available }">
                  {{ providerTestResults[p.id].available ? `✓ ${providerTestResults[p.id].models.length} 个模型` : `✗ ${providerTestResults[p.id].error?.slice(0, 60)}` }}
                </span>
                <n-button size="tiny" :loading="testingId === p.id" @click="testProvider(p.id)">{{ t('modelsView.test') }}</n-button>
                <n-button size="tiny" quaternary @click="openKeyModal(p.id, p.label)">🔑 Key</n-button>
                <n-button size="tiny" @click="openProviderForm(p)">{{ t('modelsView.edit') }}</n-button>
                <n-button size="tiny" quaternary @click="moveProvider(p.id, -1)">↑</n-button>
                <n-button size="tiny" quaternary @click="moveProvider(p.id, 1)">↓</n-button>
                <n-popconfirm v-if="!p.builtin" @positive-click="removeProvider(p.id)">
                  <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('modelsView.delete') }}</n-button></template>
                  {{ t('modelsView.confirmDelete') }} provider {{ p.id }}？
                </n-popconfirm>
              </div>
            </div>
      </div>
    </section>
    </Tilt3D>

    <Tilt3D :max-x="4" :max-y="6">
    <section class="glass-panel section">
      <h3>{{ t('modelsView.taskRouting') }} <span class="hint">{{ t('modelsView.noRestartHint') }}</span></h3>
      <div class="table-scroll">
        <table class="route-table">
          <thead>
            <tr><th>{{ t('modelsView.taskCol') }}</th><th>model</th><th>max_tokens</th><th>thinking</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="(r, task) in routes" :key="task">
              <td class="mono">{{ task }}</td>
              <td>
                <n-select
                  v-model:value="r.model"
                  class="route-select"
                  size="small"
                  filterable
                  :options="modelSelectOptions"
                  @update:value="(v: string) => onRouteModelChange(r, v)"
                />
              </td>
              <td><n-input-number v-model:value="r.max_tokens" class="route-number" size="small" :min="64" :max="32768" :show-button="false" /></td>
              <td><n-switch v-model:value="r.thinking" size="small" /></td>
              <td class="route-ops">
                <n-button size="tiny" type="primary" secondary @click="saveRoute(task as string)">{{ t('modelsView.save') }}</n-button>
                <n-button size="tiny" :loading="testingId === `route:${task}`" @click="testRoute(task as string)">{{ t('modelsView.test') }}</n-button>
                <span v-if="routeTestResults[task as string]" class="test-badge"
                      :class="{ ok: routeTestResults[task as string].ok }">
                  {{ routeTestResults[task as string].ok ? '✓' : '✗' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="fallback-chain">
        {{ t('modelsView.degradeChain') }}<template v-for="(to, from, i) in fallback" :key="from">
          <span v-if="i > 0" class="chain-sep"> ｜ </span>
          <span class="mono">{{ from }} → {{ to }}</span>
        </template>
      </div>
    </section>
    </Tilt3D>

    <Tilt3D :max-x="4" :max-y="6">
    <section class="glass-panel section">
      <h3 class="parameter-heading">Temperature 调节 <span class="hint">控制 LLM 回复的随机性，越低越确定，越高越发散 <span v-if="tempLoading" class="temp-saving">保存中...</span></span></h3>
      <div class="temp-row">
        <span class="temp-val">{{ temperature.toFixed(2) }}</span>
        <n-slider class="parameter-slider" :value="temperature" :min="0" :max="2" :step="0.05"
                  :tooltip="false"
                  @update:value="(v: number) => { temperature = v; onTempChange() }" />
      </div>
      <div class="temp-presets">
        <n-button v-for="p in tempPresets" :key="p.value" size="tiny" quaternary
                  :type="Math.abs(temperature - p.value) < 0.03 ? 'primary' : 'default'"
                  @click="setTempPreset(p.value)">
          {{ p.label }} {{ p.value }}
        </n-button>
      </div>
      <div class="dedup-row">
        <span class="hint">自动去重：回复内容与近期历史高度相似时自动重写一次</span>
        <n-switch v-model:value="dedupEnabled" size="medium"
                  :loading="dedupLoading" @update:value="onDedupChange" />
      </div>
    </section>
    </Tilt3D>

    <Tilt3D :max-x="4" :max-y="6">
    <section class="glass-panel section">
      <h3 class="parameter-heading">Frequency Penalty 调节 <span class="hint">惩罚已出现 token 的重复频率，越高越抑制套模板重复 <span v-if="freqLoading" class="temp-saving">保存中...</span></span></h3>
      <div class="temp-row">
        <span class="temp-val">{{ freqPenalty.toFixed(2) }}</span>
        <n-slider class="parameter-slider" :value="freqPenalty" :min="0" :max="2" :step="0.05"
                  :tooltip="false"
                  @update:value="(v: number) => { freqPenalty = v; onFreqChange() }" />
      </div>
      <div class="temp-presets">
        <n-button v-for="p in freqPresets" :key="p.value" size="tiny" quaternary
                  :type="Math.abs(freqPenalty - p.value) < 0.03 ? 'primary' : 'default'"
                  @click="setFreqPreset(p.value)">
          {{ p.label }} {{ p.value }}
        </n-button>
      </div>
    </section>
    </Tilt3D>

    <Tilt3D :max-x="4" :max-y="6">
    <section class="glass-panel section">
      <h3 class="parameter-heading">Presence Penalty 调节 <span class="hint">惩罚已出现 token 的再次生成，越高越抑制条件模式重复 <span v-if="presLoading" class="temp-saving">保存中...</span></span></h3>
      <div class="temp-row">
        <span class="temp-val">{{ presPenalty.toFixed(2) }}</span>
        <n-slider class="parameter-slider" :value="presPenalty" :min="0" :max="2" :step="0.05"
                  :tooltip="false"
                  @update:value="(v: number) => { presPenalty = v; onPresChange() }" />
      </div>
      <div class="temp-presets">
        <n-button v-for="p in presPresets" :key="p.value" size="tiny" quaternary
                  :type="Math.abs(presPenalty - p.value) < 0.03 ? 'primary' : 'default'"
                  @click="setPresPreset(p.value)">
          {{ p.label }} {{ p.value }}
        </n-button>
      </div>
    </section>
    </Tilt3D>

    <Tilt3D :max-x="4" :max-y="6">
    <section class="glass-panel section">
      <h3>{{ t('modelsView.credPoolStatus') }}</h3>
      <div class="table-scroll">
        <table class="route-table">
          <thead><tr><th>provider</th><th>key</th><th>{{ t('modelsView.statusCol') }}</th><th>{{ t('modelsView.usageCol') }}</th><th>{{ t('modelsView.errorCol') }}</th></tr></thead>
          <tbody>
            <tr v-for="c in credentials" :key="`${c.provider}-${c.index}`">
              <td>{{ c.provider }}</td>
              <td class="mono">{{ c.key_masked }}</td>
              <td><n-tag size="small" :type="(stateColor[c.state] as any) || 'default'" :bordered="false">{{ c.state }}</n-tag></td>
              <td>{{ c.use_count }}</td>
              <td class="error-cell">{{ c.last_error || '—' }}</td>
            </tr>
            <tr v-if="!credentials.length"><td colspan="5" class="empty-cell">{{ t('modelsView.credPoolEmpty') }}</td></tr>
          </tbody>
        </table>
      </div>
    </section>
    </Tilt3D>

    <Tilt3D :max-x="4" :max-y="6">
    <section class="glass-panel section">
      <h3>{{ t('modelsView.usage7days') }}
        <span class="hint" v-if="usage.total">
          {{ t('modelsView.totalCalls') }} {{ usage.total.calls || 0 }} 次调用 · {{ ((usage.total.tokens || 0) / 1000).toFixed(1) }}k tokens
          · ${{ (usage.total.cost || 0).toFixed(4) }}
        </span>
      </h3>
      <div ref="chartEl" class="usage-chart"></div>
    </section>
    </Tilt3D>

    <provider-wizard v-model:show="providerWizardOpen" :provider="editingProvider" @saved="loadAll" />

    <n-modal v-model:show="showKeyModal" preset="card" :title="`🔑 ${keyProviderLabel} — API Key`" style="width: min(440px, 92vw)">
      <n-input v-model:value="keyValue" type="password" show-password-on="click" placeholder="输入新的 API Key" />
      <div class="key-modal-footer">
        <n-button type="primary" :loading="keySaving" :disabled="!keyValue" @click="saveKey">保存</n-button>
        <span v-if="keyResult" class="key-result">{{ keyResult }}</span>
      </div>
    </n-modal>
  </div>
</template>

<style scoped>
.models-view {
  width: 100%; max-width: 100%; min-width: 0;
}
.models-view > * { max-width: 100%; min-width: 0; }
.view-header h2 {
  min-width: 0; margin: 0; overflow-wrap: anywhere;
  font-family: 'Noto Serif SC', serif;
}
.view-header .page-actions { max-width: 100%; }

.section {
  box-sizing: border-box; width: 100%; max-width: 100%; min-width: 0;
  padding: 16px 18px; margin-bottom: 16px;
}
.section h3 {
  font-size: 15px; margin-bottom: 12px; color: var(--dendro);
  overflow-wrap: anywhere;
}
.hint {
  font-size: 12px; color: var(--moon-dim); font-weight: 400; margin-left: 10px;
  overflow-wrap: anywhere;
}

.provider-list { display: flex; min-width: 0; flex-direction: column; gap: 8px; }
.provider-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px 12px; min-width: 0; padding: 8px 10px; border-radius: 8px;
  border: 1px solid var(--glass-border);
  flex-wrap: wrap;
}
.provider-info {
  display: flex; flex: 1 1 360px; align-items: center; gap: 6px 8px;
  flex-wrap: wrap; min-width: 0; max-width: 100%;
}
.p-label { min-width: 0; max-width: 100%; font-weight: 600; overflow-wrap: anywhere; }
.p-url {
  min-width: 0; max-width: 100%; font-size: 12px; color: var(--moon-dim);
  font-family: 'JetBrains Mono', monospace; overflow-wrap: anywhere; word-break: break-word;
}
.provider-ops {
  display: flex; flex: 0 1 auto; align-items: center; gap: 6px;
  flex-wrap: wrap; min-width: 0; max-width: 100%;
}
.provider-ops > .n-button,
.provider-ops > .n-popconfirm { flex: 0 0 auto; }

.test-badge {
  min-width: 0; max-width: min(100%, 360px); font-size: 12px; color: var(--alert);
  overflow-wrap: anywhere; word-break: break-word;
}
.test-badge.ok { color: var(--dendro); }

.table-scroll { max-width: 100%; }
.route-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.route-table th {
  text-align: left; padding: 6px 8px; color: var(--moon-dim);
  border-bottom: 1px solid var(--glass-border); font-weight: 500;
}
.route-table td { padding: 6px 8px; border-bottom: 1px solid rgba(127, 214, 80, 0.08); }
.route-select { width: 220px; min-width: 220px; }
.route-number { width: 90px; }
.route-ops { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
.mono { font-family: 'JetBrains Mono', monospace; font-size: 12.5px; }
.error-cell {
  max-width: 280px; font-size: 12px; color: var(--alert);
  overflow-wrap: anywhere; word-break: break-word;
}
.empty-cell { text-align: center; color: var(--moon-dim); }

.fallback-chain {
  max-width: 100%; margin-top: 10px; font-size: 12.5px; color: var(--wisdom);
  overflow-wrap: anywhere; word-break: break-word;
}

.parameter-heading { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; }
.parameter-heading .hint { min-width: 0; margin-left: 0; }
.temp-row {
  display: flex; min-width: 0; align-items: center; gap: 12px; margin-bottom: 10px;
}
.parameter-slider { flex: 1 1 auto; min-width: 0; margin: 0 16px; }
.temp-val {
  font-family: 'JetBrains Mono', monospace; font-size: 20px; font-weight: 700;
  color: var(--dendro); min-width: 48px; text-align: right;
}
.temp-presets {
  display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 6px;
}
.temp-presets > .n-button { flex: 0 1 auto; }
.dedup-row {
  display: flex; align-items: center; justify-content: space-between; gap: 8px 12px;
  flex-wrap: wrap; min-width: 0; margin-top: 10px; padding-top: 10px;
  border-top: 1px dashed rgba(255,255,255,.12);
}
.dedup-row .hint { flex: 1 1 320px; min-width: 0; margin-left: 0; }
.dedup-row .n-switch { flex: 0 0 auto; }
.temp-source {
  font-size: 12px; color: var(--moon-dim); margin-top: 4px;
}
.temp-saving {
  color: var(--dendro); font-size: 12px; font-weight: 400;
  animation: pulse 1s ease-in-out infinite;
}
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }

.usage-chart { width: 100%; max-width: 100%; height: 260px; }
.key-modal-footer { display: flex; min-width: 0; align-items: center; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.key-result { min-width: 0; font-size: 12px; color: var(--dendro); overflow-wrap: anywhere; }

@media (max-width: 768px) {
  .models-view > :deep(.tilt3d) { max-width: 100%; min-width: 0; }
  .view-header.page-header { max-width: 100%; }
  .view-header .page-actions,
  .view-header .page-actions > .n-button { max-width: 100%; }
  .view-header .page-actions > .n-button { white-space: normal; }
  .section { padding: 14px 12px; }
  .section h3 { line-height: 1.5; }
  .parameter-heading { align-items: flex-start; flex-direction: column; gap: 2px; }
  .provider-row { align-items: flex-start; padding: 8px; }
  .provider-info,
  .provider-ops { flex: 1 1 100%; width: 100%; }
  .provider-ops { justify-content: flex-start; }
  .provider-ops .test-badge { flex: 1 1 100%; max-width: 100%; }
  .fallback-chain { line-height: 1.7; }
  .temp-row { gap: 8px; }
  .parameter-slider { margin: 0 4px; }
  .temp-presets { gap: 2px 4px; }
  .temp-presets > .n-button { padding-inline: 6px; }
  .dedup-row { align-items: flex-start; }
  .dedup-row .hint { flex-basis: calc(100% - 52px); }
  .key-modal-footer { align-items: flex-start; flex-direction: column; }
}

@media (max-width: 420px) {
  .section { padding-inline: 10px; }
  .temp-val { min-width: 44px; font-size: 18px; }
  .parameter-slider { margin-inline: 0; }
  .dedup-row .hint { flex-basis: 100%; }
}
</style>
