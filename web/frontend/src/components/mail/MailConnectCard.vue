<script setup lang="ts">
/**
 * 邮箱连接向导卡：已连接 / 未安装（引导去对话窗）/ 未授权（重授权）三分支。
 * 授权状态经 props 注入，动作向上 emit；复制指令/链接为纯前端行为留在本组件。
 */
import { NAlert, NButton, NEmpty, NSpin, useMessage } from 'naive-ui'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import type { AuthStatus } from './types'

const props = defineProps<{
  status: AuthStatus | null
  checking: boolean
  logging: boolean
  url: string
  /** 连接状态：0=未安装 1=未授权 2=已授权 -1=未知 */
  step: number
}>()

const emit = defineEmits<{
  (e: 'refresh'): void
  (e: 'reauth'): void
  (e: 'check-reauth'): void
  (e: 'go-chat'): void
}>()

const message = useMessage()
const setupInstruction = t('mailView.setupInstruction')

function copySetupInstruction() {
  navigator.clipboard.writeText(setupInstruction).then(() => {
    message.success(t('mailView.copied'))
  }).catch(() => {})
}

function copyAuthUrl() {
  if (props.url) {
    navigator.clipboard.writeText(props.url).then(() => {
      message.success('已复制授权链接')
    }).catch(() => {})
  }
}
</script>

<template>
  <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section animate-slide-up connect-section">
    <h3>{{ t('mailView.connectCard') }}</h3>
    <n-spin :show="checking">
      <div v-if="status" class="connect-body">
        <!-- 已连接 -->
        <div v-if="step === 2" class="connect-success">
          <div class="connect-status-row">
            <span class="connect-dot on">●</span>
            <span class="connect-label">{{ t('mailView.connected') }}</span>
            <span v-if="status.email" class="connect-email">{{ status.email }}</span>
          </div>
          <n-button size="small" quaternary :loading="checking" @click="emit('refresh')">
            {{ t('refresh') }}
          </n-button>
        </div>

        <!-- 未安装 — 引导去对话窗口安装 -->
        <div v-else-if="step === 0" class="connect-wizard">
          <n-alert type="info" :show-icon="true" class="connect-alert">
            {{ t('mailView.notInstalledHint') }}
          </n-alert>

          <div class="setup-intro">{{ t('mailView.setupIntro') }}</div>

          <div class="connect-steps">
            <div class="connect-step">
              <div class="step-num">1</div>
              <div class="step-content">
                <div class="step-title">{{ t('mailView.guideStep1Title') }}</div>
                <div class="step-desc">{{ t('mailView.guideStep1Desc') }}</div>
                <code class="setup-cmd" @click="copySetupInstruction">{{ setupInstruction }}</code>
              </div>
            </div>
            <div class="connect-step">
              <div class="step-num">2</div>
              <div class="step-content">
                <div class="step-title">{{ t('mailView.guideStep2Title') }}</div>
                <div class="step-desc">{{ t('mailView.guideStep2Desc') }}</div>
              </div>
            </div>
            <div class="connect-step">
              <div class="step-num">3</div>
              <div class="step-content">
                <div class="step-title">{{ t('mailView.guideStep3Title') }}</div>
                <div class="step-desc">{{ t('mailView.guideStep3Desc') }}</div>
              </div>
            </div>
          </div>

          <div class="connect-actions">
            <n-button type="primary" @click="emit('go-chat')">
              {{ t('mailView.goToChat') }}
            </n-button>
            <n-button size="small" quaternary :loading="checking" @click="emit('refresh')">
              {{ t('mailView.checkAgain') }}
            </n-button>
          </div>
        </div>

        <!-- CLI 已安装但授权失效 — 直接重新授权 -->
        <div v-else class="connect-wizard">
          <n-alert type="warning" :show-icon="true" class="connect-alert">
            {{ status.error || t('mailView.notAuthorizedHint') }}
          </n-alert>

          <!-- 授权 URL 已获取，显示链接 -->
          <div v-if="url" class="auth-url-section">
            <div class="setup-intro">{{ t('mailView.reAuthDesc') }}</div>
            <a :href="url" target="_blank" rel="noopener" class="auth-url-link">
              {{ url }}
            </a>
            <div class="auth-url-actions">
              <n-button size="small" quaternary @click="copyAuthUrl">
                复制链接
              </n-button>
              <n-button size="small" quaternary :loading="checking" @click="emit('check-reauth')">
                已完成授权？点击检查
              </n-button>
            </div>
          </div>

          <!-- 未获取 URL，显示重新授权按钮 -->
          <template v-else>
            <div class="setup-intro">{{ t('mailView.reAuthDesc') }}</div>
            <div class="connect-actions">
              <n-button type="primary" :loading="logging" @click="emit('reauth')">
                {{ t('mailView.reAuthButton') }}
              </n-button>
              <n-button size="small" quaternary :loading="checking" @click="emit('refresh')">
                {{ t('mailView.checkAgain') }}
              </n-button>
            </div>
          </template>
        </div>
      </div>
      <n-empty v-else style="padding: 24px 0" />
    </n-spin>
  </section></Tilt3D>
</template>

<style scoped>
.section { padding: 16px 18px; margin-bottom: 14px; }
.section h3 { font-size: 14px; color: var(--dendro); margin-bottom: 14px; }

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

.setup-intro {
  font-size: 13px;
  color: var(--wisdom);
  line-height: 1.7;
  padding: 8px 0;
}

.auth-url-section {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.auth-url-link {
  display: block;
  padding: 12px 16px;
  background: rgba(10, 24, 16, 0.6);
  border: 1px solid var(--glass-border);
  border-radius: 8px;
  color: #5cb8ff;
  font-size: 14px;
  word-break: break-all;
  text-decoration: none;
  transition: border-color 0.2s;
}

.auth-url-link:hover {
  border-color: rgba(92, 184, 255, 0.5);
  background: rgba(92, 184, 255, 0.08);
}

.auth-url-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.setup-cmd {
  display: block;
  margin-top: 8px;
  padding: 10px 14px;
  background: rgba(10, 24, 16, 0.6);
  border: 1px solid var(--glass-border);
  border-radius: 8px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--dendro);
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s, transform 0.2s var(--ease-out);
  word-break: break-all;
  line-height: 1.6;
}
.setup-cmd:hover {
  border-color: rgba(143, 229, 96, 0.4);
  background: rgba(143, 229, 96, 0.08);
  transform: translateX(2px);
}
</style>
