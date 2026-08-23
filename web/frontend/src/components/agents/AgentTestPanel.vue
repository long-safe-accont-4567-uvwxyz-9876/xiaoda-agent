<script setup lang="ts">
/**
 * 测试 Tab：向指定 agent 发送测试 prompt 并展示回包。
 */
import { ref } from 'vue'
import { NButton } from 'naive-ui'
import { post } from '../../api'
import { t } from '../../i18n'

const props = defineProps<{
  agentName: string
  displayName: string
}>()

const testing = ref(false)
const testResult = ref<any>(null)

async function runTest() {
  testing.value = true
  testResult.value = null
  try {
    testResult.value = await post(`/agents/${props.agentName}/test`)
  } catch (e: any) {
    testResult.value = { ok: false, error: e.message }
  } finally {
    testing.value = false
  }
}
</script>

<template>
  <div>
    <n-button :loading="testing" @click="runTest">{{ t('agentsView.testPrompt') }} {{ displayName }} {{ t('agentsView.sendTest') }}</n-button>
    <div v-if="testResult" class="test-result glass-panel"
         :class="{ failed: !testResult.ok }">
      <div>{{ testResult.ok ? t('agentsView.testPass') : t('agentsView.testFail') }} · {{ testResult.elapsed_ms }}ms</div>
      <div class="test-reply">{{ testResult.reply || testResult.error }}</div>
    </div>
  </div>
</template>

<style scoped>
.test-result {
  margin-top: 12px; padding: 12px 14px; font-size: 13px;
  border-color: rgba(127, 214, 80, 0.4);
}
.test-result.failed { border-color: var(--alert); }
.test-reply { margin-top: 6px; color: var(--moon-dim); white-space: pre-wrap; }
</style>
