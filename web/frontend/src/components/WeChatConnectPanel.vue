<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { NButton, NTag, NSpin, NResult, NPopconfirm, useMessage } from 'naive-ui'
import { get, post } from '../api'
import { t } from '../i18n'
import Tilt3D from './fx/Tilt3D.vue'

/**
 * 微信 Bot 连接面板
 *
 * 状态机：
 *   checking       — 页面加载，正在检查连接状态
 *   idle           — 未连接，显示「连接微信」按钮
 *   loading_qrcode — 正在获取二维码
 *   qrcode         — 显示二维码，轮询扫码状态
 *   scaned         — 已扫码，提示「请在手机上确认」
 *   confirmed      — 扫码成功，显示「测试连接」按钮
 *   testing        — 测试连接中
 *   connected      — 已连接，显示「断开」按钮
 *   expired        — 二维码过期，显示「重新获取」按钮
 *   error          — 错误，显示错误信息和「重试」按钮
 *
 * API 契约（后端 /api/v1/wechat/*）：
 *   GET  /wechat/status              → { connected: boolean }
 *   POST /wechat/qrcode              → { qrcode_id: string, qrcode_url: string }
 *   GET  /wechat/qrcode-status?qrcode_id=xxx → { status: 'wait'|'scaned'|'confirmed'|'expired' }
 *   POST /wechat/test                → 测试连接
 *   POST /wechat/start               → 启动消息轮询
 *   POST /wechat/stop                → 停止并断开
 */

type State =
  | 'checking'
  | 'idle'
  | 'loading_qrcode'
  | 'qrcode'
  | 'scaned'
  | 'confirmed'
  | 'testing'
  | 'connected'
  | 'expired'
  | 'error'

const message = useMessage()

const state = ref<State>('checking')
const qrcodeUrl = ref('')
const qrcodeId = ref('')
const errorMsg = ref('')

// 轮询定时器与连续失败计数（网络抖动时不刷屏报错）
let pollTimer: ReturnType<typeof setInterval> | null = null
let pollFailCount = 0
// 轮询在途标记：防止慢响应时 setInterval 触发重叠轮询、乱序覆盖状态
let pollInFlight = false
const POLL_INTERVAL = 2000
const POLL_MAX_FAIL = 5

// ── 状态展示 ──────────────────────────────────────────────
const statusTagType = computed<'success' | 'info' | 'warning' | 'error' | 'default'>(() => {
  switch (state.value) {
    case 'connected':
      return 'success'
    case 'qrcode':
    case 'scaned':
    case 'confirmed':
    case 'testing':
      return 'info'
    case 'expired':
      return 'warning'
    case 'error':
      return 'error'
    default:
      return 'default'
  }
})

const statusText = computed(() => {
  switch (state.value) {
    case 'checking':
      return t('wechat.loadingStatus')
    case 'idle':
      return t('wechat.statusDisconnected')
    case 'loading_qrcode':
      return t('wechat.connecting')
    case 'qrcode':
      return t('wechat.scanTip')
    case 'scaned':
      return t('wechat.scanned')
    case 'confirmed':
      return t('wechat.confirmed')
    case 'testing':
      return t('wechat.testing')
    case 'connected':
      return t('wechat.statusConnected')
    case 'expired':
      return t('wechat.expired')
    case 'error':
      return t('wechat.error')
    default:
      return ''
  }
})

// ── 生命周期 ──────────────────────────────────────────────
onMounted(() => {
  checkStatus()
})

onUnmounted(() => {
  stopPolling()
})

// ── 轮询控制 ──────────────────────────────────────────────
function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  pollFailCount = 0
  pollInFlight = false
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(pollStatus, POLL_INTERVAL)
}

// ── 状态检查（页面加载时） ────────────────────────────────
async function checkStatus() {
  state.value = 'checking'
  try {
    const data = await get<{ connected?: boolean; expired?: boolean }>('/wechat/status')
    if (data?.connected) {
      state.value = 'connected'
    } else if (data?.expired) {
      state.value = 'expired'
    } else {
      state.value = 'idle'
    }
  } catch {
    // 状态检查失败不报错，回退到 idle 让用户可主动连接
    state.value = 'idle'
  }
}

// ── 获取二维码 ────────────────────────────────────────────
async function fetchQrcode() {
  state.value = 'loading_qrcode'
  errorMsg.value = ''
  stopPolling()
  try {
    const data = await post<{ qrcode_id: string; qrcode_url: string; qrcode_img?: string }>('/wechat/qrcode')
    qrcodeId.value = data.qrcode_id
    // 优先使用后端生成的 base64 二维码图片，回退到原始 URL
    qrcodeUrl.value = data.qrcode_img || data.qrcode_url
    state.value = 'qrcode'
    startPolling()
  } catch (e: any) {
    errorMsg.value = e?.message || t('wechat.qrFailed')
    state.value = 'error'
    message.error(errorMsg.value)
  }
}

// ── 轮询扫码状态 ──────────────────────────────────────────
async function pollStatus() {
  // guard：上一轮轮询仍在途时跳过本次，避免 overlap 导致响应乱序覆盖状态
  if (pollInFlight) return
  if (!qrcodeId.value) return
  // guard：已确认/已连接后忽略残留的轮询响应
  // stopPolling 清除定时器，但已发出的在途请求仍会返回 confirmed，
  // 若不拦截会重复触发 message.success（"登录成功出现10次"根因）
  if (state.value === 'confirmed' || state.value === 'connected') return
  pollInFlight = true
  try {
    const data = await get<{ status: string }>(
      '/wechat/qrcode-status?qrcode_id=' + encodeURIComponent(qrcodeId.value),
    )
    pollFailCount = 0
    const st = data?.status
    if (st === 'wait') {
      // 继续轮询
    } else if (st === 'scaned') {
      state.value = 'scaned'
    } else if (st === 'confirmed') {
      state.value = 'confirmed'
      stopPolling()
      message.success(t('wechat.confirmed'))
    } else if (st === 'expired') {
      state.value = 'expired'
      stopPolling()
    }
  } catch {
    // 网络抖动：静默重试，连续失败超过阈值则停止轮询并提示
    pollFailCount += 1
    if (pollFailCount >= POLL_MAX_FAIL) {
      stopPolling()
      errorMsg.value = t('wechat.qrFailed')
      state.value = 'error'
      message.error(errorMsg.value)
    }
  } finally {
    pollInFlight = false
  }
}

// ── 测试连接 → 成功后启动轮询 ─────────────────────────────
async function testConnection() {
  state.value = 'testing'
  try {
    const data = await post<{ success?: boolean; error?: string }>('/wechat/test')
    if (data?.success === false) {
      // 后端软失败（ok=true 但 success=false）：回到 confirmed 允许重试
      state.value = 'confirmed'
      errorMsg.value = data?.error || t('wechat.testFailed')
      message.error(errorMsg.value)
      return
    }
    message.success(t('wechat.testSuccess'))
    await startBot()
  } catch (e: any) {
    // 网络错误或后端返回 ok=false：回到 confirmed，允许重试
    state.value = 'confirmed'
    errorMsg.value = e?.message || t('wechat.testFailed')
    message.error(errorMsg.value)
  }
}

async function startBot() {
  try {
    await post('/wechat/start')
    state.value = 'connected'
    message.success(t('wechat.startSuccess'))
  } catch (e: any) {
    state.value = 'confirmed'
    errorMsg.value = e?.message || t('wechat.startFailed')
    message.error(errorMsg.value)
  }
}

// ── 断开连接 ──────────────────────────────────────────────
async function disconnect() {
  stopPolling()
  try {
    const data = await post<{ success?: boolean }>('/wechat/stop')
    if (data?.success === false) {
      // 后端软失败（ok=true 但 success=false）：保留已连接状态，提示失败
      errorMsg.value = t('wechat.disconnectFailed')
      message.error(errorMsg.value)
      return
    }
    state.value = 'idle'
    qrcodeId.value = ''
    qrcodeUrl.value = ''
    message.success(t('wechat.disconnected'))
  } catch (e: any) {
    errorMsg.value = e?.message || t('wechat.disconnectFailed')
    message.error(errorMsg.value)
  }
}
</script>

<template>
  <Tilt3D :max-x="4" :max-y="6">
    <section class="glass-panel section wechat-panel">
      <div class="section-head">
        <h3>{{ t('wechat.title') }}</h3>
        <n-tag :type="statusTagType" size="small" round>{{ statusText }}</n-tag>
      </div>
      <p class="apikey-desc">{{ t('wechat.desc') }}</p>

      <!-- checking：加载连接状态 -->
      <div v-if="state === 'checking'" class="qr-center">
        <n-spin size="medium" />
        <p class="qr-tip">{{ t('wechat.loadingStatus') }}</p>
      </div>

      <!-- idle：未连接 -->
      <div v-else-if="state === 'idle'" class="qr-center">
        <p class="qr-tip">{{ t('wechat.idleHint') }}</p>
        <n-button type="primary" size="medium" @click="fetchQrcode">
          {{ t('wechat.connect') }}
        </n-button>
      </div>

      <!-- loading_qrcode：获取二维码中 -->
      <div v-else-if="state === 'loading_qrcode'" class="qr-center">
        <n-spin size="medium" />
        <p class="qr-tip">{{ t('wechat.connecting') }}</p>
      </div>

      <!-- qrcode / scaned：显示二维码 -->
      <div v-else-if="state === 'qrcode' || state === 'scaned'" class="qr-center">
        <div class="qr-wrapper">
          <img :src="qrcodeUrl" :alt="t('wechat.scanTip')" class="qr-img" />
          <div v-if="state === 'scaned'" class="qr-overlay">
            <n-tag type="success" size="large" round>✓ {{ t('wechat.scanned') }}</n-tag>
          </div>
        </div>
        <p class="qr-tip" v-if="state === 'qrcode'">{{ t('wechat.scanTip') }}</p>
        <p class="qr-tip" v-else>{{ t('wechat.confirmOnPhone') }}</p>
        <n-button v-if="state === 'qrcode'" quaternary size="small" class="refresh-btn" @click="fetchQrcode">
          ↻ {{ t('wechat.refresh') }}
        </n-button>
      </div>

      <!-- confirmed：扫码成功 -->
      <div v-else-if="state === 'confirmed'" class="qr-center">
        <n-result status="success" :title="t('wechat.confirmed')" :description="t('wechat.confirmedDesc')" />
        <n-button type="primary" size="medium" class="action-btn" @click="testConnection">
          {{ t('wechat.test') }}
        </n-button>
      </div>

      <!-- testing：测试连接中 -->
      <div v-else-if="state === 'testing'" class="qr-center">
        <n-spin size="medium" />
        <p class="qr-tip">{{ t('wechat.testing') }}</p>
      </div>

      <!-- connected：已连接 -->
      <div v-else-if="state === 'connected'" class="qr-center">
        <n-result status="success" :title="t('wechat.connected')" :description="t('wechat.connectedDesc')" />
        <n-popconfirm @positive-click="disconnect">
          <template #trigger>
            <n-button type="error" secondary size="medium" class="action-btn">
              {{ t('wechat.disconnect') }}
            </n-button>
          </template>
          {{ t('wechat.disconnectConfirm') }}
        </n-popconfirm>
      </div>

      <!-- expired：二维码过期 -->
      <div v-else-if="state === 'expired'" class="qr-center">
        <n-result status="warning" :title="t('wechat.expired')" :description="t('wechat.expiredDesc')" />
        <n-button type="primary" size="medium" class="action-btn" @click="fetchQrcode">
          {{ t('wechat.retry') }}
        </n-button>
      </div>

      <!-- error：错误 -->
      <div v-else-if="state === 'error'" class="qr-center">
        <n-result status="error" :title="t('wechat.error')" :description="errorMsg" />
        <n-button type="primary" size="medium" class="action-btn" @click="fetchQrcode">
          {{ t('wechat.retryBtn') }}
        </n-button>
      </div>
    </section>
  </Tilt3D>
</template>

<style scoped>
.wechat-panel { padding: 16px 18px; margin-bottom: 14px; }
.wechat-panel h3 { font-size: 14px; color: var(--dendro); margin-bottom: 14px; }

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.section-head h3 { margin: 0; }

.apikey-desc { font-size: 12.5px; color: var(--wisdom); margin: 0 0 12px; }

.qr-center {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 10px 0 4px;
}

.qr-tip {
  font-size: 13px;
  color: var(--moon);
  margin: 0;
  text-align: center;
}

.qr-wrapper {
  position: relative;
  display: inline-flex;
  background: #fff;
  border-radius: 12px;
  padding: 10px;
  box-shadow: 0 4px 18px rgba(0, 0, 0, 0.18);
}

.qr-img {
  display: block;
  width: 200px;
  height: 200px;
  object-fit: contain;
  border-radius: 6px;
}

.qr-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(255, 255, 255, 0.82);
  border-radius: 12px;
}

.refresh-btn { margin-top: 6px; opacity: 0.7; }
.refresh-btn:hover { opacity: 1; }

.action-btn { margin-top: 4px; }
</style>
