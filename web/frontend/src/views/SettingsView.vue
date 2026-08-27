<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import SumeruIcon from '../components/fx/SumeruIcon.vue'
import ViewTitleIcon from '../components/fx/ViewTitleIcon.vue'
import {
  NButton, NSwitch, NRadioGroup, NRadioButton, NInput, NModal,
  NSelect, NSlider, NCheckbox, NCheckboxGroup, NPopconfirm, NTag, NTabs, NTabPane, useMessage,
} from 'naive-ui'
import { get, put, post, api } from '../api'
import type { PermissionModeInfo, SystemConfig } from '../api/types'
import { useUiStore } from '../stores/ui'
import { useAuthStore } from '../stores/auth'
import { useWorkspaceStore } from '../stores/workspace'
import { useRouter } from 'vue-router'
import { t, tf, setLang, state as i18nState } from '../i18n'
import type { Lang } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'
import WeChatConnectPanel from '../components/WeChatConnectPanel.vue'
import { sound } from '../utils/sound'

const message = useMessage()
const ui = useUiStore()
const auth = useAuthStore()
const ws = useWorkspaceStore()
const router = useRouter()
const newCmd = ref('')
const activeTab = ref('appearance')

const permissionMode = ref('')
const permissionOptions = ref<string[]>([])
const logs = ref<string[]>([])
const logLevel = ref<string | null>(null)
const logLoading = ref(false)
const showRestart = ref(false)
const restartConfirmText = ref('')
const showGoatConfirm = ref(false)
const goatConfirmChecked = ref(false)
// 修改登录密码弹窗
const showChangePwd = ref(false)
const changePwdOld = ref('')
const changePwdAnswer = ref('')
const changePwdNew = ref('')
const changePwdNewQuestion = ref('')
const changePwdNewAnswer = ref('')
const changePwdSubmitting = ref(false)
const lanInfo = ref<{ localhost: string; lan_urls: string[]; port: number } | null>(null)
// 多平台共用上下文：可选平台列表（web/cli/qq/wechat）
const sharedPlatforms = ref<string[]>([])
const sharedPlatformOptions = [
  { label: t('settings.sharedContextWeb'), value: 'web' },
  { label: t('settings.sharedContextCli'), value: 'cli' },
  { label: t('settings.sharedContextQq'), value: 'qq' },
  { label: t('settings.sharedContextWechat'), value: 'wechat' },
]

onMounted(async () => {
  await ui.loadRemote()
  try {
    const p = await get<PermissionModeInfo>('/system/permission-mode')
    permissionMode.value = p.mode
    permissionOptions.value = p.options
  } catch (e: any) { message.error(e.message) }
  loadLogs()
  loadLanInfo()
  // 多平台共用上下文：加载已勾选的平台
  try {
    const cfg = await get<SystemConfig>('/system/config')
    sharedPlatforms.value = Array.isArray(cfg?.context?.shared_platforms) ? cfg.context.shared_platforms : []
  } catch { /* 忽略加载失败 */ }
  // 工作目录授权状态、白名单、审计日志
  ws.loadStatus()
  ws.loadWhitelist()
  ws.loadAudit()
})

async function saveSharedPlatforms() {
  try {
    await put('/system/config', { path: 'context.shared_platforms', value: [...sharedPlatforms.value] })
    message.success(t('settings.sharedContextSaved'))
  } catch (e: any) {
    message.error(e.message || t('settings.sharedContextSaveFailed'))
  }
}

function updateSharedPlatforms(values: Array<string | number>) {
  sharedPlatforms.value = values.map(String)
  saveSharedPlatforms()
}

async function loadLanInfo() {
  try {
    const data = await get<{ localhost: string; lan_ips: string[]; lan_urls: string[]; port: number }>('/system/lan-addresses')
    lanInfo.value = data
  } catch { /* 忽略 */ }
}

function copyUrl(url: string) {
  navigator.clipboard.writeText(url).then(() => {
    message.success(t('settings.copied'))
  }).catch(() => {
    message.warning(t('settings.copyFailed'))
  })
}

async function setPermMode(mode: string) {
  if (mode === 'goat') {
    showGoatConfirm.value = true
    goatConfirmChecked.value = false
    return
  }
  try {
    await put('/system/permission-mode', { mode })
    permissionMode.value = mode
    message.success(tf('settings.permSwitched', mode))
  } catch (e: any) { message.error(e.message) }
}

async function confirmGoatMode() {
  if (!goatConfirmChecked.value) return
  try {
    await put('/system/permission-mode', { mode: 'goat', confirm: 'yes' })
    permissionMode.value = 'goat'
    showGoatConfirm.value = false
    message.success(t('settings.goatEnabled'))
  } catch (e: any) { message.error(e.message) }
}

async function loadLogs() {
  logLoading.value = true
  try {
    logs.value = await get<string[]>(`/system/logs?lines=200${logLevel.value ? `&level=${logLevel.value}` : ''}`)
  } catch (e: any) {
    message.error(e.message)
  } finally {
    logLoading.value = false
  }
}

const doctorResult = ref<any>(null)
const doctorLoading = ref(false)
const fixLoading = ref(false)

async function runDoctor() {
  doctorLoading.value = true
  try {
    doctorResult.value = await get<any>('/system/doctor')
  } catch (e: any) {
    message.error(e.message)
  } finally {
    doctorLoading.value = false
  }
}

async function doctorFix() {
  fixLoading.value = true
  try {
    const res = await post<any>('/system/doctor/fix', {})
    if (res.fixed?.length) {
      message.success(`已自动修复 ${res.fixed.length} 个问题`)
    } else {
      message.info('无需修复')
    }
    await runDoctor()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    fixLoading.value = false
  }
}

async function doRestart() {
  if (restartConfirmText.value !== 'RESTART') return
  try {
    await post('/system/restart', {}, true)
    message.warning(t('settings.restarting'))
    showRestart.value = false
  } catch (e: any) { message.error(e.message) }
}

function logout() {
  auth.logout()
  router.replace('/login')
}

// ── 修改登录密码 ───────────────────────────────────────────
async function submitChangePassword() {
  if (!changePwdAnswer.value.trim()) {
    message.warning(t('settings.changePasswordAnswer') + '：' + t('login.recoverAnswerRequired'))
    return
  }
  if ((changePwdNew.value || '').length < 8) {
    message.warning(t('login.recoverPasswordTooShort'))
    return
  }
  changePwdSubmitting.value = true
  try {
    const data = await api.changePassword({
      old_password: changePwdOld.value,
      new_password: changePwdNew.value,
      answer: changePwdAnswer.value.trim(),
      ...(changePwdNewQuestion.value.trim() ? { new_question: changePwdNewQuestion.value.trim() } : {}),
      ...(changePwdNewAnswer.value.trim() ? { new_answer: changePwdNewAnswer.value.trim() } : {}),
    })
    // 用返回的新 token 替换本地存储（与登录时的存储方式保持一致）
    localStorage.setItem('token', data.token)
    localStorage.setItem('expires_at', String(data.expires_at))
    window.dispatchEvent(new CustomEvent('xiaoda-auth-renewed', {
      detail: { token: data.token, expiresAt: data.expires_at },
    }))
    showChangePwd.value = false
    changePwdOld.value = ''
    changePwdAnswer.value = ''
    changePwdNew.value = ''
    changePwdNewQuestion.value = ''
    changePwdNewAnswer.value = ''
    message.success(t('settings.changePasswordOk'))
  } catch (e: any) {
    message.error(e.message || t('settings.changePasswordSubmit'))
  } finally {
    changePwdSubmitting.value = false
  }
}

// ── 工作目录授权管理 ──────────────────────────────────────
async function onRevokeWorkspace() {
  try {
    await ws.revoke()
    message.success(t('settings.workspaceRevokeOk'))
    ws.loadAudit()
  } catch (e: any) {
    message.error(e.message || t('settings.workspaceRevokeFailed'))
  }
}

async function onAddCmd() {
  const cmd = newCmd.value.trim()
  if (!cmd) return
  try {
    await ws.addWhitelist(cmd)
    newCmd.value = ''
    message.success(t('settings.cmdWhitelistAdded'))
  } catch (e: any) {
    message.error(e.message || t('settings.cmdWhitelistAddFailed'))
  }
}

async function onRemoveCmd(cmd: string) {
  try {
    await ws.removeWhitelist(cmd)
    message.success(t('settings.cmdWhitelistRemoved'))
  } catch (e: any) {
    message.error(e.message || t('settings.cmdWhitelistRemoveFailed'))
  }
}

async function onRefreshAudit() {
  await ws.loadAudit()
}

const permDesc = computed<Record<string, string>>(() => ({
  goat: t('settings.permissionDesc.goat'),
  default: t('settings.permissionDesc.default'),
  strict: t('settings.permissionDesc.strict'),
}))
const permLabel = computed<Record<string, string>>(() => ({
  goat: t('settings.permissionLabel.goat'),
  default: t('settings.permissionLabel.default'),
  strict: t('settings.permissionLabel.strict'),
}))
</script>

<template>
  <div class="settings-view">
    <h2 class="view-title view-title-icon"><ViewTitleIcon name="settings" />{{ t('settings.title').replace(/^⚙️\s*/, '') }}</h2>

    <n-tabs class="settings-tabs" type="line" animated display-directive="show" v-model:value="activeTab">
      <!-- 外观与语言 -->
      <n-tab-pane name="appearance" :tab="t('settings.tabs.appearance')">
        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <h3>{{ t('settings.appearance') }}</h3>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.particles') }}</span>
            <n-radio-group :value="ui.particles" @update:value="ui.setParticles">
              <n-radio-button value="off">{{ t('settings.particlesOff') }}</n-radio-button>
              <n-radio-button value="low">{{ t('settings.particlesLow') }}</n-radio-button>
              <n-radio-button value="medium">{{ t('settings.particlesMedium') }}</n-radio-button>
              <n-radio-button value="high">{{ t('settings.particlesHigh') }}</n-radio-button>
            </n-radio-group>
          </div>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.tilt3d') }}</span>
            <n-switch :value="ui.tilt3d" @update:value="ui.setTilt3d" />
          </div>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.autoSpeak') }}</span>
            <n-switch :value="ui.autoSpeak" @update:value="(v: boolean) => ui.setAutoSpeak(v).then(() => message.success(t('success'))).catch((e: any) => message.error(e.message))" />
          </div>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.soundFx') }}</span>
            <div class="soundfx-controls">
              <n-switch :value="ui.soundFx" @update:value="(v: boolean) => { ui.setSoundFx(v); sound.play('toggle') }" />
              <n-slider
                class="sound-volume-slider"
                :value="ui.soundVolume"
                :min="0"
                :max="1"
                :step="0.05"
                :disabled="!ui.soundFx"
                @update:value="ui.setSoundVolume"
                @dragend="() => sound.play('receive')"
              />
            </div>
          </div>
          <p class="brightness-hint">{{ t('settings.soundFxHint') }}</p>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.dendroCursor') }}</span>
            <n-switch :value="ui.dendroCursor" @update:value="(v: boolean) => { ui.setDendroCursor(v); sound.play('toggle') }" />
          </div>
          <p class="brightness-hint">{{ t('settings.dendroCursorHint') }}</p>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.dendroCursorTrail') }}</span>
            <n-switch :value="ui.dendroCursorTrail" @update:value="(v: boolean) => { ui.setDendroCursorTrail(v); sound.play('toggle') }" />
          </div>
          <p class="brightness-hint">{{ t('settings.dendroCursorTrailHint') }}</p>
          <div class="setting-row brightness-row">
            <div class="brightness-label">
              <span class="s-label">{{ t('settings.brightness') }}</span>
              <span class="brightness-value">{{ Math.round(ui.brightness * 100) }}%</span>
            </div>
            <div class="brightness-controls">
              <n-switch :value="ui.autoBrightness" @update:value="ui.setAutoBrightness">
                <template #checked>{{ t('settings.autoBrightness') }}</template>
                <template #unchecked>{{ t('settings.manualBrightness') }}</template>
              </n-switch>
              <n-slider
                class="brightness-slider"
                :value="ui.manualBrightness"
                :min="0.5"
                :max="1.5"
                :step="0.05"
                :disabled="ui.autoBrightness"
                @update:value="ui.setManualBrightness"
              />
            </div>
          </div>
          <p class="brightness-hint" v-if="ui.autoBrightness">
            {{ t('settings.brightnessAutoHint') }}
          </p>
          <p class="brightness-hint" v-else>
            {{ t('settings.brightnessManualHint') }}
          </p>
        </section></Tilt3D>

        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <h3>{{ t('settings.language') }}</h3>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.languageDesc') }}</span>
            <n-radio-group :value="i18nState.lang" @update:value="(v: Lang) => { setLang(v); message.success(t('success')) }">
              <n-radio-button value="zh">中文</n-radio-button>
              <n-radio-button value="en">English</n-radio-button>
            </n-radio-group>
          </div>
        </section></Tilt3D>
      </n-tab-pane>

      <!-- 权限与工作目录 -->
      <n-tab-pane name="permission" :tab="t('settings.tabs.permission')">
        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <h3>{{ t('settings.permissionMode') }}</h3>
          <n-radio-group :value="permissionMode" @update:value="setPermMode">
            <n-radio-button v-for="m in permissionOptions" :key="m" :value="m">
              {{ permLabel[m] || m }}
            </n-radio-button>
          </n-radio-group>
          <p class="perm-desc">{{ permDesc[permissionMode] || '' }}</p>
        </section></Tilt3D>

        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <h3>{{ t('settings.sharedContext') }}</h3>
          <p class="apikey-desc">{{ t('settings.sharedContextDesc') }}</p>
          <div class="setting-row">
            <n-checkbox-group :value="sharedPlatforms" @update:value="updateSharedPlatforms">
              <n-checkbox v-for="opt in sharedPlatformOptions" :key="opt.value" :value="opt.value">
                {{ opt.label }}
              </n-checkbox>
            </n-checkbox-group>
          </div>
          <p class="brightness-hint">{{ t('settings.sharedContextHint') }}</p>
        </section></Tilt3D>

        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <h3>{{ t('settings.workspaceAuth') }}</h3>
          <p class="apikey-desc">{{ t('settings.workspaceAuthDesc') }}</p>

          <!-- 授权状态 -->
          <template v-if="ws.authorized">
            <div class="setting-row">
              <span class="s-label">{{ t('settings.workspaceCurrent') }}</span>
              <span class="ws-path" :title="ws.currentPath"><SumeruIcon name="folder" :size="13" variant="duo" tone="view" interactive /> {{ ws.currentPath }}</span>
            </div>
            <div class="setting-row">
              <span class="s-label">{{ t('settings.workspaceAuthorizedAt') }}</span>
              <span class="ws-time">{{ ws.authorizedAt || '—' }}</span>
            </div>
            <div class="setting-row">
              <n-popconfirm @positive-click="onRevokeWorkspace">
                <template #trigger>
                  <n-button type="warning" ghost size="small">{{ t('settings.workspaceRevoke') }}</n-button>
                </template>
                {{ t('settings.workspaceRevokeConfirm') }}
              </n-popconfirm>
            </div>
          </template>
          <template v-else>
            <p class="perm-desc">{{ t('settings.workspaceUnauthorizedHint') }}</p>
          </template>

          <!-- 命令白名单 -->
          <h4 class="sub-title">{{ t('settings.cmdWhitelist') }}</h4>
          <p class="apikey-desc">{{ t('settings.cmdWhitelistHint') }}</p>
          <div class="setting-row whitelist-add-row">
            <n-input class="whitelist-input" v-model:value="newCmd" :placeholder="t('settings.cmdWhitelistAddPlaceholder')" size="small" @keyup.enter="onAddCmd" />
            <n-button size="small" type="primary" @click="onAddCmd">{{ t('settings.cmdWhitelistAdd') }}</n-button>
          </div>
          <div v-if="ws.whitelist.length" class="ws-whitelist">
            <n-tag v-for="cmd in ws.whitelist" :key="cmd" closable size="small" @close="onRemoveCmd(cmd)">{{ cmd }}</n-tag>
          </div>
          <p v-else class="perm-desc">{{ t('settings.cmdWhitelistEmpty') }}</p>

          <!-- 审计日志 -->
          <div class="section-head audit-section-head">
            <h4 class="sub-title">{{ t('settings.workspaceAudit') }}</h4>
            <n-button size="small" @click="onRefreshAudit">{{ t('settings.workspaceAuditRefresh') }}</n-button>
          </div>
          <div v-if="ws.auditLog.length" class="ws-audit">
            <div v-for="(log, i) in ws.auditLog" :key="i" class="audit-entry">
              <span class="audit-time">{{ log.timestamp }}</span>
              <n-tag :type="log.allowed ? 'success' : 'error'" size="small">
                {{ log.allowed ? t('settings.workspaceAuditAllowed') : t('settings.workspaceAuditDenied') }}
              </n-tag>
              <span class="audit-action">{{ log.action }}</span>
              <span class="audit-target" :title="log.target">{{ log.target }}</span>
            </div>
          </div>
          <p v-else class="perm-desc">{{ t('settings.workspaceAuditEmpty') }}</p>
        </section></Tilt3D>
      </n-tab-pane>

      <!-- 连接与访问 -->
      <n-tab-pane name="connection" :tab="t('settings.tabs.connection')">
        <Tilt3D v-if="lanInfo" :max-x="4" :max-y="6"><section class="glass-panel section">
          <h3>{{ t('settings.lanAccess') }}</h3>
          <p class="apikey-desc">{{ t('settings.lanDesc') }}</p>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.localhost') }}</span>
            <span class="url-link" @click="copyUrl(lanInfo!.localhost)">{{ lanInfo!.localhost }}</span>
          </div>
          <div class="setting-row" v-for="url in lanInfo!.lan_urls" :key="url">
            <span class="s-label">{{ t('settings.phoneAccess') }}</span>
            <span class="url-link" @click="copyUrl(url)">{{ url }}</span>
          </div>
          <p class="perm-desc" v-if="!lanInfo!.lan_urls?.length">{{ t('settings.noLanIp') }}</p>
          <p class="perm-desc" v-else>{{ t('settings.clickToCopy') }}</p>
        </section></Tilt3D>

        <WeChatConnectPanel />
      </n-tab-pane>

      <!-- 账号与资料 -->
      <n-tab-pane name="account" :tab="t('settings.tabs.account')">
        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <h3>{{ t('settings.apiKeyConfig') }}</h3>
          <p class="apikey-desc">{{ t('settings.apiKeyDesc') }}</p>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.openApiKeyWizard') }}</span>
            <n-button type="primary" secondary @click="router.push('/setup')">{{ t('settings.openApiKeyBtn') }}</n-button>
          </div>
        </section></Tilt3D>

        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <h3>{{ t('settings.userProfile') }}</h3>
          <p class="apikey-desc">{{ t('settings.userProfileDesc') }}</p>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.editProfile') }}</span>
            <n-button type="primary" secondary @click="router.push('/setup/profile')">{{ t('settings.editProfileBtn') }}</n-button>
          </div>
        </section></Tilt3D>

        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <h3>{{ t('settings.security') }}</h3>
          <p class="apikey-desc">{{ t('settings.changePasswordHint') }}</p>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.changePassword') }}</span>
            <n-button type="warning" secondary @click="showChangePwd = true">{{ t('settings.changePassword') }}</n-button>
          </div>
        </section></Tilt3D>
      </n-tab-pane>

      <!-- 系统与日志 -->
      <n-tab-pane name="system" :tab="t('settings.tabs.system')">
        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <div class="section-head">
            <h3>{{ t('settings.logViewer') }}</h3>
            <div class="log-ops">
              <n-select v-model:value="logLevel" :options="['INFO', 'WARNING', 'ERROR'].map(l => ({ label: l, value: l }))"
                        class="log-level-select" :placeholder="t('settings.logLevel')" clearable size="small"
                        @update:value="loadLogs" />
              <n-button size="small" :loading="logLoading" @click="loadLogs">{{ t('refresh') }}</n-button>
            </div>
          </div>
          <pre class="log-box">{{ logs.join('\n') || t('settings.logEmpty') }}</pre>
        </section></Tilt3D>

        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <div class="section-head">
            <h3>🏥 系统自诊断</h3>
            <div class="log-ops">
              <n-button size="small" type="primary" :loading="doctorLoading" @click="runDoctor">运行诊断</n-button>
              <n-button size="small" type="warning" :loading="fixLoading" :disabled="!doctorResult" @click="doctorFix">自动修复</n-button>
            </div>
          </div>
          <div v-if="doctorResult" class="doctor-result">
            <div v-if="doctorResult.healthy" class="doctor-ok">✓ 系统状态健康</div>
            <div v-else class="doctor-warn">⚠ 发现 {{ doctorResult.issues?.length || 0 }} 个问题</div>
            <div v-if="doctorResult.issues?.length" class="doctor-issues">
              <div v-for="(issue, i) in doctorResult.issues" :key="i" class="doctor-issue">
                <n-tag size="small" :type="issue.severity === 'error' ? 'error' : 'warning'" :bordered="false">{{ issue.severity }}</n-tag>
                <span class="issue-text">{{ issue.message }}</span>
                <span v-if="issue.fixable" class="issue-fixable">可自动修复</span>
              </div>
            </div>
            <div v-if="doctorResult.checks?.length" class="doctor-checks">
              <div v-for="(check, i) in doctorResult.checks" :key="i" class="doctor-check">
                <span :class="check.ok ? 'check-ok' : 'check-fail'">{{ check.ok ? '✓' : '✗' }}</span>
                <span>{{ check.name }}</span>
                <span v-if="check.detail" class="check-detail">{{ check.detail }}</span>
              </div>
            </div>
          </div>
          <div v-else class="doctor-empty">点击「运行诊断」检查系统健康状态</div>
        </section></Tilt3D>

        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section danger">
          <h3>{{ t('settings.dangerZone') }}</h3>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.restartService') }}</span>
            <n-button type="error" secondary @click="showRestart = true">{{ t('settings.restartBtn') }}</n-button>
          </div>
          <div class="setting-row">
            <span class="s-label">{{ t('settings.logout') }}</span>
            <n-button secondary @click="logout">{{ t('settings.logoutBtn') }}</n-button>
          </div>
        </section></Tilt3D>
      </n-tab-pane>
    </n-tabs>

    <n-modal v-model:show="showRestart" preset="card" :title="t('settings.restartConfirmTitle')"
             style="width: min(420px, 94vw)">
      <p style="margin-bottom: 12px; font-size: 13.5px">
        {{ t('settings.restartConfirmDesc') }}
      </p>
      <n-input v-model:value="restartConfirmText" placeholder="RESTART" />
      <template #footer>
        <div style="display:flex; justify-content:flex-end; gap:10px">
          <n-button @click="showRestart = false">{{ t('cancel') }}</n-button>
          <n-button type="error" :disabled="restartConfirmText !== 'RESTART'" @click="doRestart">
            {{ t('settings.restartConfirmBtn') }}
          </n-button>
        </div>
      </template>
    </n-modal>

    <n-modal v-model:show="showGoatConfirm" preset="card" :title="t('settings.goatConfirmTitle')"
             style="width: min(420px, 94vw)">      <div style="margin-bottom: 16px; font-size: 13.5px">
        <p style="margin-bottom: 12px">
          <b>GOAT</b> {{ t('settings.goatConfirmDesc') }}
        </p>
        <ul style="margin: 0 0 12px 20px; line-height: 1.8">
          <li>{{ t('settings.goatFeature1') }}</li>
          <li>{{ t('settings.goatFeature2') }}</li>
          <li>{{ t('settings.goatFeature3') }}</li>
        </ul>
        <p style="color: #e8833a; font-size: 12.5px">
          {{ t('settings.goatWarning') }}
        </p>
      </div>
      <n-checkbox v-model:checked="goatConfirmChecked">
        {{ t('settings.goatConfirmCheckbox') }}
      </n-checkbox>
      <template #footer>
        <div style="display:flex; justify-content:flex-end; gap:10px">
          <n-button @click="showGoatConfirm = false">{{ t('cancel') }}</n-button>
          <n-button type="warning" :disabled="!goatConfirmChecked" @click="confirmGoatMode">
            {{ t('settings.goatConfirmBtn') }}
          </n-button>
        </div>
      </template>
    </n-modal>

    <!-- 修改登录密码 -->
    <n-modal v-model:show="showChangePwd" preset="card" :title="t('settings.changePassword')"
             style="width: min(460px, 94vw)">
      <div style="display:flex; flex-direction:column; gap:12px; margin-bottom:8px">
        <p style="margin:0; font-size:12.5px; color:var(--wisdom); opacity:.85">
          {{ t('settings.changePasswordHint') }}
        </p>
        <n-input
          v-model:value="changePwdOld"
          type="password"
          show-password-on="click"
          :placeholder="t('settings.changePasswordOldPlaceholder')"
        />
        <n-input
          v-model:value="changePwdAnswer"
          type="password"
          show-password-on="click"
          :placeholder="t('settings.changePasswordAnswerPlaceholder')"
        />
        <n-input
          v-model:value="changePwdNew"
          type="password"
          show-password-on="click"
          :placeholder="t('settings.changePasswordNewPlaceholder')"
        />
        <n-input
          v-model:value="changePwdNewQuestion"
          :placeholder="t('settings.changePasswordNewQuestion')"
        />
        <n-input
          v-model:value="changePwdNewAnswer"
          type="password"
          show-password-on="click"
          :placeholder="t('settings.changePasswordNewAnswer')"
        />
      </div>
      <template #footer>
        <div style="display:flex; justify-content:flex-end; gap:10px">
          <n-button @click="showChangePwd = false">{{ t('cancel') }}</n-button>
          <n-button type="warning" :loading="changePwdSubmitting" @click="submitChangePassword">
            {{ changePwdSubmitting ? t('settings.changePasswordSubmitting') : t('settings.changePasswordSubmit') }}
          </n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.settings-view {
  min-width: 0;
  max-width: 100%;
  /* 不设 overflow-x：倾斜外扩需越过面板左缘绘制，裁剪交给外层 .content 兜底。
     2026-08-27 截图对照定位：邮箱页 .mail-view 无任何 overflow 声明故永不裁，
     本行的 clip 是设置页面板左缘被切出垂直线的直接原因（两者余量同样为零） */
}
.view-title { font-family: 'Noto Serif SC', serif; margin-bottom: 14px; }

.settings-tabs {
  min-width: 0;
  max-width: 100%;
}
.settings-tabs :deep(.n-tabs-nav),
.settings-tabs :deep(.n-tabs-nav-scroll-wrapper),
.settings-tabs :deep(.v-x-scroll) {
  min-width: 0;
  max-width: 100%;
}
/* 倾斜裁剪修复第二层（2026-08-27 无头复测定位）：naive-ui pane-wrapper 自带
   overflow:hidden 且裁剪口与面板左缘零余量重合（189=189），是 .settings-view
   放开后的下一个切点（外扩实测 110.5px 全被它切）。解除后由 main-area/content
   （clipLeft=68，余量 121px > 110.5px）兜底，面板视觉位置经 padding+负 margin
   保持不变 */
.settings-tabs :deep(.n-tabs-pane-wrapper) {
  overflow: visible;
  padding: 0 24px;
  margin: 0 -24px;
}

.section { padding: 16px 18px; margin-bottom: 14px; }
.section h3 { font-size: 14px; color: var(--dendro); margin-bottom: 14px; }
.section.danger { border-color: rgba(217, 106, 95, 0.3); }
.section-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-width: 0; }
.section-head h3 { margin: 0; }
.audit-section-head { margin-top: 14px; }

.setting-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 0; gap: 16px; flex-wrap: wrap; min-width: 0;
}
.setting-row > * { min-width: 0; }
.s-label { font-size: 13.5px; overflow-wrap: anywhere; }
.soundfx-controls { display: flex; align-items: center; min-width: 0; }
.sound-volume-slider {
  width: min(160px, 32vw);
  margin-left: 12px;
}
.brightness-slider {
  width: min(200px, 40vw);
  margin-left: 12px;
}
.url-link {
  font-size: 13.5px;
  color: var(--dendro);
  cursor: pointer;
  font-family: 'JetBrains Mono', monospace;
  word-break: break-all;
  overflow-wrap: anywhere;
  min-width: 0;
}
.url-link:hover { text-decoration: underline; }

.perm-desc { font-size: 12.5px; color: var(--wisdom); margin-top: 10px; }
.apikey-desc { font-size: 12.5px; color: var(--wisdom); margin: 0 0 12px; }

.log-ops { display: flex; gap: 8px; min-width: 0; }
.log-level-select { width: 120px; max-width: 100%; }
.log-box {
  margin-top: 12px;
  background: rgba(10, 20, 14, 0.85);
  border-radius: 8px;
  padding: 12px;
  font-size: 11px;
  font-family: 'JetBrains Mono', monospace;
  color: var(--moon-dim);
  max-height: 320px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

/* 亮度控制 */
.brightness-row {
  flex-direction: column;
  align-items: stretch;
  gap: 8px;
}
.brightness-label {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.brightness-value {
  font-size: 13px;
  color: var(--dendro);
  font-family: 'JetBrains Mono', monospace;
}
.brightness-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.brightness-hint {
  font-size: 11.5px;
  color: var(--moon-dim);
  margin: 4px 0 0;
  opacity: 0.7;
}

/* 工作目录授权区块 */
.sub-title {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--dendro);
  margin: 14px 0 4px;
}
.ws-path {
  font-size: 13px;
  color: var(--moon);
  font-family: var(--mono, monospace);
  word-break: break-all;
  overflow-wrap: anywhere;
  min-width: 0;
  flex: 1;
  margin-left: 8px;
}
.ws-time {
  font-size: 12.5px;
  color: var(--moon-dim);
  font-family: var(--mono, monospace);
  margin-left: 8px;
}
.ws-whitelist {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.whitelist-input { flex: 1 1 220px; }
.ws-audit {
  margin-top: 8px;
  max-height: 240px;
  overflow-y: auto;
  border-radius: 8px;
  background: rgba(10, 20, 14, 0.35);
  padding: 6px 8px;
}
.audit-entry {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
  min-width: 0;
}
.audit-entry:last-child { border-bottom: none; }
.audit-time {
  color: var(--moon-dim);
  font-family: var(--mono, monospace);
  font-size: 11px;
  flex-shrink: 0;
  width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.audit-action {
  color: var(--moon);
  font-family: var(--mono, monospace);
  flex-shrink: 0;
  max-width: 120px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.audit-target {
  color: var(--wisdom);
  font-family: var(--mono, monospace);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.doctor-result { margin-top: 10px; }
.doctor-ok { color: var(--dendro); font-weight: 600; margin-bottom: 8px; }
.doctor-warn { color: #e6a23c; font-weight: 600; margin-bottom: 8px; }
.doctor-issues { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
.doctor-issue { display: flex; align-items: center; gap: 8px; font-size: 13px; min-width: 0; }
.issue-text { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.issue-fixable { font-size: 11px; color: var(--dendro); background: rgba(76,175,80,0.1); padding: 1px 6px; border-radius: 4px; }
.doctor-checks { display: flex; flex-direction: column; gap: 4px; }
.doctor-check { display: flex; align-items: center; gap: 6px; font-size: 12.5px; min-width: 0; }
.doctor-check > span:nth-child(2) { min-width: 0; overflow-wrap: anywhere; }
.check-ok { color: var(--dendro); font-weight: 700; }
.check-fail { color: #e74c3c; font-weight: 700; }
.check-detail { color: var(--moon-dim); font-size: 11px; min-width: 0; overflow-wrap: anywhere; word-break: break-word; }
.doctor-empty { color: var(--moon-dim); font-size: 13px; padding: 8px 0; }

@media (max-width: 600px) {
  .section { padding: 12px 14px; }
  .section-head { align-items: flex-start; flex-wrap: wrap; }
  .section-head h3,
  .section-head .sub-title { flex: 1 1 180px; }
  .log-ops { flex: 1 1 100%; flex-wrap: wrap; }
  .log-level-select { flex: 1 1 120px; width: auto; }
  .brightness-controls,
  .soundfx-controls { width: min(100%, 300px); }
  .brightness-slider,
  .sound-volume-slider { flex: 1 1 auto; width: auto; margin-left: 0; }
  .setting-row :deep(.n-radio-group),
  .setting-row :deep(.n-checkbox-group),
  .section > :deep(.n-radio-group) {
    display: flex;
    flex-wrap: wrap;
    max-width: 100%;
  }
  .ws-time { margin-left: 0; overflow-wrap: anywhere; }
}

@media (max-width: 390px) {
  .section { padding: 10px 12px; }
  .setting-row { gap: 8px 10px; }
  .setting-row > .s-label { flex: 1 1 150px; }
  .brightness-controls,
  .soundfx-controls { width: 100%; }
  .whitelist-add-row { align-items: stretch; }
  .whitelist-input { flex-basis: 100%; width: 100%; }
  .whitelist-add-row :deep(.n-button) { margin-left: auto; }
  .ws-audit { padding: 6px; }
  .audit-entry {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
    gap: 5px 8px;
    padding: 7px 0;
  }
  .audit-time { grid-column: 1 / -1; width: auto; }
  .audit-action { max-width: 100%; overflow-wrap: anywhere; white-space: normal; }
  .audit-target {
    grid-column: 1 / -1;
    overflow: visible;
    text-overflow: clip;
    white-space: normal;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .doctor-issue,
  .doctor-check {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    align-items: start;
  }
  .issue-fixable,
  .check-detail { grid-column: 2; }
}
</style>