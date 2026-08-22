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

const WALLPAPER_CACHE_KEY = 'agents.mainWallpaper'

export const useAgentsStore = defineStore('agents', () => {
  const agents = ref<AgentInfo[]>([])
  const loading = ref(false)
  // 初始化优先读会话缓存：刷新时背景层立即可用，消除默认图/底色闪现窗口
  const mainWallpaper = ref(sessionStorage.getItem(WALLPAPER_CACHE_KEY) || '')

  function setMainWallpaper(url: string) {
    mainWallpaper.value = url
    try {
      if (url) sessionStorage.setItem(WALLPAPER_CACHE_KEY, url)
      else sessionStorage.removeItem(WALLPAPER_CACHE_KEY)
    } catch { /* 隐私模式等存储不可用则跳过缓存 */ }
  }

  async function load() {
    loading.value = true
    try {
      const data = await get<AgentInfo[]>('/agents')
      agents.value = data.map(a => ({
        ...a,
        display_name_en: translateToEn(a.display_name)
      }))
      const main = data.find(a => a.is_main)
      if (main?.wallpaper) setMainWallpaper(main.wallpaper)
    } finally {
      loading.value = false
    }
  }

  return { agents, loading, mainWallpaper, setMainWallpaper, load }
})