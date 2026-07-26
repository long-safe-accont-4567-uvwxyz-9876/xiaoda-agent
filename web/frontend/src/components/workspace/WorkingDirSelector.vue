<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useMessage } from 'naive-ui'
import { useWorkspaceStore } from '../../stores/workspace'
import DirectoryPickerDialog from './DirectoryPickerDialog.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import { t } from '../../i18n'

/**
 * 工作目录选择器（PromptInput 工具栏按钮版）
 *
 * 作为聊天框工具栏的一个图标按钮，紧挨 📎 🌐 🧠 三个按钮。
 * - 默认目录：灰色 📁 图标，点击选择目录 → 确认切换
 * - 已切换目录：绿色高亮 📁 图标（右上角小绿点），点击切换到其他目录
 * - 退出目录：在设置页"工作目录"区块操作，回到默认目录对话
 */
const ws = useWorkspaceStore()
const message = useMessage()
const showPicker = ref(false)
const showConfirm = ref(false)
const pendingPath = ref('')

onMounted(() => {
  ws.loadStatus()
})

function onClick() {
  // 无论已授权还是未授权，点击都弹出目录选择器
  // 已授权时选择新目录 = 切换；未授权时选择目录 = 首次授权
  showPicker.value = true
}

function onPickerSelect(path: string) {
  pendingPath.value = path
  showPicker.value = false
  // 已授权过的目录（未在设置页撤销）直接切换，不弹授权确认
  if (ws.authorizedDirs.includes(path)) {
    onConfirmAuthorize()
    return
  }
  // 首次进入该目录：弹出确认对话框，说明权限范围
  showConfirm.value = true
}

async function onConfirmAuthorize() {
  try {
    await ws.confirm(pendingPath.value)
    message.success(t('settings.workspaceAuthorized'))
  } catch (e: any) {
    message.error(e.message || t('settings.workspaceAuthorizeFailed'))
  }
  showConfirm.value = false
}
</script>

<template>
  <span class="ws-tool-group">
    <!-- 分隔线 -->
    <span class="tool-divider"></span>
    <!-- 工作目录按钮：未授权灰色，已授权绿色高亮 + 右上角小绿点 -->
    <button
      class="tool-btn ws-btn"
      :class="{ 'ws-authorized': ws.authorized }"
      :title="ws.authorized
        ? `${t('settings.workspaceCurrent')}: ${ws.currentPath}`
        : t('settings.workspaceUnauthorized')"
      @click="onClick"
    >
      📁
    </button>

    <DirectoryPickerDialog :show="showPicker" @select="onPickerSelect" @cancel="showPicker = false" />
    <ConfirmDialog :show="showConfirm" :path="pendingPath"
      @confirm="onConfirmAuthorize" @cancel="showConfirm = false" />
  </span>
</template>

<style scoped>
.ws-tool-group {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

/* 按钮基础样式（与 PromptInput.tool-btn 保持一致风格） */
.ws-btn {
  background: none;
  border: none;
  cursor: pointer;
  color: var(--moon-dim);
  font-size: 16px;
  padding: 4px 6px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  transition: background 0.2s, color 0.2s, border-color 0.2s;
  line-height: 1;
  position: relative;
}

.ws-btn:hover {
  background: rgba(127, 214, 80, 0.08);
  color: var(--moon);
}

.ws-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* 已授权时按钮高亮（草元素绿） */
.ws-btn.ws-authorized {
  background: rgba(127, 214, 80, 0.18);
  color: var(--dendro);
  border: 1px solid rgba(127, 214, 80, 0.4);
}

.ws-btn.ws-authorized:hover {
  background: rgba(127, 214, 80, 0.25);
}

/* 已授权时右上角小绿点 */
.ws-btn.ws-authorized::after {
  content: '';
  position: absolute;
  top: 2px;
  right: 2px;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: #8fe560;
  box-shadow: 0 0 4px rgba(143, 229, 96, 0.9);
}

.tool-divider {
  width: 1px;
  height: 16px;
  background: linear-gradient(
    to bottom,
    transparent,
    rgba(127, 214, 80, 0.3),
    transparent
  );
  margin: 0 2px;
  flex-shrink: 0;
}
</style>
