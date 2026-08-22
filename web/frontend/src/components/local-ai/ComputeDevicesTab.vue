<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { NButton, NEmpty, NProgress, NTag, NSelect, NModal, NTabs, NTabPane, NSpin, useMessage } from 'naive-ui'
import { useLocalAiStore, type LocalDeployStatus } from '../../stores/localAi'

const store = useLocalAiStore()
const message = useMessage()

const showLogsModal = ref(false)
const logsLoading = ref(false)
const logsTopic = ref<'deploy' | 'device'>('device')
const logsLines = ref<string[]>([])
const showEnginePanel = ref(false)
const deployStatus = ref<LocalDeployStatus | null>(null)
const deployLoading = ref(false)
const engineBusy = ref('')
const selectedDevice = ref('')
const deviceList = ref<Array<{ id: string; name: string; kind: string; available: boolean }>>([])
const runtimeBackend = ref('')

async function loadDeployStatus() {
  try {
    deployStatus.value = await store.fetchLocalDeployStatus()
  } catch { /* 静默 */ }
}

async function loadDeviceList() {
  try {
    const data = await store.loadLocalDeployDevices()
    deviceList.value = data.devices
    selectedDevice.value = data.current
    runtimeBackend.value = data.runtime_backend
  } catch { /* 静默 */ }
}

async function loadLogs() {
  logsLoading.value = true
  try {
    logsLines.value = await store.loadLocalDeployLogs(logsTopic.value, 120)
  } catch (e: any) {
    message.error(e.message)
  } finally {
    logsLoading.value = false
  }
}

async function setDevice(id: string) {
  try {
    await store.setLocalDeployDevice(id)
    selectedDevice.value = id
    message.success('设备选择已保存，切换后需重启服务生效')
  } catch (e: any) {
    message.error(e.message)
  }
}

async function setMode(mode: 'local' | 'remote') {
  engineBusy.value = mode
  try {
    deployStatus.value = await store.setLocalDeployMode(mode)
    message.success(mode === 'local' ? '已切换到本地引擎' : '已切换到远程 API')
  } catch (e: any) {
    message.error(e.message)
  } finally {
    engineBusy.value = ''
  }
}

async function startEngine() {
  engineBusy.value = 'start'
  try {
    deployStatus.value = await store.startLocalDeployEngine()
    message.success('本地引擎已启动')
  } catch (e: any) {
    message.error(e.message)
  } finally {
    engineBusy.value = ''
  }
}

async function stopEngine() {
  engineBusy.value = 'stop'
  try {
    deployStatus.value = await store.stopLocalDeployEngine()
    message.success('本地引擎已停止')
  } catch (e: any) {
    message.error(e.message)
  } finally {
    engineBusy.value = ''
  }
}

function openLogs() {
  showLogsModal.value = true
  loadLogs()
}

function openEnginePanel() {
  showEnginePanel.value = true
  loadDeployStatus()
  loadDeviceList()
}

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
const loadPct = (device: { kind: string; stats?: { usage_pct?: number | null; utilization_pct?: number | null } | null }) => {
  if (device.kind === 'cpu') return typeof device.stats?.usage_pct === 'number' ? Math.min(device.stats.usage_pct, 100) : null
  if (device.kind === 'gpu') return typeof device.stats?.utilization_pct === 'number' ? Math.min(device.stats.utilization_pct, 100) : null
  if (device.kind === 'npu') return typeof device.stats?.utilization_pct === 'number' ? Math.min(device.stats.utilization_pct, 100) : null
  return null
}

const lastUpdated = ref<string | null>(null)
let timer: ReturnType<typeof setInterval> | null = null
async function refreshStats() {
  try {
    await store.refreshDevices()
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
      <n-button secondary size="small" @click="openEnginePanel">⚙ 引擎控制</n-button>
      <n-button secondary size="small" @click="openLogs">📋 检测日志</n-button>
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
            <n-progress type="line" :percentage="memoryUsagePct(device) ?? 0" :show-indicator="false" :color="(memoryUsagePct(device) ?? 0) > 80 ? '#d03050' : (memoryUsagePct(device) ?? 0) > 60 ? '#e8d5a3' : '#70c0e8'" />
            <div class="mem-detail">可用 {{ byteText(device.memory_available) }} / 共 {{ byteText(device.memory_total) }}</div>
          </template>
        </div>

        <!-- 规格 -->
        <div class="spec-block">
          <div class="block-title">规格</div>
          <div class="stat-row" v-if="device.kind === 'cpu'"><span>核数</span><b>{{ (device.stats as any)?.cores ?? '—' }}</b></div>
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

    <n-modal v-model:show="showLogsModal" preset="card" title="📋 设备检测日志" style="width: min(720px, 94vw)">
      <n-tabs v-model:value="logsTopic" type="line" @update:value="loadLogs">
        <n-tab-pane name="device" tab="算力设备" />
        <n-tab-pane name="deploy" tab="部署引擎" />
      </n-tabs>
      <div class="logs-toolbar">
        <n-button size="small" :loading="logsLoading" @click="loadLogs">刷新</n-button>
      </div>
      <n-spin :show="logsLoading">
        <pre class="logs-box">{{ logsLines.join('\n') || '暂无相关日志' }}</pre>
      </n-spin>
    </n-modal>

    <n-modal v-model:show="showEnginePanel" preset="card" title="⚙ 引擎控制" style="width: min(520px, 94vw)">
      <div class="engine-section">
        <h4>Embedding 引擎</h4>
        <div class="engine-status" v-if="deployStatus">
          <div class="stat-row">
            <span>模式</span>
            <n-tag :type="deployStatus.mode === 'local' ? 'success' : 'info'" round>{{ deployStatus.mode === 'local' ? '本地' : '远程 API' }}</n-tag>
          </div>
          <div class="stat-row">
            <span>引擎状态</span>
            <n-tag :type="deployStatus.engine_running ? 'success' : 'warning'" round>{{ deployStatus.engine_running ? '运行中' : '未启动' }}</n-tag>
          </div>
          <div class="stat-row" v-if="deployStatus.backend">
            <span>后端</span>
            <b>{{ deployStatus.backend }}</b>
          </div>
          <div class="stat-row" v-if="deployStatus.dimensions">
            <span>向量维度</span>
            <b>{{ deployStatus.dimensions }}</b>
          </div>
        </div>
        <div class="engine-ops">
          <n-button type="primary" :loading="engineBusy === 'start'" :disabled="deployStatus?.engine_running" @click="startEngine">启动本地引擎</n-button>
          <n-button type="warning" :loading="engineBusy === 'stop'" :disabled="!deployStatus?.engine_running" @click="stopEngine">停止本地引擎</n-button>
          <n-button :loading="engineBusy === 'local'" :disabled="deployStatus?.mode === 'local'" @click="setMode('local')">切换本地</n-button>
          <n-button :loading="engineBusy === 'remote'" :disabled="deployStatus?.mode === 'remote'" @click="setMode('remote')">切换远程</n-button>
        </div>
      </div>
      <div class="engine-section">
        <h4>算力设备选择</h4>
        <div class="stat-row" v-if="runtimeBackend">
          <span>运行时后端</span>
          <b>{{ runtimeBackend }}</b>
        </div>
        <n-select
          :value="selectedDevice"
          :options="deviceList.map(d => ({ label: `${d.name}（${d.kind}）${d.available ? '' : ' ✗ 不可用'}`, value: d.id, disabled: !d.available }))"
          @update:value="setDevice"
          placeholder="选择算力设备"
        />
        <p class="engine-hint">切换设备后需重启服务生效</p>
      </div>
    </n-modal>
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
.logs-toolbar { display: flex; justify-content: flex-end; margin: 8px 0; }
.logs-box {
  background: rgba(10, 20, 14, 0.85);
  border-radius: 8px;
  padding: 12px;
  font-size: 11px;
  font-family: 'JetBrains Mono', monospace;
  color: var(--moon-dim);
  max-height: 400px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}
.engine-section { margin-bottom: 18px; }
.engine-section h4 { font-size: 14px; color: var(--dendro); margin-bottom: 10px; }
.engine-status { margin-bottom: 12px; }
.engine-ops { display: flex; gap: 8px; flex-wrap: wrap; }
.engine-hint { font-size: 12px; color: var(--moon-dim); margin-top: 8px; }
</style>