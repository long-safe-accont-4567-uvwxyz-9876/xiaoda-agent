<script setup lang="ts">
/**
 * 基础配置 Tab：身份字段 + 模型选择 + 高级折叠 + 路由描述 +
 * capabilities/max_turns/effort/权限模式/记忆域 + 音色与壁纸字段。
 */
import {
  NForm, NFormItem, NInput, NInputNumber, NSelect,
  NDynamicTags, NCollapse, NCollapseItem,
} from 'naive-ui'
import { t } from '../../i18n'
import type { AgentEditModel } from './types'
import AgentVoiceField from './AgentVoiceField.vue'
import AgentWallpaperField from './AgentWallpaperField.vue'

defineProps<{
  editing: AgentEditModel
  isCreate: boolean
  isMain: boolean
  modelOptions: Array<any>
  switchingModel: boolean
}>()

const emit = defineEmits<{
  (e: 'model-change', value: string | null): void
  (e: 'advanced-input'): void
}>()

const effortOptions = ['low', 'medium', 'high'].map(v => ({ label: v, value: v }))
const permModeOptions = ['default', 'dev', 'strict'].map(v => ({ label: v, value: v }))
const memScopeOptions = ['shared', 'isolated'].map(v => ({ label: v, value: v }))
</script>

<template>
  <n-form label-placement="left" label-width="130">
    <n-form-item :label="t('agentsView.name')" v-if="isCreate">
      <n-input v-model:value="editing.name" :placeholder="t('agentsView.namePlaceholder')" />
    </n-form-item>
    <n-form-item :label="t('agentsView.displayName')">
      <n-input v-model:value="editing.display_name" :placeholder="t('agentsView.displayNamePh')" />
    </n-form-item>
    <n-form-item label="English Name" v-if="!isMain">
      <n-input :value="editing.display_name_en" disabled placeholder="Auto-translated" />
    </n-form-item>
    <n-form-item :label="t('agentsView.model')" v-if="!isMain">
      <n-select :value="(editing.provider && editing.model) ? `${editing.provider}|${editing.model}` : null"
                :options="modelOptions"
                :loading="switchingModel" filterable tag
                :placeholder="t('agentsView.modelPh')"
                @update:value="(v: string | null) => emit('model-change', v)" />
    </n-form-item>
    <n-form-item :label="t('agentsView.advanced')" v-if="!isMain">
      <n-collapse :default-expanded-names="[]">
        <n-collapse-item :title="t('agentsView.advancedTitle')" name="advanced">
          <n-form label-placement="left" label-width="130" style="margin-top: 4px">
            <n-form-item label="base_url">
              <n-input v-model:value="editing.base_url"
                       :placeholder="t('agentsView.baseUrlPh')"
                       @update:value="emit('advanced-input')" />
            </n-form-item>
            <n-form-item label="api_key_env">
              <n-input v-model:value="editing.api_key_env"
                       :placeholder="t('agentsView.apiKeyPh')"
                       @update:value="emit('advanced-input')" />
            </n-form-item>
          </n-form>
        </n-collapse-item>
      </n-collapse>
    </n-form-item>
    <n-form-item :label="t('agentsView.routeDesc')" v-if="!isMain">
      <n-input v-model:value="editing.route_description" type="textarea" :rows="2"
               :placeholder="t('agentsView.routeDescPh')" />
    </n-form-item>
    <n-form-item label="capabilities" v-if="!isMain">
      <n-dynamic-tags v-model:value="editing.capabilities" />
    </n-form-item>
    <n-form-item label="max_turns" v-if="!isMain">
      <n-input-number v-model:value="editing.max_turns" :min="1" :max="30" />
    </n-form-item>
    <n-form-item label="effort" v-if="!isMain">
      <n-select v-model:value="editing.effort" :options="effortOptions" />
    </n-form-item>
    <n-form-item :label="t('agentsView.permMode')" v-if="!isMain">
      <n-select v-model:value="editing.permission_mode" :options="permModeOptions" />
    </n-form-item>
    <n-form-item :label="t('agentsView.memoryScope')" v-if="!isMain">
      <n-select v-model:value="editing.memory_scope" :options="memScopeOptions" />
    </n-form-item>
    <n-form-item label="voice_ref">
      <AgentVoiceField v-model:voice-ref="editing.voice_ref" :agent-name="editing.name || ''" />
    </n-form-item>
    <n-form-item :label="t('agentsView.backdrop')">
      <AgentWallpaperField v-model:wallpaper="editing.wallpaper"
                           :agent-name="editing.name || ''" :is-create="isCreate" />
    </n-form-item>
  </n-form>
</template>
