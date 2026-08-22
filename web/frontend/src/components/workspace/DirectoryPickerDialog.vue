<script setup lang="ts">
import { ref, watch } from 'vue'
import SumeruIcon from '../../components/fx/SumeruIcon.vue'
import { NModal, NButton, NInput, useMessage } from 'naive-ui'
import { useWorkspaceStore } from '../../stores/workspace'

/**
 * 目录浏览选择对话框
 *
 * 支持手动输入路径 + 后端 API 列目录浏览。
 * 选定路径后通过 select 事件回传。
 */
const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ select: [path: string]; cancel: [] }>()
const message = useMessage()
const ws = useWorkspaceStore()

const currentPath = ref('')
const parentPath = ref<string | null>(null)
const dirs = ref<string[]>([])
const manualInput = ref('')
const loading = ref(false)

async function browse(path: string) {
  loading.value = true
  try {
    const data = await ws.browse(path)
    currentPath.value = data.current
    parentPath.value = data.parent
    dirs.value = data.dirs || []
    manualInput.value = data.current
  } catch (e: any) {
    message.error(e.message || '浏览目录失败')
  } finally {
    loading.value = false
  }
}

watch(() => props.show, (v) => {
  if (v && !currentPath.value) browse('')
})

function selectDir() {
  const p = manualInput.value.trim() || currentPath.value
  if (!p) { message.warning('请输入或选择目录'); return }
  emit('select', p)
}

function enterDir(d: string) {
  const full = currentPath.value ? `${currentPath.value}/${d}` : d
  browse(full)
}
</script>

<template>
  <NModal :show="show" @update:show="(v: boolean) => !v && emit('cancel')" preset="card"
    title="选择工作目录" style="max-width: 560px">
    <div style="margin-bottom: 8px">
      <NInput v-model:value="manualInput" placeholder="手动输入绝对路径" :disabled="loading" />
    </div>
    <div v-if="currentPath" style="margin-bottom: 8px; color: #888; font-size: 13px">
      当前：{{ currentPath }}
    </div>
    <div style="max-height: 320px; overflow-y: auto; border: 1px solid #eee; border-radius: 4px">
      <div v-if="parentPath" class="dir-item" @click="browse(parentPath!)"><SumeruIcon name="folder" :size="14" variant="duo" tone="view" interactive /> ..（上级）</div>
      <div v-for="d in dirs" :key="d" class="dir-item" @click="enterDir(d)"><SumeruIcon name="folder" :size="14" variant="duo" tone="view" interactive /> {{ d }}</div>
      <div v-if="!dirs.length && currentPath && !loading" style="padding: 12px; color: #999">无子目录</div>
      <div v-if="loading" style="padding: 12px; color: #999">加载中...</div>
    </div>
    <template #footer>
      <NButton @click="emit('cancel')">取消</NButton>
      <NButton type="primary" @click="selectDir">选择此目录</NButton>
    </template>
  </NModal>
</template>

<style scoped>
.dir-item { padding: 6px 12px; cursor: pointer; }
.dir-item:hover { background: #f5f5f5; }
</style>
