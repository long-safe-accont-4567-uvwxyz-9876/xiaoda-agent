<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, watch } from 'vue'
import {
  NButton, NSwitch, NRadioGroup, NRadioButton, NSelect, NSlider,
  NSpin, NStatistic, NTag, NEmpty, NSteps, NStep, NAlert, useMessage,
} from 'naive-ui'
import { get, put, post } from '../api'
import { t } from '../i18n'

const message = useMessage()

interface MailConfig {
  enabled: boolean
  mode: 'off' | 'allowlist' | 'all'
  allowed_senders: string[]
  reply_channel: 'mail' | 'mail_and_qq'
  max_per_day: number
  dnd_start: number  // 免打扰开始小时（0-23），0+0=不启用
  dnd_end: number    // 免打扰结束小时（0-23）
}

interface MailStats {
  enabled: boolean
  mode: string
  daily_count: number
  max_per_day: number
  processed_total: number
  last_poll_time: string | null
}

interface InboxMail {
  message_id: string
  subject: string
  from: { email: string; name: string }
  created_at: string
  is_read: boolean
}

interface AuthStatus {
  installed: boolean
  cli_path: string | null
  authorized: boolean
  email: string
  error: string
}

const config = ref<MailConfig>({
  enabled: false,
  mode: 'off',
  allowed_senders: [],
  reply_channel: 'mail',
  max_per_day: 50,
  dnd_start: 0,
  dnd_end: 0,
})
const stats = ref<MailStats | null>(null)
const inbox = ref<InboxMail[]>([])
const authStatus = ref<AuthStatus | null>(null)
const authChecking = ref(false)
const authLogging = ref(false)

const configLoading = ref(false)
const statsLoading = ref(false)
const inboxLoading = ref(false)
const saving = ref(false)

// 自动保存（debounce）：config 变化后延迟 400ms 自动 PUT
let saveTimer: ReturnType<typeof setTimeout> | null = null
let initialized = false  // 防止首次 loadConfig 触发自动保存

// 免打扰小时选项（0-23，起止相同=不启用）
const dndHourOptions = Array.from({ length: 24 }, (_, i) => ({
  label: `${String(i).padStart(2, '0')}:00`,
  value: i,
}))

const modeDesc = computed(() => {
  switch (config.value.mode) {
    case 'off': return t('mailView.modeOffDesc')
    case 'allowlist': return t('mailView.modeAllowlistDesc')
    case 'all': return t('mailView.modeAllDesc')
    default: return ''
  }
})

const channelDesc = computed(() => {
  return config.value.reply_channel === 'mail_and_qq'
    ? t('mailView.channelMailQQDesc')
    : t('mailView.channelMailDesc')
})

// 邮箱连接状态：0=未安装 1=未授权 2=已授权
const authStep = computed(() => {
  if (!authStatus.value) return -1
  if (!authStatus.value.installed) return 0
  if (!authStatus.value.authorized) return 1
  return 2
})

function fmtTime(ts: string | null): string {
  if (!ts) return t('mailView.neverPolled')
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function senderDisplay(m: InboxMail): string {
  const name = m.from?.name?.trim()
  const email = m.from?.email?.trim()
  if (name && email) return `${name} <${email}>`
  return email || name || '—'
}

async function loadAuthStatus() {
  authChecking.value = true
  try {
    authStatus.value = await get<AuthStatus>('/mail/auth-status')
  } catch (e: any) {
    authStatus.value = {
      installed: false,
      cli_path: null,
      authorized: false,
      email: '',
      error: e.message || '检查失败',
    }
  } finally {
    authChecking.value = false
  }
}

async function triggerAuthLogin() {
  authLogging.value = true
  try {
    const res = await post<{ started: boolean; message: string; cli_path: string | null }>('/mail/auth-login')
    if (res.started) {
      message.info(res.message || t('mailView.authBrowserOpened'))
      // 等待 5 秒后检查授权状态
      setTimeout(async () => {
        await loadAuthStatus()
        if (authStatus.value?.authorized) {
          message.success(t('mailView.authSuccess'))
          loadInbox()
        }
        authLogging.value = false
      }, 5000)
    } else {
      message.error(res.message || t('mailView.authFailed'))
      authLogging.value = false
    }
  } catch (e: any) {
    message.error(e.message || t('mailView.authFailed'))
    authLogging.value = false
  }
}

async function loadConfig() {
  configLoading.value = true
  try {
    const data = await get<MailConfig>('/mail/config')
    config.value = {
      enabled: !!data.enabled,
      mode: data.mode || 'off',
      allowed_senders: Array.isArray(data.allowed_senders) ? data.allowed_senders : [],
      reply_channel: data.reply_channel || 'mail',
      max_per_day: typeof data.max_per_day === 'number' ? data.max_per_day : 50,
      dnd_start: typeof data.dnd_start === 'number' ? data.dnd_start : 0,
      dnd_end: typeof data.dnd_end === 'number' ? data.dnd_end : 0,
    }
  } catch (e: any) {
    message.error(e.message || t('mailView.loadFailed'))
  } finally {
    configLoading.value = false
  }
}

async function loadStats() {
  statsLoading.value = true
  try {
    stats.value = await get<MailStats>('/mail/stats')
  } catch (e: any) {
    message.error(e.message || t('mailView.loadFailed'))
  } finally {
    statsLoading.value = false
  }
}

async function loadInbox() {
  inboxLoading.value = true
  try {
    const res = await get<{ data: InboxMail[]; pagination: any }>('/mail/inbox?limit=10')
    inbox.value = Array.isArray(res?.data) ? res.data : []
  } catch (e: any) {
    message.error(e.message || t('mailView.loadFailed'))
  } finally {
    inboxLoading.value = false
  }
}

async function saveConfig() {
  if (saving.value) return  // 防止重复提交
  saving.value = true
  try {
    await put('/mail/config', {
      enabled: config.value.enabled,
      mode: config.value.mode,
      allowed_senders: config.value.allowed_senders,
      reply_channel: config.value.reply_channel,
      max_per_day: config.value.max_per_day,
      dnd_start: config.value.dnd_start,
      dnd_end: config.value.dnd_end,
    })
    message.success(t('mailView.saved'))
    loadStats()
  } catch (e: any) {
    message.error(e.message || t('mailView.loadFailed'))
  } finally {
    saving.value = false
  }
}

function scheduleSave() {
  if (!initialized) return  // 首次加载跳过
  if (saveTimer) clearTimeout(saveTimer)
  saveTimer = setTimeout(() => { saveConfig() }, 400)
}

// 监听 config 变化，自动触发保存
watch(config, () => scheduleSave(), { deep: true })

onMounted(async () => {
  await loadAuthStatus()
  await loadConfig()
  initialized = true  // 加载完成后才允许自动保存
  loadStats()
  if (authStatus.value?.authorized) {
    loadInbox()
  }
})

onUnmounted(() => {
  if (saveTimer) clearTimeout(saveTimer)
})
</script>

<template>
  <div class="mail-view">
    <h2 class="view-title">{{ t('mailView.title') }}</h2>

    <!-- 邮箱连接向导 -->
    <section class="glass-panel section animate-slide-up connect-section">
      <h3>{{ t('mailView.connectCard') }}</h3>
      <n-spin :show="authChecking">
        <div v-if="authStatus" class="connect-body">
          <!-- 已连接 -->
          <div v-if="authStep === 2" class="connect-success">
            <div class="connect-status-row">
              <span class="connect-dot on">●</span>
              <span class="connect-label">{{ t('mailView.connected') }}</span>
              <span v-if="authStatus.email" class="connect-email">{{ authStatus.email }}</span>
            </div>
            <n-button size="small" quaternary :loading="authChecking" @click="loadAuthStatus">
              {{ t('refresh') }}
            </n-button>
          </div>

          <!-- 未授权 -->
          <div v-else-if="authStep === 1" class="connect-wizard">
            <n-alert type="warning" :show-icon="true" class="connect-alert">
              {{ t('mailView.notAuthorizedHint') }}
            </n-alert>
            <div class="connect-steps">
              <div class="connect-step">
                <div class="step-num">1</div>
                <div class="step-content">
                  <div class="step-title">{{ t('mailView.stepOpenBrowser') }}</div>
                  <div class="step-desc">{{ t('mailView.stepOpenBrowserDesc') }}</div>
                </div>
              </div>
              <div class="connect-step">
                <div class="step-num">2</div>
                <div class="step-content">
                  <div class="step-title">{{ t('mailView.stepAuthQQ') }}</div>
                  <div class="step-desc">{{ t('mailView.stepAuthQQDesc') }}</div>
                </div>
              </div>
              <div class="connect-step">
                <div class="step-num">3</div>
                <div class="step-content">
                  <div class="step-title">{{ t('mailView.stepDone') }}</div>
                  <div class="step-desc">{{ t('mailView.stepDoneDesc') }}</div>
                </div>
              </div>
            </div>
            <div class="connect-actions">
              <n-button type="primary" :loading="authLogging" @click="triggerAuthLogin">
                {{ t('mailView.startAuth') }}
              </n-button>
              <n-button size="small" quaternary :loading="authChecking" @click="loadAuthStatus">
                {{ t('mailView.checkAgain') }}
              </n-button>
            </div>
          </div>

          <!-- 未安装 -->
          <div v-else class="connect-wizard">
            <n-alert type="error" :show-icon="true" class="connect-alert">
              {{ t('mailView.notInstalledHint') }}
            </n-alert>
            <div class="connect-actions">
              <n-button size="small" quaternary :loading="authChecking" @click="loadAuthStatus">
                {{ t('mailView.checkAgain') }}
              </n-button>
            </div>
          </div>
        </div>
        <n-empty v-else style="padding: 24px 0" />
      </n-spin>
    </section>

    <!-- 邮箱未连接时，下方功能置灰提示 -->
    <template v-if="authStep === 2">
      <!-- 5.1 收件处理设置 -->
      <section class="glass-panel section animate-slide-up">
        <h3>{{ t('mailView.configCard') }}</h3>
        <n-spin :show="configLoading">
          <div class="cfg-body">
            <div class="setting-row">
              <div class="row-label">
                <span class="s-label">{{ t('mailView.masterSwitch') }}</span>
                <span class="row-desc">{{ t('mailView.masterSwitchDesc') }}</span>
              </div>
              <n-switch v-model:value="config.enabled" />
            </div>

            <div class="setting-row">
              <div class="row-label">
                <span class="s-label">{{ t('mailView.processMode') }}</span>
              </div>
              <n-radio-group v-model:value="config.mode">
                <n-radio-button value="off">{{ t('mailView.modeOff') }}</n-radio-button>
                <n-radio-button value="allowlist">{{ t('mailView.modeAllowlist') }}</n-radio-button>
                <n-radio-button value="all">{{ t('mailView.modeAll') }}</n-radio-button>
              </n-radio-group>
            </div>
            <p class="perm-desc">{{ modeDesc }}</p>

            <transition name="fade-slide">
              <div v-if="config.mode === 'allowlist'" class="setting-row column-row">
                <span class="s-label">{{ t('mailView.allowedSenders') }}</span>
                <n-select
                  v-model:value="config.allowed_senders"
                  tag
                  filterable
                  multiple
                  :placeholder="t('mailView.allowedSendersPh')"
                  :max-tag-count="8"
                  class="full-width"
                />
              </div>
            </transition>

            <div class="setting-row">
              <div class="row-label">
                <span class="s-label">{{ t('mailView.replyChannel') }}</span>
              </div>
              <n-radio-group v-model:value="config.reply_channel">
                <n-radio-button value="mail">{{ t('mailView.channelMail') }}</n-radio-button>
                <n-radio-button value="mail_and_qq">{{ t('mailView.channelMailQQ') }}</n-radio-button>
              </n-radio-group>
            </div>
            <p class="perm-desc">{{ channelDesc }}</p>

            <div class="setting-row brightness-row">
              <div class="brightness-label">
                <span class="s-label">{{ t('mailView.dailyLimit') }}</span>
                <span class="brightness-value">{{ config.max_per_day }}</span>
              </div>
              <n-slider
                v-model:value="config.max_per_day"
                :min="5"
                :max="100"
                :step="1"
                :marks="{ 5: '5', 50: '50', 100: '100' }"
                class="full-width"
              />
            </div>
            <p class="brightness-hint">{{ t('mailView.dailyLimitHint') }}</p>

            <div class="setting-row">
              <div class="row-label">
                <span class="s-label">{{ t('mailView.dndPeriod') }}</span>
                <span class="row-desc">{{ t('mailView.dndPeriodDesc') }}</span>
              </div>
              <div class="dnd-pickers">
                <n-select v-model:value="config.dnd_start" :options="dndHourOptions"
                          size="small" style="width: 110px" />
                <span class="dnd-sep">~</span>
                <n-select v-model:value="config.dnd_end" :options="dndHourOptions"
                          size="small" style="width: 110px" />
              </div>
            </div>
            <p class="perm-desc">{{ t('mailView.dndHint') }}</p>

            <div class="save-row" v-if="saving">
              <span class="saving-hint">{{ t('mailView.saving') }}</span>
            </div>
          </div>
        </n-spin>
      </section>

      <!-- 5.2 状态统计 -->
      <section class="glass-panel section animate-slide-up">
        <div class="section-head">
          <h3>{{ t('mailView.statsCard') }}</h3>
          <n-button size="small" :loading="statsLoading" @click="loadStats">{{ t('refresh') }}</n-button>
        </div>
        <n-spin :show="statsLoading">
          <div class="stats-grid" v-if="stats">
            <div class="stat-item">
              <n-statistic :label="t('mailView.statEnabled')">
                <template #default>
                  <span :class="['stat-state', stats.enabled ? 'on' : 'off']">
                    ● {{ stats.enabled ? t('mailView.statOn') : t('mailView.statOff') }}
                  </span>
                </template>
              </n-statistic>
            </div>
            <div class="stat-item">
              <n-statistic :label="t('mailView.statMode')">
                <span class="stat-val">{{ t(`mailView.modeLabel.${stats.mode}`) || stats.mode }}</span>
              </n-statistic>
            </div>
            <div class="stat-item">
              <n-statistic :label="t('mailView.statDailyCount')">
                <span class="stat-val">{{ stats.daily_count }} / {{ stats.max_per_day }}</span>
              </n-statistic>
            </div>
            <div class="stat-item">
              <n-statistic :label="t('mailView.statProcessedTotal')">
                <span class="stat-val">{{ stats.processed_total }}</span>
              </n-statistic>
            </div>
            <div class="stat-item wide">
              <n-statistic :label="t('mailView.statLastPoll')">
                <span class="stat-val mono">{{ fmtTime(stats.last_poll_time) }}</span>
              </n-statistic>
            </div>
          </div>
          <n-empty v-else style="padding: 24px 0" />
        </n-spin>
      </section>

      <!-- 5.3 收件箱预览 -->
      <section class="glass-panel section animate-slide-up">
        <div class="section-head">
          <h3>{{ t('mailView.inboxCard') }}</h3>
          <n-button size="small" :loading="inboxLoading" @click="loadInbox">{{ t('refresh') }}</n-button>
        </div>
        <p class="apikey-desc">{{ t('mailView.inboxHint') }}</p>
        <n-spin :show="inboxLoading">
          <div v-if="inbox.length" class="mail-list">
            <div v-for="m in inbox" :key="m.message_id" class="mail-item" :class="{ unread: !m.is_read }">
              <div class="mail-from">
                <span class="from-text" :title="senderDisplay(m)">{{ senderDisplay(m) }}</span>
                <n-tag v-if="!m.is_read" size="tiny" type="warning" round>{{ t('mailView.unread') }}</n-tag>
                <n-tag v-else size="tiny" round>{{ t('mailView.read') }}</n-tag>
              </div>
              <div class="mail-subject" :title="m.subject">{{ m.subject || t('mailView.noSubject') }}</div>
              <div class="mail-time mono">{{ fmtTime(m.created_at) }}</div>
            </div>
          </div>
          <n-empty v-else :description="t('mailView.inboxEmpty')" style="padding: 32px 0" />
        </n-spin>
      </section>
    </template>

    <!-- 邮箱未连接时的提示 -->
    <section v-else class="glass-panel section animate-slide-up not-connected-hint">
      <n-empty :description="t('mailView.connectFirst')" style="padding: 40px 0" />
    </section>
  </div>
</template>

<style scoped>
.mail-view { display: flex; flex-direction: column; }

.view-title {
  font-family: 'Noto Serif SC', serif;
  margin-bottom: 14px;
  color: var(--dendro);
  text-shadow: 0 0 12px rgba(143, 229, 96, 0.25);
}

.section { padding: 16px 18px; margin-bottom: 14px; }
.section h3 { font-size: 14px; color: var(--dendro); margin-bottom: 14px; }
.section-head { display: flex; align-items: center; justify-content: space-between; }
.section-head h3 { margin: 0; }

.cfg-body { display: flex; flex-direction: column; gap: 4px; }

.setting-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 0; gap: 16px; flex-wrap: wrap;
}
.column-row { flex-direction: column; align-items: stretch; gap: 8px; }
.row-label { display: flex; flex-direction: column; gap: 2px; }
.row-desc { font-size: 11.5px; color: var(--moon-dim); opacity: 0.75; }
.s-label { font-size: 13.5px; }
.full-width { width: 100%; }

.perm-desc { font-size: 12.5px; color: var(--wisdom); margin: 4px 0 8px; }
.apikey-desc { font-size: 12.5px; color: var(--wisdom); margin: 0 0 12px; }

/* 滑块行 */
.brightness-row { flex-direction: column; align-items: stretch; gap: 8px; }
.brightness-label { display: flex; justify-content: space-between; align-items: center; }
.brightness-value {
  font-size: 13px; color: var(--dendro);
  font-family: 'JetBrains Mono', monospace;
}
.brightness-hint { font-size: 11.5px; color: var(--moon-dim); margin: 4px 0 0; opacity: 0.7; }

.save-row { display: flex; justify-content: flex-end; padding-top: 8px; }

/* DND 时段选择器 */
.dnd-pickers { display: flex; align-items: center; gap: 8px; }
.dnd-sep { color: var(--moon-dim); font-size: 13px; }
.saving-hint {
  font-size: 12px; color: var(--moon-dim);
  font-family: 'JetBrains Mono', monospace;
  opacity: 0.8;
}

/* 统计卡片 */
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 16px;
  margin-top: 4px;
}
.stat-item {
  background: rgba(10, 24, 16, 0.4);
  border: 1px solid var(--glass-border);
  border-radius: 12px;
  padding: 14px 16px;
  transition: border-color 0.25s, box-shadow 0.25s;
}
.stat-item:hover { border-color: rgba(143, 229, 96, 0.35); box-shadow: var(--shadow-glow); }
.stat-item.wide { grid-column: span 2; }
.stat-state { font-size: 16px; font-weight: 600; }
.stat-state.on { color: var(--dendro); }
.stat-state.off { color: var(--moon-dim); }
.stat-val { font-size: 16px; font-weight: 600; color: var(--moon); }
.mono { font-family: 'JetBrains Mono', monospace; font-size: 13px; }

/* 收件箱列表 */
.mail-list { display: flex; flex-direction: column; gap: 6px; }
.mail-item {
  display: grid;
  grid-template-columns: 1.4fr 2fr auto;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid transparent;
  background: rgba(10, 24, 16, 0.3);
  transition: background 0.2s, border-color 0.2s, transform 0.2s var(--ease-out);
}
.mail-item:hover {
  background: rgba(143, 229, 96, 0.08);
  border-color: var(--glass-border);
  transform: translateX(2px);
}
.mail-item.unread { background: rgba(232, 213, 163, 0.07); border-left: 3px solid var(--wisdom); }
.mail-from {
  display: flex; align-items: center; gap: 8px;
  min-width: 0;
}
.from-text {
  font-size: 13px; color: var(--moon);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  flex: 1;
}
.mail-item.unread .from-text { font-weight: 600; }
.mail-subject {
  font-size: 13px; color: var(--moon-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mail-item.unread .mail-subject { color: var(--moon); }
.mail-time { font-size: 11.5px; color: var(--moon-dim); white-space: nowrap; }

/* 过渡动画 */
.fade-slide-enter-active, .fade-slide-leave-active {
  transition: all 0.28s var(--ease-smooth);
}
.fade-slide-enter-from, .fade-slide-leave-to {
  opacity: 0; transform: translateY(-6px); max-height: 0;
}
.fade-slide-enter-to, .fade-slide-leave-from {
  opacity: 1; max-height: 400px;
}

@media (max-width: 640px) {
  .mail-item { grid-template-columns: 1fr; gap: 4px; }
  .stat-item.wide { grid-column: span 1; }
}

/* 邮箱连接向导 */
.connect-section { border: 1px solid rgba(143, 229, 96, 0.15); }
.connect-body { min-height: 60px; }

.connect-success {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 0;
}
.connect-status-row { display: flex; align-items: center; gap: 10px; }
.connect-dot { font-size: 14px; }
.connect-dot.on { color: var(--dendro); }
.connect-label { font-size: 14px; font-weight: 600; color: var(--moon); }
.connect-email {
  font-size: 13px; color: var(--wisdom);
  font-family: 'JetBrains Mono', monospace;
}

.connect-wizard { display: flex; flex-direction: column; gap: 16px; }
.connect-alert { margin-bottom: 4px; }

.connect-steps { display: flex; flex-direction: column; gap: 12px; }
.connect-step {
  display: flex; align-items: flex-start; gap: 12px;
  padding: 10px 14px;
  background: rgba(10, 24, 16, 0.3);
  border: 1px solid var(--glass-border);
  border-radius: 10px;
}
.step-num {
  width: 24px; height: 24px;
  border-radius: 50%;
  background: rgba(143, 229, 96, 0.18);
  color: var(--dendro);
  font-size: 13px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
}
.step-content { display: flex; flex-direction: column; gap: 2px; }
.step-title { font-size: 13.5px; color: var(--moon); font-weight: 500; }
.step-desc { font-size: 12px; color: var(--moon-dim); opacity: 0.75; }

.connect-actions { display: flex; align-items: center; gap: 10px; }

.not-connected-hint { opacity: 0.6; }
</style>
