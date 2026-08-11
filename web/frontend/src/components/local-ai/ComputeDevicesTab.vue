<script setup lang="ts">
import { NButton, NEmpty, NProgress, NTag, useMessage } from 'naive-ui'
import { useLocalAiStore } from '../../stores/localAi'

const store = useLocalAiStore()
const message = useMessage()
const percent = (available: number, total: number) => total > 0 ? Math.round((available / total) * 100) : 0

async function rescan() {
  try { await store.rescan(); message.success('算力设备已重新扫描') } catch (error) { message.error(error instanceof Error ? error.message : String(error)) }
}
</script>

<template>
  <div>
    <div class="device-toolbar"><span>仅展示后端实际探测到的设备与运行提供程序</span><n-button :loading="store.loading" @click="rescan">重新扫描</n-button></div>
    <div class="device-grid">
      <article v-for="device in store.devices" :key="device.id" class="glass-panel device-card">
        <div class="device-head"><div><strong>{{ device.name }}</strong><span>{{ device.kind }} · {{ device.architecture }}</span></div><n-tag :type="device.state === 'available' ? 'success' : device.state === 'degraded' ? 'warning' : 'error'">{{ device.state }}</n-tag></div>
        <n-progress type="line" :percentage="percent(device.memory_available, device.memory_total)" :show-indicator="false" />
        <div class="memory-line">可用 {{ device.memory_available.toLocaleString() }} / {{ device.memory_total.toLocaleString() }} bytes</div>
        <div class="backend-list"><n-tag v-for="backend in device.backends" :key="`${backend.runtime}:${backend.provider}`" size="small" :type="backend.healthy ? 'success' : 'warning'">{{ backend.runtime }} · {{ backend.provider }}</n-tag></div>
      </article>
      <n-empty v-if="!store.devices.length" description="尚未探测到算力设备" />
    </div>
  </div>
</template>

<style scoped>
.device-toolbar, .device-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.device-toolbar { margin-bottom: 16px; color: var(--moon-dim); font-size: 12px; }
.device-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; }
.device-card { padding: 16px; border-radius: 14px; }
.device-head { margin-bottom: 16px; }
.device-head strong, .device-head span { display: block; }
.device-head span, .memory-line { margin-top: 4px; color: var(--moon-dim); font-size: 12px; }
.backend-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 14px; }
</style>
