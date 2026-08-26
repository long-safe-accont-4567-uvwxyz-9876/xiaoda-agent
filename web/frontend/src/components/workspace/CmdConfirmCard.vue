<script setup lang="ts">
import { ref } from 'vue'
import { NButton } from 'naive-ui'
import { useWorkspaceStore } from '../../stores/workspace'
import { useMessage } from 'naive-ui'
import { t } from '../../i18n'
import { sound } from '../../utils/sound'

/**
 * 命令确认问答卡片（聊天流内联版）
 *
 * Agent 执行非白名单命令时，后端推送 cmd_confirm_request，
 * 前端在消息流末尾渲染本卡片，用户选择后回传决策。
 * 样式参考 ToolCallCard + trae 选择卡片风格。
 */
const props = defineProps<{
  requestId: string
  command: string
  sessionId?: string
}>()

const ws = useWorkspaceStore()
const message = useMessage()
const deciding = ref(false)
const decided = ref<'deny' | 'allow_once' | 'allow' | null>(null)

async function decide(d: 'deny' | 'allow_once' | 'allow', addToWhitelist: boolean) {
  if (deciding.value || decided.value) return
  deciding.value = true
  decided.value = d
  sound.play('toggle')
  try {
    // 延迟回传，让用户先看到决策结果（1.2s），再调 confirmCmd 清空卡片
    await new Promise(r => setTimeout(r, 1200))
    await ws.confirmCmd(props.requestId, d, addToWhitelist, props.command, props.sessionId || '')
  } catch (e: any) {
    decided.value = null  // 失败时恢复按钮，允许重试
    message.error(e.message || t('settings.cmdConfirmFailed'))
  } finally {
    deciding.value = false
  }
}
</script>

<template>
  <div class="cmd-card" :class="decided ? `decided ${decided}` : 'pending'">
    <div class="cmd-head">
      <span class="cmd-icon">🛡️</span>
      <span class="cmd-title">{{ t('settings.cmdConfirmTitle') }}</span>
    </div>
    <div class="cmd-body">
      <code class="cmd-code">{{ command }}</code>
      <p class="cmd-hint">{{ t('settings.cmdConfirmHint') }}</p>
    </div>
    <div v-if="!decided" class="cmd-actions">
      <NButton size="small" :loading="deciding" @click="decide('deny', false)">
        {{ t('settings.cmdConfirmDeny') }}
      </NButton>
      <NButton size="small" :loading="deciding" @click="decide('allow_once', false)">
        {{ t('settings.cmdConfirmAllowOnce') }}
      </NButton>
      <NButton size="small" type="primary" :loading="deciding" @click="decide('allow', true)">
        {{ t('settings.cmdConfirmAllowWhitelist') }}
      </NButton>
    </div>
    <div v-else class="cmd-result">
      <span v-if="decided === 'deny'" class="res-deny">✗ {{ t('settings.cmdConfirmDenied') }}</span>
      <span v-else-if="decided === 'allow_once'" class="res-once">✓ {{ t('settings.cmdConfirmAllowedOnce') }}</span>
      <span v-else class="res-allow">✓ {{ t('settings.cmdConfirmAllowedWhitelist') }}</span>
    </div>
  </div>
</template>

<style scoped>
.cmd-card {
  background: rgba(15, 31, 23, 0.6);
  border: 1px solid rgba(127, 214, 80, 0.3);
  border-radius: 12px;
  padding: 12px 14px;
  margin: 8px 0;
  font-size: 13px;
  box-shadow: 0 2px 12px rgba(127, 214, 80, 0.08);
  animation: cmd-in 0.25s ease-out;
}
@keyframes cmd-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}
.cmd-card.decided {
  border-color: rgba(127, 214, 80, 0.18);
  opacity: 0.85;
}
.cmd-card.decided.deny { border-color: rgba(217, 106, 95, 0.35); }

.cmd-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}
.cmd-icon { font-size: 15px; }
.cmd-title {
  font-weight: 600;
  color: var(--dendro, #7fd650);
  font-size: 13.5px;
}

.cmd-body { margin-bottom: 10px; }
.cmd-code {
  display: block;
  font-family: 'JetBrains Mono', monospace;
  background: rgba(10, 20, 14, 0.45);
  border: 1px solid rgba(127, 214, 80, 0.15);
  border-radius: 6px;
  padding: 6px 9px;
  color: var(--moon, #e8f0e0);
  word-break: break-all;
  white-space: pre-wrap;
  font-size: 12.5px;
}
.cmd-hint {
  font-size: 12px;
  color: var(--moon-dim, #9aa);
  margin: 6px 0 0;
  line-height: 1.5;
}

.cmd-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.cmd-result {
  font-size: 12.5px;
  font-weight: 500;
  padding-top: 2px;
}
.res-deny { color: var(--alert, #d96a5f); }
.res-once { color: var(--moon, #e8f0e0); }
.res-allow { color: var(--dendro, #7fd650); }
</style>
