<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import {
  NButton, NSwitch, NModal, NForm, NFormItem, NInput, NInputNumber,
  NSelect, NTag, NPopconfirm, NRadioGroup, NRadio, useMessage,
} from 'naive-ui'
import draggable from 'vuedraggable'
import { get, post, put, del } from '../api'
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([BarChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

const message = useMessage()

const providers = ref<any[]>([])
const routes = ref<Record<string, any>>({})
const fallback = ref<Record<string, string>>({})
const credentials = ref<any[]>([])
const usage = ref<any>({ series: [], total: {} })
const showProviderForm = ref(false)
const providerForm = ref<any>({})
const isCreateProvider = ref(false)
const testResults = ref<Record<string, any>>({})
const testingId = ref('')
const chartEl = ref<HTMLElement | null>(null)
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

const builtinProviders = computed(() => providers.value.filter(p => p.builtin))
const customProviders = computed({
  get: () => providers.value.filter(p => !p.builtin),
  set: (val: any[]) => {
    providers.value = [...builtinProviders.value, ...val]
  },
})

onMounted(loadAll)

async function loadAll() {
  try {
    const [p, r, c, u, dm] = await Promise.all([
      get<any[]>('/models/providers'),
      get('/models/routes'),
      get<any[]>('/models/credentials/status'),
      get('/models/usage?days=7'),
      get<any[]>('/models/discover').catch(() => []),
    ])
    providers.value = p
    routes.value = r.routes
    fallback.value = r.fallback
    credentials.value = c
    usage.value = u
    discoveredModels.value = dm
    renderChart()
  } catch (e: any) {
    message.error(e.message)
  }
}

function renderChart() {
  if (!chartEl.value) return
  const days = [...new Set(usage.value.series.map((s: any) => s.day))].sort()
  const models = [...new Set(usage.value.series.map((s: any) => s.model))]
  const series = models.map(m => ({
    name: m, type: 'bar', stack: 'tokens',
    data: days.map(d => {
      const row = usage.value.series.find((s: any) => s.day === d && s.model === m)
      return row ? (row.prompt_tokens + row.completion_tokens) : 0
    }),
  }))
  const chart = echarts.init(chartEl.value)
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { textStyle: { color: '#f2f7ee' }, type: 'scroll' },
    grid: { left: 60, right: 20, top: 40, bottom: 24 },
    xAxis: { type: 'category', data: days, axisLabel: { color: '#f2f7ee' } },
    yAxis: { type: 'value', axisLabel: { color: '#f2f7ee' }, splitLine: { lineStyle: { color: 'rgba(127,214,80,.1)' } } },
    series,
  })
}

function openProviderForm(p: any | null) {
  isCreateProvider.value = !p
  providerForm.value = p
    ? { ...p, api_key: '' }
    : { id: '', label: '', format: 'openai', base_url: '', default_model: '', api_key: '' }
  showProviderForm.value = true
}

async function saveProvider() {
  try {
    if (isCreateProvider.value) {
      await post('/models/providers', providerForm.value)
      message.success('provider 已创建并注册 ✓')
    } else {
      await put(`/models/providers/${providerForm.value.id}`, providerForm.value)
      if (providerForm.value.api_key) {
        await post(`/models/providers/${providerForm.value.id}/key`,
          { api_key: providerForm.value.api_key })
      }
      message.success('provider 已更新 ✓')
    }
    showProviderForm.value = false
    await loadAll()
  } catch (e: any) {
    message.error(e.message)
  }
}

async function removeProvider(id: string) {
  try {
    await del(`/models/providers/${id}`, true)
    message.success('已删除')
    await loadAll()
  } catch (e: any) {
    message.error(e.message)
  }
}

async function testProvider(id: string) {
  testingId.value = id
  try {
    testResults.value[id] = await post('/health/test/llm', { provider_id: id })
  } catch (e: any) {
    testResults.value[id] = { ok: false, error: e.message }
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
    message.success(`路由 ${task} 已更新，即时生效 ✓`)
  } catch (e: any) {
    message.error(e.message)
    await loadAll()
  }
}

async function testRoute(task: string) {
  testingId.value = `route:${task}`
  try {
    testResults.value[`route:${task}`] = await post('/health/test/llm', { route: task })
  } catch (e: any) {
    testResults.value[`route:${task}`] = { ok: false, error: e.message }
  } finally {
    testingId.value = ''
  }
}

async function onDragEnd() {
  try {
    const order = customProviders.value.map(p => p.id)
    await post('/models/providers/reorder', { order })
    message.success('Provider 顺序已更新 ✓')
  } catch (e: any) {
    message.error(e.message)
    await loadAll()
  }
}

const stateColor: Record<string, string> = { ok: 'success', exhausted: 'warning', dead: 'error' }
</script>

<template>
  <div class="models-view">
    <div class="view-header">
      <h2>🧠 模型与凭证</h2>
      <n-button type="primary" @click="openProviderForm(null)">＋ 自定义 Provider</n-button>
    </div>

    <section class="glass-panel section">
      <h3>Provider 列表</h3>
      <div class="provider-list">
        <div v-for="p in builtinProviders" :key="p.id" class="provider-row">
          <div class="provider-info">
            <span class="p-label">{{ p.label }}</span>
            <n-tag size="small" :type="p.format === 'anthropic' ? 'warning' : 'info'" :bordered="false">
              {{ p.format === 'anthropic' ? 'Anthropic 兼容' : 'OpenAI 兼容' }}
            </n-tag>
            <n-tag v-if="p.builtin" size="small" :bordered="false">内置</n-tag>
            <span class="p-url">{{ p.base_url }}</span>
            <span class="p-key">{{ p.key_masked || '（未配置 Key）' }}</span>
          </div>
          <div class="provider-ops">
            <span v-if="testResults[p.id]" class="test-badge"
                  :class="{ ok: testResults[p.id].ok }">
              {{ testResults[p.id].ok ? `✓ ${testResults[p.id].latency_ms}ms` : `✗ ${testResults[p.id].error?.slice(0, 60)}` }}
            </span>
            <n-button size="tiny" :loading="testingId === p.id" @click="testProvider(p.id)">测试</n-button>
          </div>
        </div>
        <draggable
          v-model="customProviders"
          item-key="id"
          :disabled="false"
          handle=".drag-handle"
          @end="onDragEnd"
        >
          <template #item="{ element: p }">
            <div class="provider-row">
              <div class="provider-info">
                <span class="drag-handle" title="拖拽排序">☰</span>
                <span class="p-label">{{ p.label }}</span>
                <n-tag size="small" :type="p.format === 'anthropic' ? 'warning' : 'info'" :bordered="false">
                  {{ p.format === 'anthropic' ? 'Anthropic 兼容' : 'OpenAI 兼容' }}
                </n-tag>
                <n-tag v-if="p.builtin" size="small" :bordered="false">内置</n-tag>
                <span class="p-url">{{ p.base_url }}</span>
                <span class="p-key">{{ p.key_masked || '（未配置 Key）' }}</span>
              </div>
              <div class="provider-ops">
                <span v-if="testResults[p.id]" class="test-badge"
                      :class="{ ok: testResults[p.id].ok }">
                  {{ testResults[p.id].ok ? `✓ ${testResults[p.id].latency_ms}ms` : `✗ ${testResults[p.id].error?.slice(0, 60)}` }}
                </span>
                <n-button size="tiny" :loading="testingId === p.id" @click="testProvider(p.id)">测试</n-button>
                <n-button v-if="!p.builtin" size="tiny" @click="openProviderForm(p)">编辑</n-button>
                <n-popconfirm v-if="!p.builtin" @positive-click="removeProvider(p.id)">
                  <template #trigger><n-button size="tiny" type="error" quaternary>删</n-button></template>
                  确认删除 provider {{ p.id }}？
                </n-popconfirm>
              </div>
            </div>
          </template>
        </draggable>
      </div>
    </section>

    <section class="glass-panel section">
      <h3>任务路由表 <span class="hint">改完即生效，无须重启</span></h3>
      <table class="route-table">
        <thead>
          <tr><th>任务</th><th>model</th><th>max_tokens</th><th>thinking</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="(r, task) in routes" :key="task">
            <td class="mono">{{ task }}</td>
            <td>
              <n-select
                v-model:value="r.model"
                size="small"
                filterable
                :options="modelSelectOptions"
                style="min-width:220px"
                @update:value="(v: string) => onRouteModelChange(r, v)"
              />
            </td>
            <td><n-input-number v-model:value="r.max_tokens" size="small" :min="64" :max="32768" :show-button="false" style="width:90px" /></td>
            <td><n-switch v-model:value="r.thinking" size="small" /></td>
            <td class="route-ops">
              <n-button size="tiny" type="primary" secondary @click="saveRoute(task as string)">保存</n-button>
              <n-button size="tiny" :loading="testingId === `route:${task}`" @click="testRoute(task as string)">测试</n-button>
              <span v-if="testResults[`route:${task}`]" class="test-badge"
                    :class="{ ok: testResults[`route:${task}`].ok }">
                {{ testResults[`route:${task}`].ok ? '✓' : '✗' }}
              </span>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="fallback-chain">
        降级链：<template v-for="(to, from, i) in fallback" :key="from">
          <span v-if="i > 0" class="chain-sep"> ｜ </span>
          <span class="mono">{{ from }} → {{ to }}</span>
        </template>
      </div>
    </section>

    <section class="glass-panel section">
      <h3>凭证池状态</h3>
      <table class="route-table">
        <thead><tr><th>provider</th><th>key</th><th>状态</th><th>使用次数</th><th>最近错误</th></tr></thead>
        <tbody>
          <tr v-for="c in credentials" :key="`${c.provider}-${c.index}`">
            <td>{{ c.provider }}</td>
            <td class="mono">{{ c.key_masked }}</td>
            <td><n-tag size="small" :type="(stateColor[c.state] as any) || 'default'" :bordered="false">{{ c.state }}</n-tag></td>
            <td>{{ c.use_count }}</td>
            <td class="error-cell">{{ c.last_error || '—' }}</td>
          </tr>
          <tr v-if="!credentials.length"><td colspan="5" class="empty-cell">（凭证池为空）</td></tr>
        </tbody>
      </table>
    </section>

    <section class="glass-panel section">
      <h3>近 7 天用量
        <span class="hint" v-if="usage.total">
          共 {{ usage.total.calls || 0 }} 次调用 · {{ ((usage.total.tokens || 0) / 1000).toFixed(1) }}k tokens
          · ${{ (usage.total.cost || 0).toFixed(4) }}
        </span>
      </h3>
      <div ref="chartEl" class="usage-chart"></div>
    </section>

    <n-modal v-model:show="showProviderForm" preset="card"
             :title="isCreateProvider ? '新增自定义 Provider' : `编辑 · ${providerForm.id}`"
             style="width: min(560px, 94vw)">
      <n-form label-placement="left" label-width="110">
        <n-form-item label="id" v-if="isCreateProvider">
          <n-input v-model:value="providerForm.id" placeholder="如 my-anthropic" />
        </n-form-item>
        <n-form-item label="名称">
          <n-input v-model:value="providerForm.label" />
        </n-form-item>
        <n-form-item label="接口格式">
          <n-radio-group v-model:value="providerForm.format">
            <n-radio value="openai">OpenAI 兼容</n-radio>
            <n-radio value="anthropic">Anthropic 兼容</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item label="base_url">
          <n-input v-model:value="providerForm.base_url" placeholder="https://..." />
        </n-form-item>
        <n-form-item label="默认 model">
          <n-input v-model:value="providerForm.default_model" placeholder="如 claude-sonnet-4-6" />
        </n-form-item>
        <n-form-item label="API Key">
          <n-input v-model:value="providerForm.api_key" type="password" show-password-on="click"
                   placeholder="仅提交不回显" />
        </n-form-item>
      </n-form>
      <template #footer>
        <div style="display:flex; justify-content:flex-end; gap:10px">
          <n-button @click="showProviderForm = false">取消</n-button>
          <n-button type="primary" @click="saveProvider">保存并注册</n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.view-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 16px;
}
.view-header h2 { font-family: 'Noto Serif SC', serif; }

.section { padding: 16px 18px; margin-bottom: 16px; }
.section h3 { font-size: 15px; margin-bottom: 12px; color: var(--dendro); }
.hint { font-size: 12px; color: var(--moon-dim); font-weight: 400; margin-left: 10px; }

.provider-list { display: flex; flex-direction: column; gap: 8px; }
.provider-row {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; padding: 8px 10px; border-radius: 8px;
  border: 1px solid var(--glass-border);
  flex-wrap: wrap;
}
.provider-info { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.p-label { font-weight: 600; }
.p-url { font-size: 12px; color: var(--moon-dim); font-family: 'JetBrains Mono', monospace; }
.p-key { font-size: 12px; color: var(--wisdom); font-family: 'JetBrains Mono', monospace; }
.provider-ops { display: flex; align-items: center; gap: 6px; }

.drag-handle {
  cursor: grab;
  color: var(--moon-dim);
  font-size: 14px;
  user-select: none;
  padding: 0 4px;
  line-height: 1;
}
.drag-handle:active { cursor: grabbing; }

.test-badge { font-size: 12px; color: var(--alert); max-width: 260px; }
.test-badge.ok { color: var(--dendro); }

.route-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.route-table th {
  text-align: left; padding: 6px 8px; color: var(--moon-dim);
  border-bottom: 1px solid var(--glass-border); font-weight: 500;
}
.route-table td { padding: 6px 8px; border-bottom: 1px solid rgba(127, 214, 80, 0.08); }
.route-ops { display: flex; align-items: center; gap: 6px; }
.mono { font-family: 'JetBrains Mono', monospace; font-size: 12.5px; }
.error-cell { font-size: 12px; color: var(--alert); max-width: 280px; overflow: hidden; text-overflow: ellipsis; }
.empty-cell { text-align: center; color: var(--moon-dim); }

.fallback-chain { margin-top: 10px; font-size: 12.5px; color: var(--wisdom); }
.usage-chart { height: 260px; }

@media (max-width: 768px) {
  .route-table { display: block; overflow-x: auto; }
}
</style>
