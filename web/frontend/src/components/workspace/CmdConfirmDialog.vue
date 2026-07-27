<script setup lang="ts">
import { NModal, NButton } from 'naive-ui'

/**
 * 命令动态确认弹窗
 *
 * Agent 调用非白名单命令时触发。
 * 用户可选：拒绝 / 允许本次 / 允许并加入白名单。
 */
defineProps<{
  show: boolean
  command: string
  requestId: string
}>()
const emit = defineEmits<{
  decision: [decision: 'deny' | 'allow_once' | 'allow', addToWhitelist: boolean]
}>()
</script>

<template>
  <NModal :show="show" preset="dialog" title="Agent 请求执行命令" :show-icon="false" :maskClosable="false">
    <div style="padding: 8px 0">
      <p><b>命令：</b><code>{{ command }}</code></p>
      <p style="color: #888; font-size: 13px; margin-top: 8px">
        此命令不在白名单中。允许本次仅执行这一次；加入白名单后此类命令将自动放行。
      </p>
    </div>
    <template #action>
      <NButton @click="emit('decision', 'deny', false)">拒绝</NButton>
      <NButton @click="emit('decision', 'allow_once', false)">允许本次</NButton>
      <NButton type="primary" @click="emit('decision', 'allow', true)">允许并加入白名单</NButton>
    </template>
  </NModal>
</template>
