<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NButton, NEmpty, NProgress, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'
import { localAiApi } from '../../api/localAi'

const store = useLocalAiStore()
const message = useMessage()

const KIND_TEXT: Record<string, string> = { cpu: 'CPU', gpu: 'GPU', npu: 'NPU' }
const STATE_TEXT: Record<string, string> = { available: '可用', degraded: '降级', unavailable: '不可用' }
const byteText = (value: number | null | undefined) => {
  if (value == null) return '—'
  if (value >= 1024 ** 3) return (value / 1024 ** 3).toFixed(2) + ' GB'
  if (value >= 1024 ** 2) return (value / 1024 ** 2).toFixed(1) + ' MB'
  if (value >= 1024) return (value / 1024).toFixed(1) + ' KB'
  return value + ' B'
}
const gHz = (hz: number | null | undefined) => hz == null ? '' : hz >= 1e9 ? (hz / 1e9).toFixed(2) + ' GHz' : (hz / 1e6).toFixed(0) + ' MHz'
const freqText = (mhz: number | null | undefined) => mhz == null ? '—' : mhz >= 1000 ? (mhz / 1000).toFixed(2) + ' GHz' : mhz + ' MHz'
const kindLabel = (device: { kind: string; architecture: string }) => {
  const kind = KIND_TEXT[device.kind] ?? device.kind
  return device.architecture && device.architecture !== 'unknown' ? `${kind} · ${device.architecture}` : kind
}
const stateMeta = (state: string) => ({ text: STATE_TEXT[state] ?? state, type: state === 'available' ? 'success' : state === 'degraded' ? 'warning' : 'error' } as const)

// 内存占用率 = 1 - 可用/总量（此前误用可用率标为占用率，语义颠倒）
const memoryUsagePct = (device: { memory_available?: number | null; memory_total?: number | null }) => {
  const total = device.memory_total
  if (!total || total <= 0) return null
  const available = device.memory_available ?? 0
  return Math.max(0, Math.min(100, Math.round((1 - available / total) * 100)))
}
const loadPct = (device: { kind: string; stats?: Record<string, unknown> | null }) => {
  if (device.kind === 'cpu') return typeof device.stats?.usage_pct === 'number' ? Math.min(device.stats.usage_pct, 100) : null
  if (device.kind === 'gpu') return typeof device.stats?.utilization_pct === 'number' ? Math.min(device.stats.utilization_pct, 100) : null
  if (device.kind === 'npu') return typeof device.stats?.utilization_pct === 'number' ? Math.min(device.stats.utilization_pct, 100) : null
  return null
}

const lastUpdated = ref<string | null>(null)
let timer: ReturnType<typeof setInterval> | null = null
async function refreshStats() {
  try {
    const devices = await localAiApi.loadDevices()
    for (const device of devices) store.upsertDevice(device)
    lastUpdated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  } catch { /* 轮询失败静默，下次重试 */ }
}
onMounted(() => {
  refreshStats()
  timer = setInterval(refreshStats, 5000)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})

async function rescan() {
  try { await store.rescan(); await refreshStats(); message.success('算力设备已重新扫描') } catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
}

const devices = computed(() => store.devices)
</script>

<template>
  <div class="devices-wrap">
    <div class="device-toolbar">
      <span class="toolbar-note">仅展示后端实际探测到的设备与运行提供程序</span>
      <span class="toolbar-time" :class="{ fresh: lastUpdated }">最后更新 {{ lastUpdated ?? '—' }} · 每 5s 自动刷新负载</span>
      <n-button :loading="store.rescanning" @click="rescan">重新扫描</n-button>
    </div>

    <div class="device-grid">
      <article v-for="device in devices" :key="device.id" class="glass-panel device-card">
        <!-- 头部：名称 / 类型 / 状态 -->
        <div class="device-head">
          <div class="head-name">
            <strong>{{ device.name }}</strong>
            <span>{{ kindLabel(device) }}</span>
          </div>
          <n-tag :type="stateMeta(device.state).type" round>{{ stateMeta(device.state).text }}</n-tag>
        </div>

        <!-- 实时负载 -->
        <div class="load-block">
          <div class="block-title">实时负载</div>
          <template v-if="loadPct(device) != null">
            <div class="stat-row">
              <span>{{ device.kind === 'npu' ? 'NPU 占用' : device.kind === 'gpu' ? 'GPU 占用' : 'CPU 占用' }}</span>
              <b>{{ loadPct(device) }}%</b>
            </div>
            <div class="load-bar"><span :style="{ width: loadPct(device) + '%' }"></span></div>
          </template>
          <div v-else class="no-load">该设备暂无可读取的实时负载数据</div>

          <template v-if="memoryUsagePct(device) != null">
            <div class="stat-row">
              <span>内存占用</span>
              <b>{{ memoryUsagePct(device) }}%</b>
            </div>
            <n-progress type="line" :percentage="memoryUsagePct(device)" :show-indicator="false" :color="memoryUsagePct(device) > 80 ? '#d03050' : memoryUsagePct(device) > 60 ? '#e8d5a3' : '#70c0e8'" />
            <div class="mem-detail">可用 {{ byteText(device.memory_available) }} / 共 {{ byteText(device.memory_total) }}</div>
          </template>
        </div>

        <!-- 规格 -->
        <div class="spec-block">
          <div class="block-title">规格</div>
          <div class="stat-row" v-if="device.kind === 'cpu'"><span>核数</span><b>{{ (device.stats as any)?.cores ?? device.cores ?? '—' }}</b></div>
          <div class="stat-row" v-if="device.kind === 'cpu' && (device.stats as any)?.freq_mhz"><span>频率</span><b>{{ freqText((device.stats as any)?.freq_mhz) }}</b></div>
          <div class="stat-row" v-if="device.kind === 'npu' && (device.stats as any)?.freq_hz"><span>频率</span><b>{{ gHz((device.stats as any)?.freq_hz) }}<template v-if="(device.stats as any)?.max_freq_hz"> / {{ gHz((device.stats as any)?.max_freq_hz) }}</template></b></div>
          <div class="stat-row" v-if="device.kind === 'gpu' && (device.stats as any)?.temperature_c"><span>温度</span><b>{{ (device.stats as any)?.temperature_c }} °C</b></div>
          <div class="stat-row" v-if="device.kind === 'gpu' && (device.stats as any)?.memory_total"><span>显存</span><b>可用 {{ byteText((device.stats as any)?.memory_available) }} / {{ byteText((device.stats as any)?.memory_total) }}</b></div>
          <div v-if="device.kind === 'npu' && !(device.stats as any)?.freq_hz" class="no-load">该 NPU 无 devfreq 频率节点，仅展示状态</div>
        </div>

        <!-- 运行提供程序 -->
        <div class="backend-block">
          <div class="block-title">运行提供程序</div>
          <div v-if="device.backends.length" class="backend-list">
            <n-tag v-for="backend in device.backends" :key="`${backend.runtime}:${backend.provider}`" size="small" :type="backend.healthy ? 'success' : 'warning'">{{ backend.runtime }} · {{ backend.provider }}</n-tag>
          </div>
          <div v-else class="no-load">未探测到可用提供程序</div>
        </div>
      </article>

      <n-empty v-if="!devices.length" description="尚未探测到算力设备，请点击「重新扫描」" style="grid-column: 1 / -1;" />
    </div>
  </div>
</template>

<style scoped>
.devices-wrap { display: flex; flex-direction: column; gap: 14px; }
.device-toolbar { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.toolbar-note { color: var(--moon-dim); font-size: 12px; }
.toolbar-time { color: var(--moon-dim); font-size: 12px; }
.toolbar-time.fresh { color: var(--moon-leaf); }
.device-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 14px; }
.device-card { padding: 16px; border-radius: 14px; display: flex; flex-direction: column; gap: 14px; }
.device-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.head-name strong, .head-name span { display: block; }
.head-name span { margin-top: 3px; color: var(--moon-dim); font-size: 12px; }
.block-title { color: var(--moon-dim); font-size: 11px; font-weight: 600; letter-spacing: .05em; margin-bottom: 8px; }
.load-block, .spec-block, .backend-block { border-top: 1px solid var(--moon-line); padding-top: 10px; }
.stat-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; color: var(--moon-dim); font-size: 12px; margin-bottom: 6px; }
.stat-row b { color: var(--text-2); font-weight: 600; }
.load-bar { height: 6px; border-radius: 6px; background: var(--moon-soft); overflow: hidden; margin-bottom: 12px; }
.load-bar span { display: block; height: 100%; border-radius: 6px; background: linear-gradient(90deg, #73d13d, #52c41a); transition: width .8s ease; }
.mem-detail { color: var(--moon-dim); font-size: 11px; margin-top: 4px; }
.no-load { color: var(--moon-dim); font-size: 12px; padding: 2px 0 6px; }
.backend-list { display: flex; flex-wrap: wrap; gap: 6px; }
</style>
