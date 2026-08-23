<script setup lang="ts">
/**
 * 权限矩阵 Tab：工具按分类分组 + MCP 服务分组。
 * 状态（permissions/permDirty）由父级编辑器编排持有，此处纯展示 + 事件上抛。
 */
import { NButton, NSwitch } from 'naive-ui'
import SumeruIcon from '../fx/SumeruIcon.vue'
import { t } from '../../i18n'
import type { AgentPermissions } from '../../api/types'
import type { ToolGroup } from './types'

defineProps<{
  permissions: AgentPermissions
  dirty: boolean
  groups: ToolGroup[]
}>()

const emit = defineEmits<{
  (e: 'toggle-tool', name: string, value: boolean): void
  (e: 'toggle-mcp', name: string, value: boolean): void
  (e: 'set-group', group: Array<[string, any]>, value: boolean): void
  (e: 'apply'): void
}>()
</script>

<template>
  <div>
    <div class="perm-toolbar">
      <span class="perm-hint">{{ t('agentsView.permHint') }}</span>
      <n-button size="small" type="primary" :disabled="!dirty" @click="emit('apply')">
        {{ t('agentsView.applyPerms') }}
      </n-button>
    </div>
    <div v-for="[cat, group] in groups" :key="cat" class="perm-group">
      <div class="perm-group-head">
        <span>{{ cat }}</span>
        <span class="group-ops">
          <n-button size="tiny" quaternary @click="emit('set-group', group, true)">{{ t('agentsView.allOn') }}</n-button>
          <n-button size="tiny" quaternary @click="emit('set-group', group, false)">{{ t('agentsView.allOff') }}</n-button>
        </span>
      </div>
      <div class="perm-rows">
        <div v-for="[name, info] in group" :key="name" class="perm-row">
          <span class="perm-name" :title="name">{{ name }}</span>
          <span v-if="info.locked" class="perm-lock" :title="info.reason">🔒</span>
          <n-switch v-else size="small" :value="info.enabled"
                    @update:value="(v: boolean) => emit('toggle-tool', name, v)" />
        </div>
      </div>
    </div>
    <div v-if="Object.keys(permissions.mcp_servers || {}).length" class="perm-group">
      <div class="perm-group-head"><span class="inline-ic"><SumeruIcon name="mcp" :size="13" variant="duo" interactive /> {{ t('agentsView.mcpServices') }}</span></div>
      <div class="perm-rows">
        <div v-for="(info, name) in permissions.mcp_servers" :key="name" class="perm-row">
          <span class="perm-name">{{ name }}</span>
          <n-switch size="small" :value="info.enabled" :disabled="info.locked"
                    @update:value="(v: boolean) => emit('toggle-mcp', String(name), v)" />
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.perm-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 12px; gap: 12px;
}
.perm-hint { font-size: 12px; color: var(--wisdom); }

.perm-group { margin-bottom: 14px; }
.perm-group-head {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 13px; color: var(--dendro); font-weight: 600;
  padding-bottom: 4px; border-bottom: 1px solid var(--glass-border);
  margin-bottom: 6px;
}
.group-ops { display: flex; gap: 4px; }

.perm-rows {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
  gap: 4px 16px;
}

.perm-row {
  display: flex; align-items: center; gap: 8px;
  padding: 3px 6px; border-radius: 6px;
}
.perm-row:hover { background: rgba(127, 214, 80, 0.06); }
.perm-name {
  flex: 1; font-size: 12.5px;
  font-family: 'JetBrains Mono', monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.perm-lock { cursor: help; }
</style>
