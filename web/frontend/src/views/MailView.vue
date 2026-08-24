<script setup lang="ts">
/**
 * 邮箱视图（编排层，2026-08-23 大文件拆分专项）：
 * 数据链路在 composables（useMailAuth/useMailSettings/useMailInbox），
 * UI 块拆分至 components/mail/*Card。本文件保留装配与加载时序：
 * onMounted 顺序（授权状态 → 配置 → 放开自动保存 → 统计 → 条件加载收件箱）
 * 与拆分前一致；未连接时下方功能区整体置灰为提示卡。
 */
import { onMounted } from 'vue'
import { NEmpty } from 'naive-ui'
import { t } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'
import ViewTitleIcon from '../components/fx/ViewTitleIcon.vue'
import MailConnectCard from '../components/mail/MailConnectCard.vue'
import MailConfigForm from '../components/mail/MailConfigForm.vue'
import MailStatsCard from '../components/mail/MailStatsCard.vue'
import MailInboxList from '../components/mail/MailInboxList.vue'
import { useMailAuth } from '../composables/useMailAuth'
import { useMailInbox } from '../composables/useMailInbox'
import { useMailSettings } from '../composables/useMailSettings'

const { inbox, inboxLoading, loadInbox } = useMailInbox()
const {
  authStatus, authChecking, authLogging, authUrl, authStep,
  loadAuthStatus, triggerAuthLogin, checkAuthAfterReauth, goToChat,
} = useMailAuth({ onAuthorized: () => loadInbox() })
const { config, stats, configLoading, statsLoading, saving, loadConfig, loadStats, setInitialized } = useMailSettings()

onMounted(async () => {
  await loadAuthStatus()
  await loadConfig()
  setInitialized()  // 加载完成后才允许自动保存
  loadStats()
  if (authStatus.value?.authorized) {
    loadInbox()
  }
})
</script>

<template>
  <div class="mail-view">
    <h2 class="view-title view-title-icon"><ViewTitleIcon name="mail" /> {{ t('mailView.title') }}</h2>

    <!-- 邮箱连接向导 -->
    <MailConnectCard
      :status="authStatus" :checking="authChecking" :logging="authLogging"
      :url="authUrl" :step="authStep"
      @refresh="loadAuthStatus" @reauth="triggerAuthLogin"
      @check-reauth="checkAuthAfterReauth" @go-chat="goToChat"
    />

    <!-- 邮箱未连接时，下方功能置灰提示 -->
    <template v-if="authStep === 2">
      <!-- 5.1 收件处理设置 -->
      <MailConfigForm :config="config" :loading="configLoading" :saving="saving" />

      <!-- 5.2 状态统计 -->
      <MailStatsCard :stats="stats" :loading="statsLoading" @refresh="loadStats" />

      <!-- 5.3 收件箱预览 -->
      <MailInboxList :items="inbox" :loading="inboxLoading" @refresh="loadInbox" />
    </template>

    <!-- 邮箱未连接时的提示 -->
    <Tilt3D v-else :max-x="4" :max-y="6"><section class="glass-panel section animate-slide-up not-connected-hint">
      <n-empty :description="t('mailView.connectFirst')" style="padding: 40px 0" />
    </section></Tilt3D>
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

.not-connected-hint { opacity: 0.6; }
</style>
