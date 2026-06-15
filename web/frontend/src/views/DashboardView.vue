<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useMessage } from 'naive-ui'
import { get } from '../api'
import Tilt3D from '../components/fx/Tilt3D.vue'
import * as echarts from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const message = useMessage()

const stats = ref({ messages: 0, cost: 0, toolCalls: 0, memories: 0 })
const system = ref<any>({})
const audit = ref<any[]>([])
const permissionMode = ref('')
const costChartEl = ref<HTMLElement | null>(null)
const toolChartEl = ref<HTMLElement | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  await loadAll()
  timer = setInterval(loadSystem, 5000)
})

onBeforeUnmount(() => { if (timer) clearInterval(timer) })

async function loadAll() {
  try {
    const [today, usage, auditRows, perm] = await Promise.all([
      get('/insight/today'),
      get('/models/usage?days=7'),
      get<any[]>('/system/audit?limit=10'),
      get('/system/permission-mode'),
    ])
    stats.value.messages = today.stats.conversations
    stats.value.toolCalls = today.stats.tool_calls
    stats.value.memories = today.stats.memories
    permissionMode.value = perm.mode
    const todayStr = new Date().toLocaleDateString('sv-SE')
    stats.value.cost = usage.series
      .filter((s: any) => s.day === todayStr)
      .reduce((sum: number, s: any) => sum + (s.cost_usd || 0), 0)
    audit.value = auditRows
    await nextTick()
    renderCostChart(usage.series)
    renderToolChart()
    loadSystem()
  } catch (e: any) {
    message.error(e.message)
  }
}

async function loadSystem() {
  try { system.value = await get('/health/system') } catch { /* */ }
}

function renderCostChart(series: any[]) {
  if (!costChartEl.value) return
  const days = [...new Set(series.map(s => s.day))].sort()
  const data = days.map(d => series.filter(s => s.day === d)
    .reduce((sum, s) => sum + (s.cost_usd || 0), 0))
  echarts.init(costChartEl.value).setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 16, top: 16, bottom: 24 },
    xAxis: { type: 'category', data: days, axisLabel: { color: '#9ca3af', fontSize: 10 } },
    yAxis: { type: 'value', axisLabel: { color: '#9ca3af', formatter: '${value}' },
             splitLine: { lineStyle: { color: 'rgba(127,214,80,.08)' } } },
    series: [{ type: 'line', smooth: true, areaStyle: { opacity: 0.25 }, color: '#7fd650', data }],
  })
}

async function renderToolChart() {
  if (!toolChartEl.value) return
  try {
    const snap = await get('/system/metrics')
    const counters = snap.counters || snap
    const toolCounts: Array<[string, number]> = []
    for (const [k, v] of Object.entries<any>(counters)) {
      const m = k.match(/^tool_execute\.(.+)\.success$/)
      if (m) toolCounts.push([m[1], Number(v)])
    }
    toolCounts.sort((a, b) => b[1] - a[1])
    const top = toolCounts.slice(0, 10).reverse()
    echarts.init(toolChartEl.value).setOption({
      tooltip: {},
      grid: { left: 140, right: 20, top: 10, bottom: 24 },
      xAxis: { type: 'value', axisLabel: { color: '#9ca3af' },
               splitLine: { lineStyle: { color: 'rgba(127,214,80,.08)' } } },
      yAxis: { type: 'category', data: top.map(t => t[0]),
               axisLabel: { color: '#f2f7ee', fontSize: 10, fontFamily: 'JetBrains Mono' } },
      series: [{ type: 'bar', color: '#e8d5a3', data: top.map(t => t[1]), barWidth: 12 }],
    })
  } catch { /* */ }
}

function pct(used: number, total: number): number {
  return total ? Math.round((used / total) * 100) : 0
}

function gb(bytes: number): string {
  return (bytes / 1024 / 1024 / 1024).toFixed(1)
}
</script>

<template>
  <div class="dashboard-view">
    <h2 class="view-title">📊 仪表盘</h2>

    <div v-if="permissionMode === 'bypass'" class="bypass-warning">
      ⚠ 当前权限模式为 BYPASS — 所有工具不经确认直接执行，存在安全风险！
    </div>

    <div class="stat-grid">
      <Tilt3D><div class="stat-card glass-panel">
        <span class="stat-num">{{ stats.messages }}</span><span class="stat-label">今日对话轮数</span>
      </div></Tilt3D>
      <Tilt3D><div class="stat-card glass-panel">
        <span class="stat-num">${{ stats.cost.toFixed(4) }}</span><span class="stat-label">今日成本</span>
      </div></Tilt3D>
      <Tilt3D><div class="stat-card glass-panel">
        <span class="stat-num">{{ stats.toolCalls }}</span><span class="stat-label">今日工具调用</span>
      </div></Tilt3D>
      <Tilt3D><div class="stat-card glass-panel">
        <span class="stat-num">{{ stats.memories }}</span><span class="stat-label">今日新增记忆</span>
      </div></Tilt3D>
    </div>

    <div class="chart-row">
      <div class="glass-panel chart-box">
        <h4>7 天成本</h4>
        <div ref="costChartEl" class="chart"></div>
      </div>
      <div class="glass-panel chart-box">
        <h4>工具调用 Top10</h4>
        <div ref="toolChartEl" class="chart"></div>
      </div>
    </div>

    <div class="chart-row">
      <div class="glass-panel chart-box monitor">
        <h4>{{ system.platform === 'Windows' ? 'Windows' : 'Orange Pi' }} 系统监控 <span class="hint">5s 轮询</span></h4>
        <div class="monitor-grid">
          <div class="m-item">
            <span class="m-label">负载</span>
            <span class="m-value mono">{{ (system.load || []).map((l: number) => l.toFixed(2)).join(' / ') || '—' }}</span>
          </div>
          <div class="m-item">
            <span class="m-label">内存</span>
            <span class="m-value mono">
              {{ system.mem_total ? `${pct(system.mem_total - system.mem_available, system.mem_total)}% · 可用 ${gb(system.mem_available)}G` : '—' }}
            </span>
          </div>
          <div class="m-item">
            <span class="m-label">磁盘</span>
            <span class="m-value mono">
              {{ system.disk_total ? `余 ${gb(system.disk_free)}G / ${gb(system.disk_total)}G` : '—' }}
            </span>
          </div>
          <div class="m-item" v-for="t in (system.temperatures || []).slice(0, 4)" :key="t.zone">
            <span class="m-label">🌡 {{ t.zone }}</span>
            <span class="m-value mono" :class="{ hot: t.temp_c > 70 }">{{ t.temp_c }}°C</span>
          </div>
          <div class="m-item">
            <span class="m-label">进程内存</span>
            <span class="m-value mono">{{ system.process_rss ? gb(system.process_rss) + 'G' : '—' }}</span>
          </div>
        </div>
      </div>
      <div class="glass-panel chart-box">
        <h4>最近审计</h4>
        <div class="audit-list">
          <div v-for="a in audit" :key="a.id" class="audit-row">
            <span class="a-time mono">{{ new Date(a.timestamp * 1000).toLocaleTimeString('zh-CN') }}</span>
            <span class="a-type">{{ a.event_type }}</span>
            <span class="a-detail">{{ a.detail }}</span>
          </div>
          <div v-if="!audit.length" class="empty-hint">（暂无）</div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.view-title { font-family: 'Noto Serif SC', serif; margin-bottom: 14px; }

.bypass-warning {
  background: rgba(217, 106, 95, 0.18);
  border: 1px solid var(--alert);
  border-radius: 10px;
  padding: 10px 16px;
  color: var(--alert);
  font-weight: 600;
  margin-bottom: 14px;
}

.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}

.stat-card {
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  text-align: center;
}
.stat-num {
  font-size: 28px; font-weight: 700; color: var(--dendro);
  font-family: 'JetBrains Mono', monospace;
}
.stat-label { font-size: 12px; color: var(--moon-dim); }

.chart-row { display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 14px; }
.chart-box { flex: 1; min-width: 300px; padding: 14px 16px; }
.chart-box h4 { font-size: 13px; color: var(--dendro); margin-bottom: 10px; }
.hint { font-size: 11px; color: var(--moon-dim); font-weight: 400; }
.chart { height: 220px; }

.monitor-grid { display: grid; grid-columns: 1fr 1fr; gap: 10px; }
.m-item {
  display: flex; flex-direction: column; gap: 2px;
  padding: 8px 10px; border-radius: 8px;
  background: rgba(15, 31, 23, 0.4);
}
.m-label { font-size: 11px; color: var(--moon-dim); }
.m-value { font-size: 14px; }
.m-value.hot { color: var(--alert); }
.mono { font-family: 'JetBrains Mono', monospace; }

.audit-list { display: flex; flex-direction: column; gap: 4px; max-height: 240px; overflow-y: auto; }
.audit-row {
  display: flex; gap: 8px; font-size: 12px;
  padding: 3px 0; border-bottom: 1px solid rgba(127, 214, 80, 0.05);
}
.a-time { color: var(--moon-dim); flex-shrink: 0; font-size: 11px; }
.a-type { color: var(--wisdom); flex-shrink: 0; }
.a-detail { color: var(--moon-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.empty-hint { color: var(--moon-dim); font-size: 12px; }
</style>
