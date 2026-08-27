<script setup lang="ts">
/**
 * Agent 编辑器弹窗外壳：Tab 编排 + 底部保存栏。
 * 状态与保存链路在 useAgentEditor；基础表单/权限/测试/表情包各自成组件。
 * 通过 defineExpose(open) 供视图调用。
 */
import { ref, watch, nextTick } from 'vue'
import { NModal, NTabs, NTabPane, NButton, NInput, NDynamicTags } from 'naive-ui'
import { t } from '../../i18n'
import { useAgentEditor } from '../../composables/useAgentEditor'
import { shakeEl } from '../../utils/gsapMotion'
import type { AgentEditModel } from './types'
import AgentBasicForm from './AgentBasicForm.vue'
import AgentPermissionsPanel from './AgentPermissionsPanel.vue'
import AgentTestPanel from './AgentTestPanel.vue'
import AgentStickerPanel from './AgentStickerPanel.vue'

const {
  showEditor, isCreate, editing, personality, permissions, permDirty, saving,
  saveFailedTick, isMain, toolGroups, modelOptions, switchingModel,
  open, save, close, onModelChange, onAdvancedInput,
  togglePerm, toggleMcpPerm, groupSetAll, applyPermissions,
} = useAgentEditor()

// 保存失败的差异化反馈：toast 报原因（composable 内），震颤指向弹窗本体。
// 失败计数在弹窗打开期间才消费，避免上次会话的残留触发
const modalBodyEl = ref<HTMLElement | null>(null)
watch(saveFailedTick, async () => {
  if (!showEditor.value) return
  await nextTick()
  if (modalBodyEl.value) shakeEl(modalBodyEl.value)
})

function openEditor(agent: AgentEditModel | null) {
  open(agent)
}

defineExpose({ open: openEditor })
</script>

<template>
  <n-modal v-model:show="showEditor" preset="card" class="agent-modal"
           :title="isCreate ? t('agentsView.createSub') : `${t('agentsView.editDot')}${editing.display_name || editing.name}`"
           style="width: min(860px, 94vw); max-height: 88vh; overflow-y: auto;">
    <div ref="modalBodyEl">
    <n-tabs type="line" animated>
      <n-tab-pane name="base" :tab="t('agentsView.basicConfig')">
        <AgentBasicForm :editing="editing"
                        :is-create="isCreate"
                        :is-main="isMain"
                        :model-options="modelOptions"
                        :switching-model="switchingModel"
                        @model-change="onModelChange"
                        @advanced-input="onAdvancedInput" />
      </n-tab-pane>

      <n-tab-pane name="perm" :tab="t('agentsView.permissions')" v-if="!isCreate">
        <AgentPermissionsPanel :permissions="permissions"
                               :dirty="permDirty"
                               :groups="toolGroups"
                               @toggle-tool="togglePerm"
                               @toggle-mcp="toggleMcpPerm"
                               @set-group="groupSetAll"
                               @apply="applyPermissions" />
      </n-tab-pane>

      <n-tab-pane name="personality" :tab="t('agentsView.personality')">
        <n-input v-model:value="personality" type="textarea" :rows="14"
                 :placeholder="t('agentsView.personalityPh')" />
      </n-tab-pane>

      <n-tab-pane name="ack" tab="随心即言" v-if="isMain">
        <div style="margin-bottom: 12px; color: var(--text-secondary); font-size: 13px;">
          自定义收到消息时的提示语。每条一行，发送时随机选一条。留空则使用默认"小妲收到啦，正在想～🌿"。
          <br>提示：如果文本中包含 agent 原名（如"纳西妲"），会自动替换为当前显示名；不含则原样输出。
        </div>
        <n-dynamic-tags v-model:value="editing.ack_messages" type="success"
                        :max="20" />
      </n-tab-pane>

      <n-tab-pane name="test" :tab="t('agentsView.test')" v-if="!isCreate">
        <AgentTestPanel :agent-name="editing.name || ''"
                        :display-name="editing.display_name || ''" />
      </n-tab-pane>

      <n-tab-pane name="stickers" :tab="t('agentsView.stickers')" v-if="!isCreate">
        <AgentStickerPanel :agent-name="editing.name || ''" />
      </n-tab-pane>
    </n-tabs>
    </div>

    <template #footer>
      <div class="modal-footer">
        <n-button @click="close">{{ t('cancel') }}</n-button>
        <n-button type="primary" :loading="saving" @click="save">
          {{ isCreate ? t('agentsView.create') : t('save') }}
        </n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.modal-footer { display: flex; justify-content: flex-end; gap: 10px; }
</style>
