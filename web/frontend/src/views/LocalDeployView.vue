<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, h } from 'vue'
import { NButton, NTag, NSelect, NModal, NTabs, NTabPane, useMessage, type SelectOption } from 'naive-ui'
import { get, post } from '../api'
import { t } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'

const message = useMessage()

const activeTab = ref('deploy')

// ── 部署页：引擎状态 ──────────────────────────────────
const status = ref<any>(null)
const logs = ref<string[]>([])
const loading = ref(false)
let timer: number | null = null

const mode = computed(() => status.value?.mode ?? 'remote')
const running = computed(() => status.value?.engine_running ?? false)
const backend = computed(() => status.value?.backend ?? 'auto')
const apiConfigured = computed(() => status.value?.api_configured ?? false)

async function refreshStatus() {
  try { status.value = await get('/local-deploy/status') } catch { /* 向量库未就绪时静默 */ }
}
async function refreshLogs() {
  try { logs.value = await get('/local-deploy/logs?limit=80') } catch { /* 静默 */ }
}

async function setMode(m: string) {
  if (m === mode.value) return
  loading.value = true
  try {
    status.value = await post('/local-deploy/mode', { mode: m })
    message.success(t('localDeployView.switchSuccess'))
  } catch (e: any) {
    message.error(t('localDeployView.switchFailed') + (e?.message ? `：${e.message}` : ''))
  } finally {
    loading.value = false
  }
  refreshLogs()
}

async function startEngine() {
  loading.value = true
  try {
    status.value = await post('/local-deploy/start')
    message.success(t('localDeployView.startDone'))
  } catch (e: any) {
    message.error(t('localDeployView.startFailed') + (e?.message ? `：${e.message}` : ''))
  } finally {
    loading.value = false
  }
  refreshLogs()
}

async function stopEngine() {
  loading.value = true
  try {
    status.value = await post('/local-deploy/stop')
    message.success(t('localDeployView.stopDone'))
  } catch (e: any) {
    message.error(t('localDeployView.stopFailed') + (e?.message ? `：${e.message}` : ''))
  } finally {
    loading.value = false
  }
  refreshLogs()
}

// ── 算力设备检测 ─────────────────────────────────────
const devices = ref<any[]>([])
const currentDevice = ref('')
const runtimeBackend = ref('auto')
const deviceLogs = ref<string[]>([])
const devicesLoading = ref(false)
const showDeviceConfirm = ref(false)
const pendingDevice = ref<any>(null)

const deviceOptions = computed<SelectOption[]>(() =>
  devices.value.map(d => ({
    label: `${d.name} · ${d.model}`,
    value: d.id,
    disabled: !d.available,
    dev: d,
  })),
)

// 设备卡状态：使用中 / 空闲 / 不可用
function devStatusClass(d: any) {
  if (d.id === currentDevice.value) return 'on'
  return d.available ? 'idle' : 'off'
}
function devStatusText(d: any) {
  if (d.id === currentDevice.value) return t('localDeployView.deviceInUse')
  return d.available ? t('localDeployView.deviceIdle') : t('localDeployView.deviceUnavailable')
}
// 部署页设备下拉：两行渲染（名称 · 型号 / 描述）
function renderDeviceLabel(option: any) {
  return h('div', { class: 'device-option' }, [
    h('span', { class: 'device-option-name' }, option.label as string),
    option.dev?.desc ? h('span', { class: 'device-option-desc' }, option.dev.desc as string) : null,
  ])
}

async function refreshDevices() {
  devicesLoading.value = true
  try {
    const r = await get<any>('/local-deploy/devices')
    devices.value = r.devices || []
    currentDevice.value = r.current || ''
    runtimeBackend.value = r.runtime_backend || 'auto'
  } catch { /* 静默 */ } finally {
    devicesLoading.value = false
  }
}
async function refreshDeviceLogs() {
  try { deviceLogs.value = await get('/local-deploy/logs?limit=80&topic=device') } catch { /* 静默 */ }
}

function onDeviceChange(val: string) {
  if (!val || val === currentDevice.value) return
  const d = devices.value.find(x => x.id === val)
  if (!d?.available) return
  pendingDevice.value = d
  showDeviceConfirm.value = true
}

async function confirmDeviceSwitch() {
  const d = pendingDevice.value
  if (!d) return
  showDeviceConfirm.value = false
  devicesLoading.value = true
  try {
    await post('/local-deploy/device', { device: d.id })
    message.success(t('localDeployView.deviceSaved'))
    currentDevice.value = d.id
    message.warning(t('localDeployView.deviceRestarting'))
    // 重启服务：X-Confirm 头由 api.post 的 confirm 参数携带
    setTimeout(() => { post('/system/restart', {}, true).catch(() => {}) }, 1200)
  } catch (e: any) {
    message.error(e?.message || t('localDeployView.switchFailed'))
    await refreshDevices()
  } finally {
    devicesLoading.value = false
  }
}

onMounted(async () => {
  await Promise.all([refreshStatus(), refreshLogs(), refreshDevices(), refreshDeviceLogs()])
  timer = window.setInterval(() => { refreshStatus(); refreshLogs(); refreshDevices(); refreshDeviceLogs() }, 5000)
})
onBeforeUnmount(() => { if (timer) window.clearInterval(timer) })
</script>

<template>
  <div class="local-deploy-view">
    <div class="view-header">
      <h2>🖥️ {{ t('localDeployView.title') }}</h2>
    </div>
    <p class="view-sub">{{ t('localDeployView.subtitle') }}</p>

    <n-tabs type="line" animated display-directive="show" v-model:value="activeTab">
      <!-- 部署 -->
      <n-tab-pane name="deploy" :tab="t('localDeployView.tabDeploy')">
        <Tilt3D :max-x="4" :max-y="6">
        <section class="glass-panel section engine-section">
          <h3 class="section-title">⚙️ {{ t('localDeployView.engine') }}</h3>

          <div class="engine-card" :class="{ active: mode === 'remote' }"
               :aria-disabled="loading" @click="setMode('remote')">
            <div class="radio-dot" :class="{ on: mode === 'remote' }"></div>
            <div class="engine-info">
              <div class="engine-title">☁️ {{ t('localDeployView.apiMode') }}</div>
              <div class="engine-desc">{{ t('localDeployView.apiDesc') }}</div>
              <n-tag :type="apiConfigured ? 'success' : 'warning'" size="small">
                {{ apiConfigured ? t('localDeployView.apiConfigured') : t('localDeployView.apiNotConfigured') }}
              </n-tag>
            </div>
          </div>

          <div class="engine-card" :class="{ active: mode === 'local' }"
               :aria-disabled="loading" @click="setMode('local')">
            <div class="radio-dot" :class="{ on: mode === 'local' }"></div>
            <div class="engine-info">
              <div class="engine-title">🖥️ {{ t('localDeployView.localMode') }}</div>
              <div class="engine-desc">{{ t('localDeployView.localDesc') }}</div>
              <div class="local-controls">
                <n-tag :type="running ? 'success' : 'default'" size="small">
                  {{ running ? t('localDeployView.localRunning') : t('localDeployView.localStopped') }}
                </n-tag>
                <span v-if="mode === 'local'" class="backend-row">
                  <span class="backend-hint">
                    {{ backend === 'cpu' ? t('localDeployView.backendCpu') : t('localDeployView.backendNpu') }}
                  </span>
                  <span class="device-pick">
                    <span class="device-pick-label">{{ t('localDeployView.devicePick') }}</span>
                    <n-select
                      :value="currentDevice"
                      :options="deviceOptions"
                      :loading="devicesLoading"
                      :disabled="devicesLoading"
                      size="small"
                      placeholder="CPU / NPU"
                      class="device-pick-select"
                      :render-label="renderDeviceLabel"
                      @update:value="onDeviceChange"
                    />
                  </span>
                </span>
                <n-button v-if="mode === 'local'" size="small" type="primary"
                          :loading="loading" :disabled="running" @click.stop="startEngine">
                  ▶ {{ t('localDeployView.startBtn') }}
                </n-button>
                <n-button v-if="mode === 'local'" size="small" type="warning"
                          :loading="loading" :disabled="!running" @click.stop="stopEngine">
                  ⏹ {{ t('localDeployView.stopBtn') }}
                </n-button>
              </div>
              <p v-if="mode === 'local' && !running" class="must-start">
                ⚠️ {{ t('localDeployView.mustStartFirst') }}
              </p>
            </div>
          </div>
        </section>
        </Tilt3D>

        <Tilt3D :max-x="4" :max-y="6">
        <section class="glass-panel section log-section">
          <h3 class="section-title">📜 {{ t('localDeployView.logs') }}</h3>
          <div class="log-box">
            <div v-for="(ln, i) in logs" :key="i" class="log-line">{{ ln }}</div>
            <div v-if="!logs.length" class="log-empty">{{ t('localDeployView.noLogs') }}</div>
          </div>
        </section>
        </Tilt3D>
      </n-tab-pane>

      <!-- 算力设备检测 -->
      <n-tab-pane name="devices" :tab="t('localDeployView.tabDevices')">
        <Tilt3D :max-x="4" :max-y="6">
        <section class="glass-panel section device-section">
          <h3 class="section-title">🔍 {{ t('localDeployView.devicesTitle') }}</h3>
          <p class="view-sub">{{ t('localDeployView.devicesDesc') }}</p>

          <div class="device-list">
            <div v-for="d in devices" :key="d.id"
                 class="device-card" :class="{ available: d.available, active: d.id === currentDevice }">
              <div class="dev-head">
                <span class="dev-name">{{ d.name }}</span>
                <span class="dev-status" :class="devStatusClass(d)">
                  <span class="status-dot"></span>
                  {{ devStatusText(d) }}
                </span>
              </div>
              <div class="dev-model">{{ d.model }}</div>

              <!-- CPU：性能数据 + 实时占用 -->
              <div v-if="d.id === 'cpu' && d.stats" class="dev-stats">
                <div class="stat-line">
                  {{ t('localDeployView.cores') }}: {{ d.stats.cores ?? '—' }}
                  <span class="stat-sep">·</span>
                  {{ t('localDeployView.freq') }}: {{ d.stats.freq_mhz ?? '—' }} MHz
                </div>
                <div class="usage-bar">
                  <div class="usage-fill" :style="{ width: (d.stats.usage_pct ?? 0) + '%' }"></div>
                </div>
                <div class="stat-line usage-text">
                  {{ t('localDeployView.usage') }}: {{ d.stats.usage_pct ?? '—' }}%
                </div>
              </div>

              <!-- NPU：算力 + 常驻流状态 + 最近推理 -->
              <div v-else-if="d.id === 'npu' && d.stats" class="dev-stats">
                <div class="stat-line">
                  3 TOPS INT8
                  <span class="stat-sep">·</span>
                  {{ d.stats.resident ? t('localDeployView.npuResident') : t('localDeployView.npuNotResident') }}
                </div>
                <div class="stat-line">
                  {{ t('localDeployView.lastCall') }}: {{ d.stats.last_call_ms != null ? d.stats.last_call_ms + ' ms' : '—' }}
                  <span class="stat-sep">·</span>
                  {{ t('localDeployView.callCount') }}: {{ d.stats.calls ?? 0 }}
                </div>
              </div>
            </div>
          </div>

          <div class="runtime-hint">
            {{ t('localDeployView.runtimeBackend') }}: <code>{{ runtimeBackend }}</code>
            <span class="restart-tag">{{ t('localDeployView.restartRequired') }}</span>
          </div>
        </section>
        </Tilt3D>

        <Tilt3D :max-x="4" :max-y="6">
        <section class="glass-panel section log-section">
          <h3 class="section-title">📜 {{ t('localDeployView.deviceLogs') }}</h3>
          <div class="log-box">
            <div v-for="(ln, i) in deviceLogs" :key="i" class="log-line">{{ ln }}</div>
            <div v-if="!deviceLogs.length" class="log-empty">{{ t('localDeployView.noLogs') }}</div>
          </div>
        </section>
        </Tilt3D>
      </n-tab-pane>
    </n-tabs>

    <!-- 设备切换确认弹窗 -->
    <n-modal
      v-model:show="showDeviceConfirm"
      preset="dialog"
      type="warning"
      :title="t('localDeployView.switchDeviceConfirmTitle')"
      :content="t('localDeployView.switchDeviceConfirmDesc')(pendingDevice?.name || '')"
      :positive-text="t('ok')"
      :negative-text="t('cancel')"
      @positive-click="confirmDeviceSwitch"
      @negative-click="showDeviceConfirm = false"
    />
  </div>
</template>

<style scoped>
.local-deploy-view {
  padding: 20px 24px;
  max-width: 880px;
  margin: 0 auto;
}
.view-header h2 {
  margin: 0;
  font-family: 'Noto Serif SC', serif;
  font-weight: 700;
}
.view-sub {
  margin: 6px 0 18px;
  color: var(--moon-dim);
  font-size: 13px;
  opacity: 0.75;
}
.section {
  padding: 18px 20px;
  margin-bottom: 18px;
  border-radius: 14px;
}
.section-title {
  margin: 0 0 14px;
  font-size: 15px;
  color: var(--dendro, #7fd650);
}

.engine-card {
  display: flex;
  gap: 14px;
  align-items: flex-start;
  padding: 14px 16px;
  border: 1px solid var(--glass-border);
  border-radius: 12px;
  margin-bottom: 12px;
  cursor: pointer;
  transition: border-color 0.25s, background 0.25s, transform 0.25s;
}
.engine-card:hover {
  border-color: rgba(127, 214, 80, 0.45);
  transform: translateX(3px);
}
.engine-card.active {
  border-color: var(--dendro, #7fd650);
  background: linear-gradient(90deg, rgba(127, 214, 80, 0.10), rgba(127, 214, 80, 0.02));
}
.engine-card[aria-disabled='true'] { cursor: not-allowed; opacity: 0.7; }

.radio-dot {
  width: 16px;
  height: 16px;
  margin-top: 3px;
  border-radius: 50%;
  border: 2px solid rgba(232, 213, 163, 0.45);
  flex-shrink: 0;
  transition: border-color 0.2s, box-shadow 0.2s;
}
.radio-dot.on {
  border-color: var(--dendro, #7fd650);
  box-shadow: inset 0 0 0 3px rgba(15, 31, 23, 0.9), 0 0 0 1px var(--dendro, #7fd650);
}

.engine-info { flex: 1; }
.engine-title { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
.engine-desc { font-size: 12px; color: var(--moon-dim); margin-bottom: 8px; line-height: 1.6; opacity: 0.8; }

.local-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.backend-hint {
  font-size: 12px;
  color: var(--moon-dim);
  opacity: 0.8;
}
.backend-row {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.must-start {
  margin: 8px 0 0;
  font-size: 12px;
  color: #e6c26a;
}

.log-box {
  background: rgba(8, 18, 13, 0.72);
  border: 1px solid var(--glass-border);
  border-radius: 10px;
  padding: 10px 12px;
  height: 260px;
  overflow-y: auto;
  font-family: 'JetBrains Mono', ui-monospace, monospace;
  font-size: 11.5px;
  line-height: 1.7;
}
.log-line {
  color: rgba(190, 214, 200, 0.85);
  white-space: pre-wrap;
  word-break: break-all;
}
.log-empty {
  color: rgba(190, 214, 200, 0.35);
  text-align: center;
  padding-top: 90px;
  font-size: 12px;
}

/* 算力设备检测 */
.device-section .view-sub {
  margin-bottom: 14px;
}
.device-pick {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--moon-dim);
  opacity: 0.9;
}
.device-pick-label {
  white-space: nowrap;
}
.device-pick-select {
  min-width: 150px;
}
.device-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
  gap: 12px;
}
.device-card {
  border: 1px solid var(--glass-border);
  border-radius: 12px;
  padding: 12px 14px;
  background: rgba(255, 255, 255, 0.02);
  transition: border-color 0.25s, background 0.25s;
}
.device-card.available {
  border-color: rgba(127, 214, 80, 0.28);
}
.device-card.active {
  border-color: var(--dendro, #7fd650);
  background: linear-gradient(90deg, rgba(127, 214, 80, 0.10), rgba(127, 214, 80, 0.02));
}
.dev-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.dev-name {
  font-size: 14px;
  font-weight: 600;
}
.dev-model {
  font-size: 12.5px;
  color: var(--moon-dim);
  margin-bottom: 6px;
  opacity: 0.9;
  word-break: break-all;
}
.dev-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  white-space: nowrap;
}
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.22);
  flex-shrink: 0;
}
.dev-status.on {
  color: var(--dendro, #7fd650);
}
.dev-status.on .status-dot {
  background: var(--dendro, #7fd650);
  box-shadow: 0 0 6px rgba(127, 214, 80, 0.6);
}
.dev-status.idle {
  color: var(--moon-dim);
  opacity: 0.85;
}
.dev-status.idle .status-dot {
  background: rgba(127, 214, 80, 0.45);
}
.dev-status.off {
  color: rgba(255, 255, 255, 0.35);
}

/* 设备卡性能 / 占用数据 */
.dev-stats {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed rgba(255, 255, 255, 0.09);
}
.stat-line {
  font-size: 11.5px;
  color: var(--moon-dim);
  opacity: 0.85;
  line-height: 1.7;
  word-break: break-all;
}
.stat-sep {
  margin: 0 4px;
  opacity: 0.5;
}
.usage-bar {
  height: 6px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.08);
  overflow: hidden;
  margin: 6px 0 2px;
}
.usage-fill {
  height: 100%;
  border-radius: 3px;
  background: linear-gradient(90deg, rgba(127, 214, 80, 0.5), var(--dendro, #7fd650));
  transition: width 0.6s ease;
}
.usage-text {
  font-size: 11px;
}

/* 部署页设备下拉：两行渲染（名称 · 型号 / 描述） */
.device-option {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
  padding: 2px 0;
}
.device-option-name {
  font-size: 13px;
}
.device-option-desc {
  font-size: 11px;
  color: var(--moon-dim);
  opacity: 0.7;
}
.runtime-hint {
  margin-top: 16px;
  font-size: 12px;
  color: var(--moon-dim);
  opacity: 0.85;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.runtime-hint code {
  background: rgba(8, 18, 13, 0.72);
  border: 1px solid var(--glass-border);
  border-radius: 6px;
  padding: 2px 8px;
  color: var(--dendro, #7fd650);
}
.restart-tag {
  border: 1px solid #e6c26a;
  color: #e6c26a;
  border-radius: 6px;
  padding: 1px 8px;
  font-size: 11px;
}
</style>
