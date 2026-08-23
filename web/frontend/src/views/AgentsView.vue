<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NButton, useMessage } from 'naive-ui'
import { post, del } from '../api'
import { useAgentsStore } from '../stores/agents'
import { t } from '../i18n'
import SumeruIcon from '../components/fx/SumeruIcon.vue'
import Tilt3D from '../components/fx/Tilt3D.vue'
import ViewTitleIcon from '../components/fx/ViewTitleIcon.vue'
import AgentCard from '../components/agents/AgentCard.vue'
import AgentEditorModal from '../components/agents/AgentEditorModal.vue'
import type { AgentInfo } from '../api/types'

const message = useMessage()
const agentsStore = useAgentsStore()
const editorRef = ref<InstanceType<typeof AgentEditorModal> | null>(null)

onMounted(() => {
  agentsStore.load().catch((e) => message.error(e.message))
})

function openEditor(agent: AgentInfo | null) {
  editorRef.value?.open(agent)
}

async function toggleEnabled(agent: AgentInfo, value: boolean) {
  try {
    await post(`/agents/${agent.name}/${value ? 'enable' : 'disable'}`)
    agent.enabled = value
    message.success(`${agent.display_name} ` + t(value ? 'agentsView.enabled' : 'agentsView.disabled'))
  } catch (e: any) {
    message.error(e.message)
  }
}

async function removeAgent(agent: AgentInfo) {
  try {
    await del(`/agents/${agent.name}`, true)
    message.success(`${agent.display_name} ` + t('agentsView.agentDeleted'))
    await agentsStore.load()
  } catch (e: any) {
    message.error(e.message)
  }
}
</script>

<template>
  <div class="agents-view">
    <div class="view-header">
      <h2 class="view-title view-title-icon"><ViewTitleIcon name="agents" /> {{ t('agentsView.title') }}</h2>
      <n-button type="primary" @click="openEditor(null)"><SumeruIcon name="plus" :size="14" variant="duo" tone="add" interactive /> {{ t('agentsView.createSub') }}</n-button>
    </div>

    <div class="agent-grid">
      <Tilt3D v-for="a in agentsStore.agents" :key="a.name">
        <AgentCard :agent="a"
                   @edit="openEditor" @toggle="toggleEnabled" @remove="removeAgent" />
      </Tilt3D>
    </div>

    <AgentEditorModal ref="editorRef" />
  </div>
</template>

<style scoped>
.view-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}
.view-header h2 { color: var(--moon); font-family: 'Noto Serif SC', serif; }

.agent-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
}
</style>
