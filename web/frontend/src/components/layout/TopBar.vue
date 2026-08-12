<script setup lang="ts">
import { onMounted, onBeforeUnmount, computed, ref } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useAgentsStore } from '../../stores/agents'
import { getWsClient } from '../../api/ws'
import EmotionAvatar from '../chat/EmotionAvatar.vue'
import { t } from '../../i18n'
import { refreshAgentNames } from '../../utils/agentNames'

const chat = useChatStore()
const agentsStore = useAgentsStore()
const ws = getWsClient()

defineProps<{ mobileSidebarOpen: boolean }>()
defineEmits<{ 'toggle-sidebar': [] }>()

function onConfigChanged(e: any) {
  // display_name 等变更 → 全局联动刷新 Agent 列表 + 名称映射
  if (e.domain === 'agents') {
    agentsStore.load().catch(() => {})
    refreshAgentNames()
  }
}

const failedAvatars = ref(new Set<string>())

function onAvatarError(name: string) {
  failedAvatars.value = new Set(failedAvatars.value).add(name)
}

onMounted(() => {
  if (!agentsStore.agents.length) agentsStore.load().catch(() => {})
  ws.on('config_changed', onConfigChanged)
})

onBeforeUnmount(() => ws.off('config_changed', onConfigChanged))

const enabledAgents = computed(() =>
  agentsStore.agents.filter(a => a.enabled))

const stageText: Record<string, string> = {
  thinking: '🌿 ' + t('topBar.thinking') + '...',
  tool: '🛠 ' + t('topBar.usingTool') + '...',
  replying: '✍️ ' + t('topBar.replying') + '...',
}

const connectionStatusText = computed(() => chat.wsConnected
  ? t('topBar.connected')
  : chat.wsReconnecting ? t('topBar.reconnecting') : t('topBar.disconnected'))
</script>

<template>
  <header class="topbar">
    <button class="mobile-menu-button" type="button" :aria-label="t('nav.openNavigation')"
            :aria-expanded="mobileSidebarOpen" aria-controls="app-sidebar"
            @click="$emit('toggle-sidebar')">☰</button>
    <div class="agent-switcher">
      <button
        v-for="a in enabledAgents"
        :key="a.name"
        class="agent-chip"
        :class="{ active: chat.currentAgent === a.name }"
        :aria-pressed="chat.currentAgent === a.name"
        :title="`${a.display_name} · ${a.model || a.provider} · ${a.tool_count ?? '?'} ${t('topBar.toolsCount')}`"
        @click="chat.setAgent(a.name)"
      >
        <span class="chip-avatar">
          <img v-if="a.wallpaper && !failedAvatars.has(a.name)" :src="a.wallpaper" class="chip-avatar-img"
               :alt="a.display_name" @error="onAvatarError(a.name)" />
          <template v-else>{{ a.display_name.slice(0, 1) }}</template>
        </span>
        <span class="chip-name">{{ a.display_name }}</span>
      </button>
    </div>

    <div class="brand-signature" :aria-label="t('topBar.signature')">
      <span class="sig-leaf">🌿</span>
      <span class="sig-text">{{ t('brand_signature.text') }}</span>
    </div>

    <div v-if="chat.isProcessing" class="stage-indicator">
      {{ chat.statusText || stageText[chat.currentStage] || ('🌿 ' + t('topBar.processing') + '...') }}
    </div>

    <div class="topbar-right">
      <EmotionAvatar />
      <!-- 三态连接灯：绿=已连接 / 黄=重连中 / 红=已断开（无限后台重连中） -->
      <span class="connection-status" role="status"
            :class="chat.wsConnected ? 'green' : (chat.wsReconnecting ? 'yellow' : 'red')"
            :title="connectionStatusText">
        <span class="status-dot"></span>
        <span class="status-text">{{ connectionStatusText }}</span>
      </span>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  height: var(--topbar-height);
  background: rgba(15, 31, 23, 0.55);
  backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--glass-border);
  flex-shrink: 0;
}

.mobile-menu-button {
  display: none;
  width: 36px;
  height: 36px;
  border: 1px solid var(--glass-border);
  border-radius: 9px;
  background: rgba(20, 40, 28, 0.5);
  color: var(--moon);
  cursor: pointer;
}

.agent-switcher {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  flex: 1;
  min-width: 0;
  padding: 4px 0;
}

.agent-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px 4px 4px;
  background: rgba(20, 40, 28, 0.5);
  border: 1px solid var(--glass-border);
  border-radius: 18px;
  color: var(--moon-dim);
  cursor: pointer;
  white-space: nowrap;
  transition: transform 0.25s var(--ease-spring, var(--ease-out)), border-color 0.25s, box-shadow 0.25s, background-color 0.25s, color 0.25s;
  transform-style: preserve-3d;
}

.agent-chip:hover {
  transform: perspective(400px) translateZ(6px) rotateX(-4deg);
  border-color: rgba(127, 214, 80, 0.4);
}

.agent-chip:active {
  transform: perspective(400px) translateZ(2px) scale(0.95);
  transition-duration: 0.08s;
}

.agent-chip.active {
  border-color: var(--dendro);
  color: var(--dendro);
  box-shadow: 0 0 12px rgba(127, 214, 80, 0.25);
  transform: perspective(400px) translateZ(8px);
}

.chip-avatar {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: rgba(127, 214, 80, 0.18);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
  flex-shrink: 0;
  overflow: hidden;
}

.chip-avatar-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: 50%;
}

.chip-name { font-size: 13px; }

.stage-indicator {
  font-size: 13px;
  color: var(--wisdom);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 280px;
  animation: breathe 2s ease-in-out infinite;
}

.brand-signature {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px;
  background: linear-gradient(90deg, rgba(127, 214, 80, 0.12), rgba(232, 213, 163, 0.08));
  border: 1px solid rgba(127, 214, 80, 0.25);
  border-radius: 16px;
  font-size: 12px;
  color: var(--wisdom);
  font-family: 'Noto Serif SC', serif;
  white-space: nowrap;
  flex-shrink: 0;
  pointer-events: none;
  user-select: none;
}
.sig-leaf {
  font-size: 14px;
  animation: leaf-sway 3s ease-in-out infinite;
}
@keyframes leaf-sway {
  0%, 100% { transform: rotate(-8deg); }
  50% { transform: rotate(8deg); }
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}

.connection-status {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--moon-dim);
  font-size: 12px;
}
.status-dot {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.connection-status.green .status-dot { background: var(--dendro); box-shadow: 0 0 8px var(--dendro); }
.connection-status.yellow .status-dot { background: var(--wisdom, #f0c05a); box-shadow: 0 0 8px var(--wisdom, #f0c05a); animation: breathe 1.2s ease-in-out infinite; }
.connection-status.red .status-dot { background: var(--alert); box-shadow: 0 0 8px var(--alert); }

@keyframes breathe {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

@media (max-width: 768px) {
  .mobile-menu-button { display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .chip-name { display: none; }
  .stage-indicator { display: none; }
  .sig-text { display: none; }
  .brand-signature { padding: 4px 8px; }
  .status-text { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
}
</style>
