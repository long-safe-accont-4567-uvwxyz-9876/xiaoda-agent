<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import {
  NButton, NSwitch, NRadioGroup, NRadioButton, NInput, NModal,
  NSelect, NSlider, NCheckbox, NPopconfirm, NTag, NTabs, NTabPane, useMessage,
} from 'naive-ui'
import { get, put, post } from '../api'
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
const lanInfo = ref<{ localhost: string; lan_urls: string[]; port: number } | null>(null)

onMounted(async () => {
  await ui.loadRemote()
  try {
    const p = await get('/system/permission-mode')
    permissionMode.value = p.mode
    permissionOptions.value = p.options
  } catch (e: any) { message.error(e.message) }
  loadLogs()
  loadLanInfo()
  // 工作目录授权状态、白名单、审计日志
  ws.loadStatus()
  ws.loadWhitelist()
  ws.loadAudit()
})

async function loadLanInfo() {
  try {
    const data = await get('/system/lan-addresses')
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
    <h2 class="view-title">{{ t('settings.title') }}</h2>

    <n-tabs type="line" animated display-directive="show" v-model:value="activeTab">
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
                :value="ui.soundVolume"
                :min="0"
                :max="1"
                :step="0.05"
                :disabled="!ui.soundFx"
                style="width: 160px; margin-left: 12px"
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
                :value="ui.manualBrightness"
                :min="0.5"
                :max="1.5"
                :step="0.05"
                :disabled="ui.autoBrightness"
                style="width: 200px; margin-left: 12px"
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
          <h3>{{ t('settings.workspaceAuth') }}</h3>
          <p class="apikey-desc">{{ t('settings.workspaceAuthDesc') }}</p>

          <!-- 授权状态 -->
          <template v-if="ws.authorized">
            <div class="setting-row">
              <span class="s-label">{{ t('settings.workspaceCurrent') }}</span>
              <span class="ws-path" :title="ws.currentPath">📁 {{ ws.currentPath }}</span>
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
          <div class="setting-row">
            <n-input v-model:value="newCmd" :placeholder="t('settings.cmdWhitelistAddPlaceholder')" size="small" style="flex:1" @keyup.enter="onAddCmd" />
            <n-button size="small" type="primary" @click="onAddCmd">{{ t('settings.cmdWhitelistAdd') }}</n-button>
          </div>
          <div v-if="ws.whitelist.length" class="ws-whitelist">
            <n-tag v-for="cmd in ws.whitelist" :key="cmd" closable size="small" @close="onRemoveCmd(cmd)">{{ cmd }}</n-tag>
          </div>
          <p v-else class="perm-desc">{{ t('settings.cmdWhitelistEmpty') }}</p>

          <!-- 审计日志 -->
          <div class="section-head" style="margin-top:14px">
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
      </n-tab-pane>

      <!-- 系统与日志 -->
      <n-tab-pane name="system" :tab="t('settings.tabs.system')">
        <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
          <div class="section-head">
            <h3>{{ t('settings.logViewer') }}</h3>
            <div class="log-ops">
              <n-select v-model:value="logLevel" :options="['INFO', 'WARNING', 'ERROR'].map(l => ({ label: l, value: l }))"
                        :placeholder="t('settings.logLevel')" clearable size="small" style="width: 120px"
                        @update:value="loadLogs" />
              <n-button size="small" :loading="logLoading" @click="loadLogs">{{ t('refresh') }}</n-button>
            </div>
          </div>
          <pre class="log-box">{{ logs.join('\n') || t('settings.logEmpty') }}</pre>
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
             style="width: min(420px, 94vw)">
      <div style="margin-bottom: 16px; font-size: 13.5px">
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
  </div>
</template>

<style scoped>
.view-title { font-family: 'Noto Serif SC', serif; margin-bottom: 14px; }

.section { padding: 16px 18px; margin-bottom: 14px; }
.section h3 { font-size: 14px; color: var(--dendro); margin-bottom: 14px; }
.section.danger { border-color: rgba(217, 106, 95, 0.3); }
.section-head { display: flex; align-items: center; justify-content: space-between; }
.section-head h3 { margin: 0; }

.setting-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 0; gap: 16px; flex-wrap: wrap;
}
.s-label { font-size: 13.5px; }
.soundfx-controls { display: flex; align-items: center; }
.url-link {
  font-size: 13.5px;
  color: var(--dendro);
  cursor: pointer;
  font-family: 'JetBrains Mono', monospace;
  word-break: break-all;
}
.url-link:hover { text-decoration: underline; }

.perm-desc { font-size: 12.5px; color: var(--wisdom); margin-top: 10px; }
.apikey-desc { font-size: 12.5px; color: var(--wisdom); margin: 0 0 12px; }

.log-ops { display: flex; gap: 8px; }
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
.ws-audit {
  margin-top: 8px;
  max-height: 240px;
  overflow-y: auto;
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.18);
  padding: 6px 8px;
}
.audit-entry {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
  font-size: 12px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
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
}
</style>
