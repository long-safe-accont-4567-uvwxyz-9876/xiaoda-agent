<script setup lang="ts">
import { ref, watch } from 'vue'
import SumeruIcon from '../fx/SumeruIcon.vue'
import { NButton, NCheckbox, NInput, NModal, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'

const props = defineProps<{ show: boolean; initialPath?: string; requiredBytes?: number }>()
const emit = defineEmits<{ select: [path: string]; cancel: [] }>()
const store = useLocalAiStore()
const message = useMessage()
const path = ref('')
const entries = ref<string[]>([])
const loading = ref(false)
const saveAsDefault = ref(false)
let browseGeneration = 0

function resolveEntryPath(entry: string) {
  if (!path.value) {
    if (/^[A-Za-z]:$/.test(entry)) return `${entry}\\`
    return `/${entry}`
  }
  const separator = path.value.includes('\\') ? '\\' : '/'
  return path.value.endsWith('\\') || path.value.endsWith('/')
    ? `${path.value}${entry}`
    : `${path.value}${separator}${entry}`
}

async function browse(target = path.value) {
  const generation = ++browseGeneration
  loading.value = true
  try {
    const listing = await store.browseStorage(target)
    if (generation !== browseGeneration) return
    path.value = listing.path
    entries.value = listing.entries
    if (listing.error) message.warning(listing.error)
  } catch (error) {
    if (generation !== browseGeneration) return
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    if (generation === browseGeneration) loading.value = false
  }
}

async function selectPath() {
  const selected = path.value.trim()
  if (!selected) return message.warning('请手动输入或浏览选择目录')
  loading.value = true
  try {
    const validation = await store.validateStorage(selected, props.requiredBytes ?? 0)
    if (!validation.writable || validation.error) {
      return message.error(validation.error || validation.reason || '目录不可写')
    }
    if (saveAsDefault.value) await store.saveDefaultStorage(selected)
    emit('select', selected)
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    loading.value = false
  }
}

watch(() => props.show, show => {
  if (!show) return
  path.value = props.initialPath || store.defaultStorage
  saveAsDefault.value = false
  browse(path.value)
})
</script>

<template>
  <n-modal :show="show" preset="card" title="选择模型存储目录" class="local-ai-dialog" @update:show="value => !value && emit('cancel')">
    <div class="storage-picker">
      <n-input v-model:value="path" placeholder="手动输入服务器绝对路径" :disabled="loading" />
      <div class="storage-actions">
        <n-button :loading="loading" @click="browse()">浏览</n-button>
        <n-checkbox v-model:checked="saveAsDefault">保存为默认目录</n-checkbox>
      </div>
      <div class="directory-list">
        <button v-for="entry in entries" :key="entry" type="button" @click="browse(resolveEntryPath(entry))"><SumeruIcon name="folder" :size="14" /> {{ entry }}</button>
        <span v-if="!entries.length && !loading">当前目录没有可浏览的子目录</span>
      </div>
    </div>
    <template #footer>
      <div class="dialog-footer">
        <n-button @click="emit('cancel')">取消</n-button>
        <n-button type="primary" :loading="loading" @click="selectPath">使用此目录</n-button>
      </div>
    </template>
  </n-modal>
</template>

<style scoped>
.storage-picker { display: grid; gap: 12px; }
.storage-actions, .dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.directory-list { min-height: 120px; max-height: 280px; overflow: auto; padding: 8px; border: 1px solid var(--glass-border); border-radius: 10px; }
.directory-list button { display: block; width: 100%; padding: 9px 10px; color: inherit; text-align: left; background: transparent; border: 0; border-radius: 8px; cursor: pointer; }
.directory-list button:hover { background: rgba(127, 214, 80, 0.1); }
.directory-list span { display: block; padding: 28px 12px; color: var(--moon-dim); text-align: center; }
</style>
