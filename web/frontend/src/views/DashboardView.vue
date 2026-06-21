<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useMessage, NButton } from 'naive-ui'
import { get, put } from '../api'
import Tilt3D from '../components/fx/Tilt3D.vue'
import * as echarts from 'echarts/core'
import { LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const message = useMessage()

const stats = ref({ messages: 0, cost: 0, toolCalls: 0, memories: 0 })
const system = ref<any>({})
const monitorEnabled = ref(false)
const audit = ref<any[]>([])
const permissionMode = ref('')
const costChartEl = ref<HTMLElement | null>(null)
const toolChartEl = ref<HTMLElement | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  await loadMonitorConfig()
  await loadAll()
})

onBeforeUnmount(() => { stopPolling() })

async function loadMonitorConfig() {
  try {
    const cfg = await get('/system/config')
    monitorEnabled.value = !!cfg?.dashboard?.system_monitor_enabled
  } catch { /* */ }
}

function startPolling() {
  if (timer) clearInterval(timer)
  timer = setInterval(loadSystem, 5000)
}

function stopPolling() {
  if (timer) { clearInterval(timer); timer = null }
}

async function enableMonitor() {
  monitorEnabled.value = true
  try {
    await put('/system/config', { path: 'dashboard.system_monitor_enabled', value: true })
  } catch { /* */ }
  await loadSystem()
  startPolling()
}

async function disableMonitor() {
  monitorEnabled.value = false
  stopPolling()
  system.value = {}
  try {
    await put('/system/config', { path: 'dashboard.system_monitor_enabled', value: false })
  } catch { /* */ }
}

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
    if (monitorEnabled.value) {
      loadSystem()
      startPolling()
    }
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

function mb(bytes: number): string {
  return (bytes / 1024 / 1024).toFixed(0)
}

function uptimeFmt(s: number): string {
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  return d > 0 ? `${d}天${h}时${m}分` : h > 0 ? `${h}时${m}分` : `${m}分`
}

const platformLabel: Record<string, string> = {
  Windows: 'Windows',
  Linux: 'Linux',
  Darwin: 'macOS',
}

function diskLabel(d: any): string {
  if (system.value.platform === 'Windows') return d.mountpoint
  return d.mountpoint === '/' ? '/' : d.mountpoint.split('/').pop() || d.mountpoint
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
        <div class="section-header">
          <h4>{{ platformLabel[system.platform] || system.platform || '系统' }} 监控 <span v-if="monitorEnabled" class="hint">5s 轮询</span></h4>
          <NButton v-if="!monitorEnabled" size="small" type="primary" @click="enableMonitor">开启监控</NButton>
          <NButton v-else size="small" @click="disableMonitor">关闭监控</NButton>
        </div>
        <div v-if="monitorEnabled" class="monitor-grid">
          <!-- CPU -->
          <div class="m-item">
            <span class="m-label">CPU</span>
            <span class="m-value mono">
              {{ system.cpu_percent != null ? system.cpu_percent + '%' : '—' }}
              <span v-if="system.load" class="m-sub">负载 {{ system.load.map((l: number) => l.toFixed(2)).join(' / ') }}</span>
            </span>
            <span v-if="system.cpu_count" class="m-sub">{{ system.cpu_count_physical || system.cpu_count }} 核</span>
          </div>
          <!-- 内存 -->
          <div class="m-item">
            <span class="m-label">内存</span>
            <span class="m-value mono">
              {{ system.mem_total ? `${system.mem_percent}% · 可用 ${gb(system.mem_available)}G / ${gb(system.mem_total)}G` : '—' }}
            </span>
            <div v-if="system.mem_total" class="m-bar">
              <div class="m-bar-fill" :style="{ width: system.mem_percent + '%' }" :class="{ warn: system.mem_percent > 85 }"></div>
            </div>
          </div>
          <!-- 交换区 -->
          <div class="m-item" v-if="system.swap_total">
            <span class="m-label">交换区</span>
            <span class="m-value mono">{{ system.swap_percent }}% · {{ gb(system.swap_used) }}G / {{ gb(system.swap_total) }}G</span>
          </div>
          <!-- 磁盘（所有分区）-->
          <div class="m-item m-item-wide" v-if="system.disks?.length">
            <span class="m-label">磁盘</span>
            <div class="disk-list">
              <div v-for="d in system.disks" :key="d.mountpoint" class="disk-row">
                <span class="disk-name mono">{{ diskLabel(d) }}</span>
                <div class="m-bar">
                  <div class="m-bar-fill" :style="{ width: d.percent + '%' }" :class="{ warn: d.percent > 90 }"></div>
                </div>
                <span class="disk-info mono">{{ d.percent }}% · 余 {{ gb(d.free) }}G / {{ gb(d.total) }}G</span>
              </div>
            </div>
          </div>
          <!-- 温度 -->
          <div class="m-item" v-if="system.temperatures?.length">
            <span class="m-label">温度</span>
            <div class="temp-list">
              <div v-for="t in system.temperatures.slice(0, 6)" :key="t.label" class="temp-row">
                <span class="temp-label">{{ t.label }}</span>
                <span class="m-value mono" :class="{ hot: t.current > 70, warm: t.current > 55 && t.current <= 70 }">{{ t.current?.toFixed(0) }}°C</span>
              </div>
            </div>
          </div>
          <!-- 网络 -->
          <div class="m-item" v-if="system.net_bytes_recv != null">
            <span class="m-label">网络 I/O</span>
            <span class="m-value mono">↓ {{ mb(system.net_bytes_recv) }}MB · ↑ {{ mb(system.net_bytes_sent) }}MB</span>
          </div>
          <!-- 进程 -->
          <div class="m-item">
            <span class="m-label">进程内存</span>
            <span class="m-value mono">{{ system.process_rss ? mb(system.process_rss) + 'MB' : '—' }}</span>
          </div>
          <!-- 运行时间 -->
          <div class="m-item" v-if="system.uptime">
            <span class="m-label">运行时间</span>
            <span class="m-value mono">{{ uptimeFmt(system.uptime) }}</span>
          </div>
          <!-- 电池 -->
          <div class="m-item" v-if="system.battery_percent != null">
            <span class="m-label">电池</span>
            <span class="m-value mono" :class="{ warn: system.battery_percent < 20 }">{{ system.battery_percent }}% {{ system.battery_plugged ? '充电中' : '' }}</span>
          </div>
        </div>
        <div v-else class="monitor-disabled-hint">
          系统监控已关闭，点击"开启监控"查看系统资源使用情况
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

.section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.section-header h4 { margin-bottom: 0; }
.monitor-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.m-item-wide { grid-column: 1 / -1; }
.monitor-disabled-hint { color: var(--moon-dim); font-size: 12px; padding: 24px 10px; text-align: center; }
.m-item {
  display: flex; flex-direction: column; gap: 2px;
  padding: 8px 10px; border-radius: 8px;
  background: rgba(15, 31, 23, 0.4);
}
.m-label { font-size: 11px; color: var(--moon-dim); }
.m-value { font-size: 14px; }
.m-sub { font-size: 11px; color: var(--moon-dim); }
.m-value.hot { color: var(--alert); }
.m-value.warm { color: #e8a838; }
.m-value.warn { color: #e8a838; }

.m-bar {
  height: 4px; border-radius: 2px;
  background: rgba(127, 214, 80, 0.1);
  margin-top: 4px; overflow: hidden;
}
.m-bar-fill {
  height: 100%; border-radius: 2px;
  background: var(--dendro);
  transition: width 0.5s ease;
}
.m-bar-fill.warn { background: #e8a838; }

.disk-list { display: flex; flex-direction: column; gap: 6px; margin-top: 4px; }
.disk-row { display: flex; align-items: center; gap: 8px; }
.disk-name { font-size: 12px; min-width: 50px; color: var(--wisdom); }
.disk-row .m-bar { flex: 1; margin: 0; }
.disk-info { font-size: 11px; color: var(--moon-dim); white-space: nowrap; }

.temp-list { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 4px; }
.temp-row { display: flex; align-items: center; gap: 4px; }
.temp-label { font-size: 11px; color: var(--moon-dim); }
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
