<script setup lang="ts">
import { ref } from 'vue'
import { NButton, NEmpty, NPopconfirm, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'

const store = useLocalAiStore()
const message = useMessage()
const removing = ref('')

async function remove(id: string) {
  removing.value = id
  try {
    await store.remove(id)
    message.success('模型已移除')
  } catch (error) {
    message.error(error instanceof Error ? error.message : String(error))
  } finally {
    removing.value = ''
  }
}
</script>

<template>
  <div class="installed-list">
    <article v-for="model in store.models" :key="model.id" class="glass-panel installed-card">
      <div class="installed-main"><div><strong>{{ model.id }}</strong><span>{{ model.directory }}</span></div><n-tag :type="model.validation_state === 'valid' ? 'success' : 'warning'">{{ model.validation_state }}</n-tag></div>
      <div class="installed-meta"><span>{{ model.purpose }}</span><span>{{ model.revision }}</span><span>{{ model.ownership }}</span></div>
      <div class="installed-actions">
        <n-popconfirm v-if="model.removable" @positive-click="remove(model.id)"><template #trigger><n-button size="small" type="error" :loading="removing === model.id">移除</n-button></template>确认移除模型文件？</n-popconfirm>
        <n-tag v-else size="small">内置模型</n-tag>
      </div>
    </article>
    <n-empty v-if="!store.models.length" description="暂无已安装模型" />
  </div>
</template>

<style scoped>
.installed-list { display: grid; gap: 12px; }
.installed-card { padding: 16px; border-radius: 14px; }
.installed-main, .installed-meta, .installed-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.installed-main strong, .installed-main span { display: block; }
.installed-main span { max-width: 620px; margin-top: 4px; overflow: hidden; color: var(--moon-dim); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.installed-meta { justify-content: flex-start; margin-top: 12px; color: var(--moon-dim); font-size: 12px; }
.installed-actions { margin-top: 14px; justify-content: flex-end; }
</style>
