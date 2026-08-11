<script setup lang="ts">
import { NFormItem, NGrid, NGridItem, NInput } from 'naive-ui'
import type { ProviderDraft } from '../../api/providers'

const props = defineProps<{ modelValue: ProviderDraft }>()
const emit = defineEmits<{ 'update:modelValue': [value: ProviderDraft] }>()

function updateField(field: 'chat_path' | 'models_path', value: string) {
  emit('update:modelValue', { ...props.modelValue, [field]: value })
}

function updateMap(section: 'request' | 'response' | 'stream', field: string, value: string) {
  emit('update:modelValue', {
    ...props.modelValue,
    mapping: {
      ...props.modelValue.mapping,
      [section]: { ...props.modelValue.mapping[section], [field]: value },
    },
  })
}

function updateModels(value: string) {
  emit('update:modelValue', { ...props.modelValue, mapping: { ...props.modelValue.mapping, models: value } })
}

function updateHeaderName(name: string) {
  const value = Object.values(props.modelValue.headers)[0] || '{api_key}'
  emit('update:modelValue', { ...props.modelValue, headers: name ? { [name]: value } : {} })
}

function updateHeaderValue(value: string) {
  const name = Object.keys(props.modelValue.headers)[0] || 'Authorization'
  emit('update:modelValue', { ...props.modelValue, headers: { [name]: value } })
}
</script>

<template>
  <n-grid cols="1 m:2" responsive="screen" :x-gap="12">
    <n-grid-item><n-form-item label="聊天路径"><n-input :value="modelValue.chat_path" @update:value="updateField('chat_path', $event)" /></n-form-item></n-grid-item>
    <n-grid-item><n-form-item label="模型路径"><n-input :value="modelValue.models_path" @update:value="updateField('models_path', $event)" /></n-form-item></n-grid-item>
    <n-grid-item><n-form-item label="请求消息路径"><n-input :value="modelValue.mapping.request.messages" @update:value="updateMap('request', 'messages', $event)" /></n-form-item></n-grid-item>
    <n-grid-item><n-form-item label="请求模型路径"><n-input :value="modelValue.mapping.request.model" @update:value="updateMap('request', 'model', $event)" /></n-form-item></n-grid-item>
    <n-grid-item><n-form-item label="响应文本路径"><n-input :value="modelValue.mapping.response.text" @update:value="updateMap('response', 'text', $event)" /></n-form-item></n-grid-item>
    <n-grid-item><n-form-item label="流式文本路径"><n-input :value="modelValue.mapping.stream.text" @update:value="updateMap('stream', 'text', $event)" /></n-form-item></n-grid-item>
    <n-grid-item><n-form-item label="模型列表路径"><n-input :value="modelValue.mapping.models" @update:value="updateModels" /></n-form-item></n-grid-item>
    <n-grid-item><n-form-item label="认证 Header"><n-input :value="Object.keys(modelValue.headers)[0] || ''" @update:value="updateHeaderName" /></n-form-item></n-grid-item>
    <n-grid-item><n-form-item label="Header 模板"><n-input :value="Object.values(modelValue.headers)[0] || ''" @update:value="updateHeaderValue" /></n-form-item></n-grid-item>
  </n-grid>
</template>
