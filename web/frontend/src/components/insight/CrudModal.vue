<script setup lang="ts">
/**
 * Insight 共享 CRUD 模态框：六类实体（记忆/笔记/学习/本能/实体/关系）的表单。
 * 表单初值经 seed prop 注入（useInsightCrud 构造），确定时 emit ok(form 快照)；
 * 校验与 API 调用留在视图层编排（原 handleModalOk 行为不变）。
 */
import { reactive, watch } from 'vue'
import {
  NButton, NForm, NFormItem, NInput, NModal, NSelect, NSlider, NSpace,
} from 'naive-ui'
import { t } from '../../i18n'
import type { CrudType } from './types'

const props = defineProps<{
  show: boolean
  type: CrudType | null
  title: string
  seed: Record<string, any>
  editing: boolean
}>()

const emit = defineEmits<{
  (e: 'update:show', v: boolean): void
  (e: 'ok', form: Record<string, any>): void
}>()

const formModel = reactive<Record<string, any>>({})

// 打开时用 seed 克隆重建表单（原 openAdd/openEdit 清空+回填逻辑等价）
watch(() => props.show, (v) => {
  if (!v) return
  Object.keys(formModel).forEach(k => delete formModel[k])
  Object.assign(formModel, props.seed)
})

const noteKindOptions = [
  { label: t('insightView.noteLabel'), value: 'note' },
  { label: t('insightView.taskLabel'), value: 'task' },
  { label: t('insightView.ideaLabel'), value: 'idea' },
]
const priorityOptions = [
  { label: t('insightView.impLow'), value: 'low' },
  { label: t('insightView.impMed'), value: 'medium' },
  { label: t('insightView.impHigh'), value: 'high' },
]

function onOk() {
  emit('ok', { ...formModel })
}
</script>

<template>
  <n-modal :show="show" preset="card" :title="title" style="max-width: 480px"
           @update:show="emit('update:show', $event)">
    <!-- 记忆表单 -->
    <n-form v-if="type === 'memory'" label-placement="left" label-width="70">
      <n-form-item :label="t('insightView.labelSummary')">
        <n-input v-model:value="formModel.summary" type="textarea" :placeholder="t('insightView.memoryContentPh')" :rows="3" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelImportance')">
        <n-slider v-model:value="formModel.importance" :min="0" :max="1" :step="0.1" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelEmotionTag')">
        <n-input v-model:value="formModel.emotion_label" :placeholder="t('insightView.emotionTagPh')" />
      </n-form-item>
    </n-form>
    <!-- 笔记表单 -->
    <n-form v-if="type === 'note'" label-placement="left" label-width="70">
      <n-form-item :label="t('insightView.labelContent')">
        <n-input v-model:value="formModel.content" type="textarea" :placeholder="t('insightView.noteContentPh')" :rows="4" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelType')">
        <n-select v-model:value="formModel.kind" :options="noteKindOptions" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelTags')">
        <n-input v-model:value="formModel.tags" :placeholder="t('insightView.tagsPh')" />
      </n-form-item>
    </n-form>
    <!-- 学习记录表单 -->
    <n-form v-if="type === 'learning'" label-placement="left" label-width="70">
      <n-form-item :label="t('insightView.labelSummary')">
        <n-input v-model:value="formModel.summary" type="textarea" :placeholder="t('insightView.learningSummaryPh')" :rows="3" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelMode')">
        <n-input v-model:value="formModel.pattern" :placeholder="t('insightView.modePh')" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelPriority')">
        <n-select v-model:value="formModel.priority" :options="priorityOptions" />
      </n-form-item>
    </n-form>
    <!-- 本能表单 -->
    <n-form v-if="type === 'instinct'" label-placement="left" label-width="70">
      <n-form-item :label="t('insightView.labelContent')">
        <n-input v-model:value="formModel.content" type="textarea" :placeholder="t('insightView.instinctContentPh')" :rows="3" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelConfidence')">
        <n-slider v-model:value="formModel.confidence" :min="0" :max="1" :step="0.1" />
      </n-form-item>
    </n-form>
    <!-- 实体表单 -->
    <n-form v-if="type === 'entity'" label-placement="left" label-width="70">
      <n-form-item :label="t('insightView.labelName')">
        <n-input v-model:value="formModel.name" :placeholder="t('insightView.entityNamePh')" :disabled="editing" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelType')">
        <n-input v-model:value="formModel.kind" :placeholder="t('insightView.entityTypePh')" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelDesc')">
        <n-input v-model:value="formModel.observations" type="textarea" :placeholder="t('insightView.entityDescPh')" :rows="3" />
      </n-form-item>
    </n-form>
    <!-- 关系表单 -->
    <n-form v-if="type === 'relation'" label-placement="left" label-width="70">
      <n-form-item :label="t('insightView.labelStartEntity')">
        <n-input v-model:value="formModel.from" :placeholder="t('insightView.startEntityPh')" :disabled="editing" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelRelation')">
        <n-input v-model:value="formModel.relation" :placeholder="t('insightView.relationPh')" />
      </n-form-item>
      <n-form-item :label="t('insightView.labelEndEntity')">
        <n-input v-model:value="formModel.to" :placeholder="t('insightView.endEntityPh')" :disabled="editing" />
      </n-form-item>
    </n-form>
    <template #footer>
      <n-space justify="end">
        <n-button @click="emit('update:show', false)">{{ t('cancel') }}</n-button>
        <n-button type="primary" @click="onOk">{{ t('ok') }}</n-button>
      </n-space>
    </template>
  </n-modal>
</template>
