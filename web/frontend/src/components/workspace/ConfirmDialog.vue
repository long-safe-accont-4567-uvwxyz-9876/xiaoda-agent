<script setup lang="ts">
import { NModal, NButton } from 'naive-ui'

/**
 * 切换工作目录确认弹窗
 *
 * 用户选择目录后弹出，说明切换后的作用范围，用户确认后切换 Agent 对话上下文到该目录。
 */
defineProps<{
  show: boolean
  path: string
}>()
const emit = defineEmits<{ confirm: []; cancel: [] }>()
</script>

<template>
  <NModal :show="show" @update:show="(v: boolean) => !v && emit('cancel')" preset="dialog"
    title="切换到该工作目录" :show-icon="false" style="max-width: 480px">
    <div style="padding: 8px 0">
      <p><b>路径：</b>{{ path }}</p>
      <p style="margin-top: 8px"><b>切换后 Agent 可在该目录：</b></p>
      <ul style="margin-left: 20px; line-height: 1.8">
        <li>✓ 读取目录内文件</li>
        <li>✓ 创建/修改/删除目录内文件</li>
        <li>✓ 执行受限命令（白名单内）</li>
        <li>✗ 不可访问目录外文件</li>
        <li>✗ 不可执行危险命令（黑名单始终拦截）</li>
      </ul>
      <p style="margin-top: 8px; color: var(--moon-dim); font-size: 13px">
        切换持久化：重启后自动恢复，可在设置页退出回到默认目录
      </p>
    </div>
    <template #action>
      <NButton @click="emit('cancel')">取消</NButton>
      <NButton type="primary" @click="emit('confirm')">切换到该目录</NButton>
    </template>
  </NModal>
</template>
