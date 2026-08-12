import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import {
  fingerprintProviderDraft,
  normalizeProviderDraft,
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
  const testedCredentialSummary = ref('')
  const testReport = ref<CapabilityReport | null>(null)
  let testGeneration = 0

  const builtinProviders = computed(() => providers.value.filter(provider => provider.builtin))
  const customProviders = computed(() => providers.value.filter(provider => !provider.builtin))

  function invalidateTest() {
    testGeneration += 1
    testedFingerprint.value = ''
    testedCredentialSummary.value = ''
    testReport.value = null
  }

  function summarizeCredentials(credentials?: ProviderCredentials) {
    const value = credentials?.api_key ?? ''
    let first = 2166136261
    let second = 2246822507
    for (let index = 0; index < value.length; index += 1) {
      const code = value.charCodeAt(index)
      first = Math.imul(first ^ code, 16777619)
      second = Math.imul(second ^ code, 3266489909)
    }
    return `${value.length}:${(first >>> 0).toString(36)}:${(second >>> 0).toString(36)}`
  }

  function canSave(draft: ProviderDraft, credentials?: ProviderCredentials) {
    return testReport.value?.available === true
      && testedFingerprint.value === fingerprintProviderDraft(draft)
      && testedCredentialSummary.value === summarizeCredentials(credentials)
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

  async function loadCapabilities(providerId: string) {
    loading.value = true
    error.value = null
    try {
      return await providerApi.capabilities(providerId)
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    } finally {
      loading.value = false
    }
  }

  async function discoverModels(providerId: string) {
    loading.value = true
    error.value = null
    try {
      return await providerApi.models(providerId)
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : String(cause)
      throw cause
    } finally {
      loading.value = false
    }
  }

  async function testDraft(draft: ProviderDraft, credentials?: ProviderCredentials) {
    invalidateTest()
    const generation = testGeneration
    const snapshot = normalizeProviderDraft(draft)
    const fingerprint = fingerprintProviderDraft(snapshot)
    const credentialSnapshot = { api_key: credentials?.api_key ?? '' }
    const credentialSummary = summarizeCredentials(credentialSnapshot)
    const report = await providerApi.test(snapshot, credentialSnapshot)
    if (generation !== testGeneration) return report
    testReport.value = report
    if (report.available) {
      testedFingerprint.value = fingerprint
      testedCredentialSummary.value = credentialSummary
    }
    return report
  }

  async function createProvider(draft: ProviderDraft, credentials?: ProviderCredentials) {
    if (!canSave(draft, credentials)) throw new Error('请先测试当前配置')
    mutating.value = true
    try {
      const result = await providerApi.create(draft, credentials)
      await loadProviders()
      return result
    } finally {
      mutating.value = false
    }
  }

  async function updateProvider(id: string, draft: ProviderDraft, credentials?: ProviderCredentials) {
    if (!canSave(draft, credentials)) throw new Error('请先测试当前配置')
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
    testedFingerprint, testedCredentialSummary, testReport, invalidateTest, canSave, loadProviders, loadCapabilities, discoverModels,
    testDraft, createProvider, updateProvider, deleteProvider,
  }
})
