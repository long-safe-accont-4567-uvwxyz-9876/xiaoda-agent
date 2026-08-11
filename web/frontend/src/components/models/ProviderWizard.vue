<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { NAlert, NButton, NForm, NFormItem, NInput, NModal, NRadioButton, NRadioGroup, NSpace, NSteps, NStep, NSwitch, useMessage } from 'naive-ui'
import type { ProviderDefinition, ProviderDraft, ProviderProtocolInput } from '../../api/providers'
import { useProvidersStore } from '../../stores/providers'
import CapabilityMatrix from './CapabilityMatrix.vue'
import CustomMappingEditor from './CustomMappingEditor.vue'

const props = defineProps<{ show: boolean; provider?: ProviderDefinition | null }>()
const emit = defineEmits<{ 'update:show': [value: boolean]; saved: [] }>()
const providers = useProvidersStore()
const message = useMessage()
const steps = ['protocol', 'connection', 'verification', 'review'] as const
const stepIndex = ref(1)
const step = computed(() => steps[stepIndex.value - 1])
const credentials = reactive({ api_key: '' })
const manualModel = ref('')
const draft = ref<ProviderDraft>(emptyDraft())

function emptyDraft(): ProviderDraft {
  return {
    id: '', label: '', protocol: 'openai', base_url: '', chat_path: '/chat/completions',
    models_path: '/models', default_model: '', enabled: true,
    auth: { required: true, header: 'Authorization', scheme: 'Bearer' },
    capabilities: { tools: true, vision: false, streaming: true, model_discovery: true, json_mode: false },
    headers: {},
    mapping: {
      request: { messages: 'input.messages', model: 'input.model' },
      response: { text: 'result.text' }, stream: { text: 'delta.text' }, models: 'data.*.id',
    },
  }
}

function protocolInput(protocol: ProviderDefinition['protocol']): ProviderProtocolInput {
  if (protocol === 'openai_compatible') return 'openai'
  if (protocol === 'custom_mapping') return 'custom-map'
  return protocol
}

watch(() => props.show, show => {
  if (!show) return
  stepIndex.value = 1
  manualModel.value = ''
  credentials.api_key = ''
  draft.value = props.provider
    ? { ...props.provider, protocol: protocolInput(props.provider.protocol), capabilities: { ...props.provider.capabilities }, auth: { ...props.provider.auth }, headers: { ...props.provider.headers }, mapping: { ...props.provider.mapping, request: { ...props.provider.mapping.request }, response: { ...props.provider.mapping.response }, stream: { ...props.provider.mapping.stream } } }
    : emptyDraft()
  providers.invalidateTest()
})

watch(draft, () => providers.invalidateTest(), { deep: true })

function closeWizard() {
  credentials.api_key = ''
  providers.invalidateTest()
  emit('update:show', false)
}

async function verify() {
  try {
    if (manualModel.value) draft.value.default_model = manualModel.value
    let report = await providers.testDraft(draft.value, credentials)
    if (!report.available) return
    if (!report.models.length && !draft.value.default_model && !manualModel.value) {
      message.warning('未发现模型，请手动填写模型 ID')
      return
    }
    if (!draft.value.default_model && report.models[0]) {
      draft.value.default_model = report.models[0]
      report = await providers.testDraft(draft.value, credentials)
      if (!report.available) return
    }
    stepIndex.value = 4
  } catch (cause) {
    message.error(cause instanceof Error ? cause.message : String(cause))
  }
}

async function save() {
  if (manualModel.value) draft.value.default_model = manualModel.value
  try {
    if (props.provider) await providers.updateProvider(props.provider.id, draft.value, credentials)
    else await providers.createProvider(draft.value, credentials)
    emit('saved')
    closeWizard()
  } catch (cause) {
    message.error(cause instanceof Error ? cause.message : String(cause))
  }
}
</script>

<template>
  <n-modal :show="show" preset="card" class="provider-wizard" title="配置模型 Provider" @close="closeWizard">
    <n-steps :current="stepIndex" size="small">
      <n-step title="协议" /><n-step title="连接" /><n-step title="验证" /><n-step title="确认" />
    </n-steps>
    <n-form label-placement="top" class="wizard-content">
      <template v-if="step === 'protocol'">
        <n-form-item label="协议">
          <n-radio-group v-model:value="draft.protocol">
            <n-radio-button value="openai">OpenAI 兼容</n-radio-button>
            <n-radio-button value="anthropic">Anthropic</n-radio-button>
            <n-radio-button value="ollama">Ollama</n-radio-button>
            <n-radio-button value="custom-map">自定义映射</n-radio-button>
          </n-radio-group>
        </n-form-item>
      </template>
      <template v-else-if="step === 'connection'">
        <n-form-item label="ID"><n-input v-model:value="draft.id" :disabled="Boolean(provider)" /></n-form-item>
        <n-form-item label="名称"><n-input v-model:value="draft.label" /></n-form-item>
        <n-form-item label="Base URL"><n-input v-model:value="draft.base_url" /></n-form-item>
        <n-form-item label="API Key"><n-input v-model:value="credentials.api_key" type="password" show-password-on="click" /></n-form-item>
        <n-form-item label="需要认证"><n-switch v-model:value="draft.auth.required" /></n-form-item>
        <n-form-item label="默认模型"><n-input v-model:value="draft.default_model" /></n-form-item>
        <custom-mapping-editor v-if="draft.protocol === 'custom-map'" v-model="draft" />
      </template>
      <template v-else-if="step === 'verification'">
        <n-alert type="info">后端将测试当前草稿；任何配置变更都会使测试失效。</n-alert>
        <n-form-item label="手动模型 ID"><n-input v-model:value="manualModel" /></n-form-item>
        <capability-matrix :report="providers.testReport" />
      </template>
      <template v-else>
        <capability-matrix :report="providers.testReport" />
        <n-alert v-if="!providers.canSave(draft)" type="warning">配置已变化，请重新测试。</n-alert>
      </template>
    </n-form>
    <template #footer>
      <n-space justify="end">
        <n-button @click="closeWizard">取消</n-button>
        <n-button v-if="stepIndex > 1" @click="stepIndex--">上一步</n-button>
        <n-button v-if="stepIndex < 3" type="primary" @click="stepIndex++">下一步</n-button>
        <n-button v-else-if="stepIndex === 3" type="primary" @click="verify">测试配置</n-button>
        <n-button v-else type="primary" :disabled="!providers.canSave(draft)" :loading="providers.mutating" @click="save">保存</n-button>
      </n-space>
    </template>
  </n-modal>
</template>

<style scoped>
.provider-wizard { width: min(760px, 92vw); }
.wizard-content { min-height: 300px; margin-top: 22px; }
</style>
