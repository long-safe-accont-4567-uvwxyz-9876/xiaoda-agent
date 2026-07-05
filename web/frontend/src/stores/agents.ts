import { defineStore } from 'pinia'
import { ref } from 'vue'
import { get } from '../api'

export interface AgentInfo {
  name: string
  display_name: string
  display_name_en: string
  builtin: boolean
  is_main: boolean
  enabled: boolean
  provider: string
  model: string
  tool_count: number
  mcp_servers: string[]
  wallpaper?: string
  [key: string]: any
}

export const useAgentsStore = defineStore('agents', () => {
  const agents = ref<AgentInfo[]>([])
  const loading = ref(false)

  async function load() {
    loading.value = true
    try {
      agents.value = await get<AgentInfo[]>('/agents')
    } finally {
      loading.value = false
    }
  }

  return { agents, loading, load }
})
