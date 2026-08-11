import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  fingerprintProviderDraft,
  providerApi,
  type CapabilityReport,
  type ProviderCredentials,
  type ProviderDefinition,
  type ProviderDraft,
} from '../api/providers'

export const useProvidersStore = defineStore('providers', () => {
  const providers = ref<ProviderDefinition[]>([])
  const loading = ref(false)
  const mutating = ref(false)
  const error = ref<string | null>(null)
  const testedFingerprint = ref('')
  const testReport = ref<CapabilityReport | null>(null)

  const builtinProviders = computed(() => providers.value.filter(provider => provider.builtin))
  const customProviders = computed(() => providers.value.filter(provider => !provider.builtin))

  function invalidateTest() {
    testedFingerprint.value = ''
    testReport.value = null
  }

  function canSave(draft: ProviderDraft) {
    return testReport.value?.available === true && testedFingerprint.value === fingerprintProviderDraft(draft)
  }

  async function loadProviders() {
    loading.value = true
    error.value = null
    try {
      providers.value = await providerApi.list()
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    } finally {
      loading.value = false
    }
  }

  async function testDraft(draft: ProviderDraft, credentials: ProviderCredentials) {
    invalidateTest()
    const report = await providerApi.test(draft, credentials)
    testReport.value = report
    if (report.available) testedFingerprint.value = fingerprintProviderDraft(draft)
    return report
  }

  async function createProvider(draft: ProviderDraft, credentials: ProviderCredentials) {
    if (!canSave(draft)) throw new Error('请先测试当前配置')
    mutating.value = true
    try {
      const result = await providerApi.create(draft, credentials)
      await loadProviders()
      return result
    } finally {
      mutating.value = false
    }
  }

  async function updateProvider(id: string, draft: ProviderDraft, credentials: ProviderCredentials) {
    if (!canSave(draft)) throw new Error('请先测试当前配置')
    mutating.value = true
    try {
      const result = await providerApi.update(id, draft, credentials)
      await loadProviders()
      return result
    } finally {
      mutating.value = false
    }
  }

  async function deleteProvider(id: string) {
    mutating.value = true
    try {
      await providerApi.delete(id)
      await loadProviders()
    } finally {
      mutating.value = false
    }
  }

  return {
    providers, builtinProviders, customProviders, loading, mutating, error,
    testedFingerprint, testReport, invalidateTest, canSave, loadProviders,
    testDraft, createProvider, updateProvider, deleteProvider,
  }
})
