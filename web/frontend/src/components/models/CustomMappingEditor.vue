<script setup lang="ts">
import { NButton, NFormItem, NGrid, NGridItem, NInput, NSpace } from 'naive-ui'
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

function updateHeader(index: number, name: string, value: string) {
  const entries = Object.entries(props.modelValue.headers)
  entries[index] = [name, value]
  emit('update:modelValue', { ...props.modelValue, headers: Object.fromEntries(entries.filter(([key]) => key)) })
}

function addHeader() {
  const entries = Object.entries(props.modelValue.headers)
  let name = 'X-Custom-Header'
  let suffix = 2
  while (name in props.modelValue.headers) name = `X-Custom-Header-${suffix++}`
  emit('update:modelValue', { ...props.modelValue, headers: Object.fromEntries([...entries, [name, '']]) })
}

function removeHeader(name: string) {
  emit('update:modelValue', {
    ...props.modelValue,
    headers: Object.fromEntries(Object.entries(props.modelValue.headers).filter(([key]) => key !== name)),
  })
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
    <n-grid-item span="1 m:2">
      <n-form-item label="请求 Headers">
        <n-space vertical style="width: 100%">
          <n-space v-for="([name, value], index) in Object.entries(modelValue.headers)" :key="`${index}-${name}`" :wrap="false">
            <n-input :value="name" placeholder="Header 名称" @update:value="updateHeader(index, $event, value)" />
            <n-input :value="value" placeholder="Header 模板" @update:value="updateHeader(index, name, $event)" />
            <n-button type="error" quaternary @click="removeHeader(name)">删除</n-button>
          </n-space>
          <n-button dashed @click="addHeader">添加 Header</n-button>
        </n-space>
      </n-form-item>
    </n-grid-item>
  </n-grid>
</template>
