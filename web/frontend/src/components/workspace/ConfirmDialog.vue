<script setup lang="ts">
import { NModal, NButton } from 'naive-ui'

/**
 * 授权确认弹窗
 *
 * 用户选择工作目录后弹出，说明权限范围，用户确认后授予 Agent Harness 权限。
 */
defineProps<{
  show: boolean
  path: string
}>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
</script>

<template>
  <NModal :show="show" @update:show="(v: boolean) => !v && emit('cancel')" preset="dialog"
    title="授权 Agent 访问工作目录" :show-icon="false" style="max-width: 480px">
    <div style="padding: 8px 0">
      <p><b>路径：</b>{{ path }}</p>
      <p style="margin-top: 8px"><b>权限范围：</b></p>
      <ul style="margin-left: 20px; line-height: 1.8">
        <li>✓ 读取目录内文件</li>
        <li>✓ 创建/修改/删除目录内文件</li>
        <li>✓ 在该目录下执行受限命令（白名单内）</li>
        <li>✗ 不可访问目录外文件</li>
        <li>✗ 不可执行危险命令（黑名单始终拦截）</li>
      </ul>
      <p style="margin-top: 8px; color: #888; font-size: 13px">
        授权持久化：重启后自动恢复，可在设置页撤销
      </p>
    </div>
    <template #action>
      <NButton @click="emit('cancel')">取消</NButton>
      <NButton type="primary" @click="emit('confirm')">我已了解，授权</NButton>
    </template>
  </NModal>
</template>
