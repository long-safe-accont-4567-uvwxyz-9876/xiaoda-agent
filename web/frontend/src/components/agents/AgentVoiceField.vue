<script setup lang="ts">
/**
 * 参考音频（voice_ref）字段：音色下拉 + 音频上传。
 * 打开编辑器时拉取一次音色分组；上传成功回写 v-model:voiceRef（与原视图行为一致）。
 */
import { ref, computed, watch } from 'vue'
import { NButton, NSelect, useMessage } from 'naive-ui'
import { get, api } from '../../api'
import { t } from '../../i18n'
import { replaceAgentNames } from '../../utils/agentNames'

const props = defineProps<{ agentName: string }>()

const voiceRef = defineModel<string | null>('voiceRef', { default: null })

const message = useMessage()
const voiceGroups = ref<Record<string, Array<{ name: string; voice_ref: string }>>>({})
const voiceUploading = ref(false)
const voiceFile = ref<File | null>(null)
const voiceInputEl = ref<HTMLInputElement | null>(null)

async function loadVoices() {
  try {
    const v = await get<{ groups: Record<string, Array<{ name: string; voice_ref: string }>> }>('/media/tts/voices')
    voiceGroups.value = v.groups || {}
  } catch { /* */ }
}

watch(() => props.agentName, () => loadVoices(), { immediate: true })

const voiceOptions = computed(() => {
  const opts: Array<{ label: string; value: string | null; type?: string }> = [{ label: t('agentsView.noVoice'), value: null, type: 'group' }]
  const agentName = props.agentName
  if (agentName && voiceGroups.value[agentName]) {
    voiceGroups.value[agentName].forEach(v => {
      opts.push({ label: replaceAgentNames(v.name), value: v.voice_ref })
    })
  }
  return opts as any
})

function onVoiceFilePick(e: Event) {
  const input = e.target as HTMLInputElement
  voiceFile.value = input.files?.[0] || null
}

async function uploadVoiceForAgent() {
  if (!voiceFile.value || !props.agentName) return
  const agentName = props.agentName
  const voiceName = `voice_${Date.now().toString(36)}`
  voiceUploading.value = true
  try {
    const formData = new FormData()
    formData.append('name', voiceName)
    formData.append('file', voiceFile.value)
    const result = await api.uploadVoiceRef(agentName, formData)
    message.success(t('agentsView.voiceUploaded'))
    voiceFile.value = null
    if (voiceInputEl.value) voiceInputEl.value.value = ''
    voiceRef.value = result.voice_ref
    await loadVoices()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    voiceUploading.value = false
  }
}
</script>

<template>
  <div class="voice-ref-field">
    <n-select v-model:value="voiceRef" :options="voiceOptions"
              :render-label="(opt: any) => replaceAgentNames(opt.label || opt.value || '')"
              :placeholder="t('agentsView.voiceRefPh')" style="flex: 1" />
    <input ref="voiceInputEl" type="file" accept="audio/mpeg,audio/wav"
           style="display: none" @change="onVoiceFilePick" />
    <n-button size="small" @click="voiceInputEl?.click()" :loading="voiceUploading">
      {{ voiceFile ? voiceFile.name : t('agentsView.uploadVoice') }}
    </n-button>
    <n-button size="small" type="primary" :disabled="!voiceFile" @click="uploadVoiceForAgent">
      {{ t('agentsView.upload') }}
    </n-button>
  </div>
</template>

<style scoped>
.voice-ref-field { display: flex; gap: 8px; align-items: center; width: 100%; flex-wrap: wrap; }
</style>
