<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NButton, NPopconfirm, useMessage } from 'naive-ui'
import { useWorkspaceStore } from '../../stores/workspace'
import DirectoryPickerDialog from './DirectoryPickerDialog.vue'
import ConfirmDialog from './ConfirmDialog.vue'

/**
 * 工作目录选择器
 *
 * 放置在 ChatTerminal 上方，显示当前授权状态。
 * 未授权时提示选择目录；已授权时显示路径 + 切换/撤销按钮。
 */
const ws = useWorkspaceStore()
const message = useMessage()
const showPicker = ref(false)
const showConfirm = ref(false)
const pendingPath = ref('')

onMounted(() => {
  ws.loadStatus()
})

function onPickerSelect(path: string) {
  pendingPath.value = path
  showPicker.value = false
  showConfirm.value = true
}

async function onConfirmAuthorize() {
  try {
    await ws.confirm(pendingPath.value)
    message.success('已授权工作目录')
  } catch (e: any) {
    message.error(e.message || '授权失败')
  }
  showConfirm.value = false
}

async function onRevoke() {
  try {
    await ws.revoke()
    message.success('已撤销授权')
  } catch (e: any) {
    message.error(e.message || '撤销失败')
  }
}
</script>

<template>
  <div class="ws-selector">
    <template v-if="ws.authorized">
      <span class="ws-path" :title="ws.currentPath">📁 {{ ws.currentPath }}</span>
      <NButton size="small" @click="showPicker = true">切换</NButton>
      <NPopconfirm @positive-click="onRevoke">
        <template #trigger>
          <NButton size="small" type="warning" ghost>撤销授权</NButton>
        </template>
        确定撤销当前工作目录授权吗？Agent 将无法再访问该目录。
      </NPopconfirm>
    </template>
    <template v-else>
      <span class="ws-hint">未授权 — 点击选择目录以授予 Agent 工作权限</span>
      <NButton size="small" type="primary" @click="showPicker = true">选择工作目录</NButton>
    </template>

    <DirectoryPickerDialog :show="showPicker" @select="onPickerSelect" @cancel="showPicker = false" />
    <ConfirmDialog :show="showConfirm" :path="pendingPath"
      @confirm="onConfirmAuthorize" @cancel="showConfirm = false" />
  </div>
</template>

<style scoped>
.ws-selector {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--n-border-color, #eee);
  font-size: 13px;
}
.ws-path {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ws-hint { flex: 1; color: #888; }
</style>
