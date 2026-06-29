<script setup lang="ts">
import { computed, watch, ref } from 'vue'

interface Command { name: string; description: string; owner_only: boolean }

const props = defineProps<{ commands: Command[]; filter: string; visible: boolean }>()
const emit = defineEmits<{ select: [name: string] }>()

const activeIndex = ref(0)

const filtered = computed(() => {
  const q = props.filter.toLowerCase().replace(/^\//, '')
  return props.commands
    .filter(c => c.name.toLowerCase().includes(q) || c.description.toLowerCase().includes(q))
    .slice(0, 30)
})

watch(filtered, () => { activeIndex.value = 0 })

function move(delta: number) {
  if (!filtered.value.length) return
  activeIndex.value = (activeIndex.value + delta + filtered.value.length) % filtered.value.length
}

function confirm() {
  const cmd = filtered.value[activeIndex.value]
  if (cmd) emit('select', cmd.name)
}

defineExpose({ move, confirm, hasItems: () => filtered.value.length > 0 })
</script>

<template>
  <div v-if="visible && filtered.length" class="slash-palette">
    <div
      v-for="(cmd, i) in filtered"
      :key="cmd.name"
      class="cmd-item"
      :class="{ active: i === activeIndex }"
      @mousedown.prevent="emit('select', cmd.name)"
      @mouseenter="activeIndex = i"
    >
      <span class="cmd-name">/{{ cmd.name }}</span>
      <span class="cmd-desc">{{ cmd.description }}</span>
      <span v-if="cmd.owner_only" class="cmd-crown" title="仅主人可用">👑</span>
    </div>
  </div>
</template>

<style scoped>
.slash-palette {
  position: absolute;
  bottom: 100%;
  left: 0;
  right: 0;
  margin-bottom: 8px;
  background: rgba(15, 31, 23, 0.96);
  backdrop-filter: blur(14px);
  border: 1px solid var(--glass-border);
  border-radius: 12px;
  overflow-y: auto;
  max-height: 320px;
  box-shadow: var(--shadow-md);
  z-index: 30;
}

.cmd-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  cursor: pointer;
  font-size: 13px;
}

.cmd-item.active { background: rgba(127, 214, 80, 0.12); }

.cmd-name {
  font-family: 'JetBrains Mono', monospace;
  color: var(--dendro);
  flex-shrink: 0;
}

.cmd-desc {
  color: var(--moon-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}

.cmd-crown { flex-shrink: 0; font-size: 12px; }
</style>
