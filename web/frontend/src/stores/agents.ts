import { defineStore } from 'pinia'
import { ref } from 'vue'
import { get } from '../api'
import { pinyin } from 'pinyin-pro'

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

// 中文转拼音（IP 安全）
function translateToEn(zhName: string): string {
  if (!zhName) return ''
  const result = pinyin(zhName, { toneType: 'none', type: 'array' })
  const joined = result.join('')
  return joined.charAt(0).toUpperCase() + joined.slice(1).toLowerCase()
}

export const useAgentsStore = defineStore('agents', () => {
  const agents = ref<AgentInfo[]>([])
  const loading = ref(false)

  async function load() {
    loading.value = true
    try {
      const data = await get<AgentInfo[]>('/agents')
      // 自动翻译 display_name_en
      agents.value = data.map(a => ({
        ...a,
        display_name_en: translateToEn(a.display_name)
      }))
    } finally {
      loading.value = false
    }
  }

  return { agents, loading, load }
})
