<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  NButton, NSwitch, NRadioGroup, NRadioButton, NInput, NModal,
  NSelect, useMessage,
} from 'naive-ui'
import { get, put, post } from '../api'
import { useUiStore } from '../stores/ui'
import { useAuthStore } from '../stores/auth'
import { useRouter } from 'vue-router'

const message = useMessage()
const ui = useUiStore()
const auth = useAuthStore()
const router = useRouter()

const permissionMode = ref('')
const permissionOptions = ref<string[]>([])
const logs = ref<string[]>([])
const logLevel = ref<string | null>(null)
const logLoading = ref(false)
const showRestart = ref(false)
const restartConfirmText = ref('')

onMounted(async () => {
  await ui.loadRemote()
  try {
    const p = await get('/system/permission-mode')
    permissionMode.value = p.mode
    permissionOptions.value = p.options
  } catch (e: any) { message.error(e.message) }
  loadLogs()
})

async function setPermMode(mode: string) {
  try {
    await put('/system/permission-mode', { mode })
    permissionMode.value = mode
    message.success(`权限模式已切换为 ${mode.toUpperCase()} ✓ 即时生效`)
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
    message.warning('服务重启中…页面将在恢复后自动重连')
    showRestart.value = false
  } catch (e: any) { message.error(e.message) }
}

function logout() {
  auth.logout()
  router.replace('/login')
}

const permDesc: Record<string, string> = {
  default: '默认 — 危险操作需要确认',
  dev: '开发 — 放宽部分写权限（自用调试）',
  strict: '严格 — 拒绝一切写/执行类工具',
}
</script>

<template>
  <div class="settings-view">
    <h2 class="view-title">⚙️ 系统设置</h2>

    <section class="glass-panel section">
      <h3>界面与特效</h3>
      <div class="setting-row">
        <span class="s-label">草元素粒子密度</span>
        <n-radio-group :value="ui.particles" @update:value="ui.setParticles">
          <n-radio-button value="off">关</n-radio-button>
          <n-radio-button value="low">低</n-radio-button>
          <n-radio-button value="medium">中</n-radio-button>
          <n-radio-button value="high">高</n-radio-button>
        </n-radio-group>
      </div>
      <div class="setting-row">
        <span class="s-label">3D 卡片交互</span>
        <n-switch :value="ui.tilt3d" @update:value="ui.setTilt3d" />
      </div>
      <div class="setting-row">
        <span class="s-label">自动朗读回复</span>
        <n-switch :value="ui.autoSpeak" @update:value="(v: boolean) => ui.setAutoSpeak(v).then(() => message.success('已生效 ✓')).catch((e: any) => message.error(e.message))" />
      </div>
    </section>

    <section class="glass-panel section">
      <h3>全局权限模式</h3>
      <n-radio-group :value="permissionMode" @update:value="setPermMode">
        <n-radio-button v-for="m in permissionOptions" :key="m" :value="m">
          {{ m.toUpperCase() }}
        </n-radio-button>
      </n-radio-group>
      <p class="perm-desc">{{ permDesc[permissionMode] || '' }}</p>
    </section>

    <section class="glass-panel section">
      <div class="section-head">
        <h3>日志查看器</h3>
        <div class="log-ops">
          <n-select v-model:value="logLevel" :options="['INFO', 'WARNING', 'ERROR'].map(l => ({ label: l, value: l }))"
                    placeholder="级别" clearable size="small" style="width: 120px"
                    @update:value="loadLogs" />
          <n-button size="small" :loading="logLoading" @click="loadLogs">刷新</n-button>
        </div>
      </div>
      <pre class="log-box">{{ logs.join('\n') || '（空）' }}</pre>
    </section>

    <section class="glass-panel section">
      <h3>API Key 配置</h3>
      <p class="apikey-desc">配置和管理 API 密钥，测试密钥是否有效</p>
      <div class="setting-row">
        <span class="s-label">打开 API Key 设置向导</span>
        <n-button type="primary" secondary @click="router.push('/setup')">打开 API Key 设置</n-button>
      </div>
    </section>

    <section class="glass-panel section danger">
      <h3>危险操作</h3>
      <div class="setting-row">
        <span class="s-label">重启 Agent 服务（systemd 自动拉起）</span>
        <n-button type="error" secondary @click="showRestart = true">重启服务</n-button>
      </div>
      <div class="setting-row">
        <span class="s-label">退出登录</span>
        <n-button secondary @click="logout">退出</n-button>
      </div>
    </section>

    <n-modal v-model:show="showRestart" preset="card" title="⚠ 确认重启"
             style="width: min(420px, 94vw)">
      <p style="margin-bottom: 12px; font-size: 13.5px">
        重启期间所有通道（Web/QQ）暂时中断。输入 <b>RESTART</b> 以确认：
      </p>
      <n-input v-model:value="restartConfirmText" placeholder="RESTART" />
      <template #footer>
        <div style="display:flex; justify-content:flex-end; gap:10px">
          <n-button @click="showRestart = false">取消</n-button>
          <n-button type="error" :disabled="restartConfirmText !== 'RESTART'" @click="doRestart">
            确认重启
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
</style>
