<script setup lang="ts">
import { onMounted, onBeforeUnmount, computed, ref } from 'vue'
import { useChatStore } from '../../stores/chat'
import { useAgentsStore } from '../../stores/agents'
import { getWsClient } from '../../api/ws'
import EmotionAvatar from '../chat/EmotionAvatar.vue'
import { t } from '../../i18n'
import { refreshAgentNames } from '../../utils/agentNames'

defineProps<{ mobileSidebarOpen?: boolean }>()
const emit = defineEmits<{ 'toggle-sidebar': [] }>()

const chat = useChatStore()
const agentsStore = useAgentsStore()
const ws = getWsClient()

// 头像加载失败的 Agent 名称集合（按名称维护，保证首字回退可见）
const failedAvatars = ref<Set<string>>(new Set())

function onConfigChanged(e: any) {
  // display_name 等变更 → 全局联动刷新 Agent 列表 + 名称映射
  if (e.domain === 'agents') {
    agentsStore.load().catch(() => {})
    refreshAgentNames()
  }
}

// Agent 头像图片加载失败时隐藏破图，避免控制台报错与裂图显示
function onAvatarError(name: string) {
  const next = new Set(failedAvatars.value)
  next.add(name)
  failedAvatars.value = next
}

onMounted(() => {
  if (!agentsStore.agents.length) agentsStore.load().catch(() => {})
  ws.on('config_changed', onConfigChanged)
})

onBeforeUnmount(() => ws.off('config_changed', onConfigChanged))

const enabledAgents = computed(() =>
  agentsStore.agents.filter(a => a.enabled))

const connectionStatusText = computed(() =>
  chat.wsConnected ? t('topBar.connected')
    : chat.wsReconnecting ? t('topBar.reconnecting') + '...'
    : t('topBar.disconnected'))

const stageText: Record<string, string> = {
  thinking: '🌿 ' + t('topBar.thinking') + '...',
  tool: '🛠 ' + t('topBar.usingTool') + '...',
  replying: '✍️ ' + t('topBar.replying') + '...',
}
</script>

<template>
  <header class="topbar">
    <button
      type="button"
      class="menu-toggle toggle-sidebar"
      :aria-expanded="mobileSidebarOpen"
      aria-controls="app-sidebar"
      :aria-label="t('nav.mainNavigation')"
      @click="emit('toggle-sidebar')"
    >
      <span class="menu-bar"></span>
      <span class="menu-bar"></span>
      <span class="menu-bar"></span>
    </button>

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
               @error="onAvatarError(a.name)" />
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
      <span class="status-dot"
            role="status"
            :class="chat.wsConnected ? 'green' : (chat.wsReconnecting ? 'yellow' : 'red')"
            :title="connectionStatusText"></span>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  position: relative;
  z-index: 3;
  display: flex;
  height: var(--topbar-height);
  flex-shrink: 0;
  align-items: center;
  gap: 12px;
  padding: 0 clamp(12px, 1.5vw, 22px);
  border-bottom: 1px solid var(--line);
  background: linear-gradient(90deg, rgba(20, 40, 28, 0.55), rgba(18, 36, 26, 0.45));
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.14);
  backdrop-filter: blur(18px) saturate(1.08);
  -webkit-backdrop-filter: blur(18px) saturate(1.08);
}

.topbar::after {
  content: '';
  position: absolute;
  right: 0;
  bottom: -1px;
  left: 0;
  height: 1px;
  background: linear-gradient(90deg, rgba(145, 232, 102, 0.24), transparent 36%, rgba(112, 199, 220, 0.09));
  pointer-events: none;
}

.menu-toggle {
  display: none;
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  flex-direction: column;
  justify-content: center;
  gap: 4px;
  padding: 9px;
  border: 1px solid var(--line);
  border-radius: var(--control-radius);
  background: rgba(26, 48, 36, 0.7);
  cursor: pointer;
  transition: border-color var(--motion-fast), background-color var(--motion-fast);
}

.menu-toggle:hover {
  border-color: var(--line-strong);
  background: rgba(36, 60, 46, 0.82);
}

.menu-toggle:focus-visible {
  outline: 2px solid var(--dendro-bright);
  outline-offset: 2px;
}

.menu-bar {
  display: block;
  width: 100%;
  height: 1.5px;
  border-radius: 1px;
  background: var(--moon-soft);
}

.agent-switcher {
  display: flex;
  flex: 1;
  min-width: 0;
  gap: 7px;
  padding: 5px 0;
  overflow-x: auto;
  scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
}
.agent-switcher::-webkit-scrollbar { display: none; }

.agent-chip {
  display: flex;
  min-height: 36px;
  flex-shrink: 0;
  align-items: center;
  gap: 7px;
  padding: 4px 11px 4px 4px;
  border: 1px solid var(--line-soft);
  border-radius: 8px;
  background: rgba(24, 44, 33, 0.58);
  color: var(--moon-dim);
  cursor: pointer;
  white-space: nowrap;
  transition: transform var(--motion-fast) var(--ease-out), border-color var(--motion-fast), box-shadow var(--motion-fast), background-color var(--motion-fast), color var(--motion-fast);
}

.agent-chip:hover {
  border-color: rgba(145, 232, 102, 0.24);
  background: rgba(32, 54, 42, 0.7);
  color: var(--moon);
  transform: translateY(-1px);
}

.agent-chip:focus-visible {
  outline: 2px solid var(--dendro-bright);
  outline-offset: 2px;
}

.agent-chip:active {
  transform: translateY(1px);
  transition-duration: 70ms;
}

.agent-chip.active {
  border-color: rgba(145, 232, 102, 0.38);
  background: linear-gradient(135deg, rgba(145, 232, 102, 0.14), rgba(85, 217, 178, 0.07));
  box-shadow: inset 0 0 0 1px rgba(145, 232, 102, 0.06), 0 5px 16px rgba(0, 0, 0, 0.16);
  color: var(--dendro-bright);
}

.chip-avatar {
  display: flex;
  width: 27px;
  height: 27px;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  border: 1px solid rgba(145, 232, 102, 0.2);
  border-radius: 6px;
  background: rgba(145, 232, 102, 0.1);
  color: var(--dendro-bright);
  font-size: 12px;
  font-weight: 700;
}

.chip-avatar-img {
  width: 100%;
  height: 100%;
  border-radius: 5px;
  object-fit: cover;
}

.chip-name {
  max-width: 140px;
  overflow: hidden;
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
}

.stage-indicator {
  max-width: min(24vw, 300px);
  overflow: hidden;
  padding: 5px 9px;
  border: 1px solid rgba(239, 189, 100, 0.14);
  border-radius: var(--control-radius);
  background: rgba(239, 189, 100, 0.055);
  color: var(--wisdom);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
  animation: breathe 2s ease-in-out infinite;
}

.brand-signature {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 6px;
  padding: 5px 9px;
  border-left: 1px solid rgba(145, 232, 102, 0.18);
  color: rgba(240, 216, 154, 0.76);
  font-family: 'Noto Serif SC', serif;
  font-size: 11px;
  pointer-events: none;
  user-select: none;
  white-space: nowrap;
}

.sig-leaf {
  font-size: 13px;
  opacity: 0.82;
}

.topbar-right {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 12px;
  padding-left: 4px;
}

.status-dot {
  width: 10px;
  height: 10px;
}
.status-dot.green { background: var(--dendro); box-shadow: 0 0 0 3px rgba(145, 232, 102, 0.1), 0 0 8px rgba(145, 232, 102, 0.52); }
.status-dot.yellow { background: var(--warning); box-shadow: 0 0 0 3px rgba(239, 189, 100, 0.1), 0 0 8px rgba(239, 189, 100, 0.5); animation: breathe 1.2s ease-in-out infinite; }
.status-dot.red { background: var(--alert); box-shadow: 0 0 0 3px rgba(240, 124, 114, 0.1), 0 0 8px rgba(240, 124, 114, 0.48); }

@keyframes breathe {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

@media (prefers-reduced-motion: reduce) {
  .stage-indicator,
  .status-dot.yellow {
    animation: none;
  }
}

@media (max-width: 1024px) {
  .brand-signature {
    display: none;
  }
}

@media (max-width: 768px) {
  .topbar {
    gap: 8px;
    padding: 0 10px;
  }

  .menu-toggle { display: inline-flex; }
  .chip-name { display: none; }
  .stage-indicator { display: none; }
  .agent-chip { padding-right: 4px; }
  .topbar-right { gap: 8px; }
}

@media (max-width: 420px) {
  .topbar-right :deep(.emotion-avatar) {
    display: none;
  }
}
</style>
