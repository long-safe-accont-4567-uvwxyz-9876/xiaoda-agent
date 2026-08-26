<script setup lang="ts">
import { ref, watch } from 'vue'
import { NButton, NSwitch, NTag, NPopconfirm } from 'naive-ui'
import SumeruIcon from '../fx/SumeruIcon.vue'
import AgentAvatarMedia from './AgentAvatarMedia.vue'
import { wallpaperKind } from '../../utils/wallpaper'
import { t } from '../../i18n'
import type { AgentInfo } from '../../api/types'

const props = defineProps<{ agent: AgentInfo }>()

// 壁纸媒体加载失败 → 回退文字首字（此前是 display:none 直接空白）
const avatarFailed = ref(false)
// 换壁纸后重置失败标记：旧图失败不应连累新壁纸的首次尝试
watch(() => props.agent.wallpaper, () => { avatarFailed.value = false })

const emit = defineEmits<{
  (e: 'edit', agent: AgentInfo): void
  (e: 'toggle', agent: AgentInfo, value: boolean): void
  (e: 'remove', agent: AgentInfo): void
}>()
</script>

<template>
  <div class="agent-card glass-panel glass-panel-hover" @click="emit('edit', agent)">
    <div class="card-head">
      <span class="agent-avatar">
        <AgentAvatarMedia v-if="agent.wallpaper && wallpaperKind(agent.wallpaper) !== 'html' && !avatarFailed"
                          :wallpaper="agent.wallpaper" :poster="agent.wallpaper_poster"
                          @error="avatarFailed = true" />
        <span v-else class="avatar-letter">{{ agent.display_name.slice(0, 1) }}</span>
      </span>
      <div class="agent-names">
        <span class="agent-display">{{ agent.display_name }}</span>
        <span class="agent-id">{{ agent.display_name_en }}</span>
      </div>
      <n-switch v-if="!agent.is_main" size="small" :value="agent.enabled"
                @click.stop @update:value="(v: boolean) => emit('toggle', agent, v)" />
    </div>
    <div class="card-meta">
      <n-tag size="small" :bordered="false" type="success">{{ agent.provider }}</n-tag>
      <n-tag size="small" :bordered="false">{{ agent.model || t('agentsView.default') }}</n-tag>
      <n-tag size="small" :bordered="false" :type="agent.builtin || agent.is_main ? 'warning' : 'info'">
        {{ agent.is_main ? t('agentsView.main') : agent.builtin ? t('agentsView.builtin') : t('agentsView.custom') }}
      </n-tag>
      <n-tag v-if="agent.degraded" size="small" :bordered="false" type="warning">{{ t('agentsView.degraded') }}</n-tag>
    </div>
    <div class="card-stats">
      🛠 {{ agent.tool_count ?? '—' }} {{ t('agentsView.toolsUnit') }}
      <span v-if="agent.mcp_servers?.length" class="inline-ic"> · <SumeruIcon name="mcp" :size="12" variant="duo" interactive /> {{ agent.mcp_servers.length }} {{ t('agentsView.mcpUnit') }}</span>
    </div>
    <div class="card-desc">{{ agent.route_description || t('agentsView.noRouteDesc') }}</div>
    <div class="card-actions" v-if="!agent.builtin && !agent.is_main">
      <n-popconfirm @positive-click="emit('remove', agent)">
        <template #trigger>
          <n-button size="tiny" type="error" quaternary @click.stop>{{ t('agentsView.delete') }}</n-button>
        </template>
        {{ t('agentsView.deleteConfirm') }} {{ agent.display_name }}？
      </n-popconfirm>
    </div>
  </div>
</template>

<style scoped>
.agent-card { padding: 14px 16px; cursor: pointer; }

.card-head { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }

.agent-avatar {
  width: 40px; height: 40px;
  border-radius: 50%;
  background: rgba(127, 214, 80, 0.18);
  border: 1px solid var(--glass-border);
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  overflow: hidden;
}
.avatar-letter { font-size: 18px; font-weight: 700; color: var(--dendro); }

.agent-names { display: flex; flex-direction: column; flex: 1; min-width: 0; }
.agent-display { font-weight: 600; }
.agent-id { font-size: 11px; color: var(--moon-dim); font-family: 'JetBrains Mono', monospace; }

.card-meta { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.card-stats { font-size: 12px; color: var(--moon-dim); margin-bottom: 6px; }
.card-desc {
  font-size: 12px; color: var(--moon-dim);
  overflow: hidden; text-overflow: ellipsis;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  min-height: 32px;
}
.card-actions { margin-top: 8px; text-align: right; }
</style>
