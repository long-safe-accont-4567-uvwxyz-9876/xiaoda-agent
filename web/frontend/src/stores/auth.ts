import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../api'
import { getWsClient } from '../api/ws'
import { WALLPAPER_CACHE_KEY } from './agents'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const expiresAt = ref(Number(localStorage.getItem('expires_at')) || 0)

  const isLoggedIn = computed(() => !!token.value && Date.now() / 1000 < expiresAt.value)

  function onAuthRenewed(event: Event) {
    const detail = (event as CustomEvent<{ token: string; expiresAt: number }>).detail
    token.value = detail.token
    if (detail.expiresAt) expiresAt.value = detail.expiresAt
    getWsClient().reconnect(detail.token)
  }

  window.addEventListener('xiaoda-auth-renewed', onAuthRenewed)

  async function login(password: string) {
    const data = await api.login(password)
    token.value = data.token
    expiresAt.value = data.expires_at
    localStorage.setItem('token', data.token)
    localStorage.setItem('expires_at', String(data.expires_at))
    // Connect WebSocket
    getWsClient().connect(data.token)
  }

  function logout() {
    token.value = ''
    expiresAt.value = 0
    localStorage.removeItem('token')
    localStorage.removeItem('expires_at')
    // 清理会话级壁纸缓存：避免登出后同标签页短暂残留上一会话的背景（review P2）
    try { sessionStorage.removeItem(WALLPAPER_CACHE_KEY) } catch { /* 存储不可用则跳过 */ }
    getWsClient().disconnect()
  }

  return { token, expiresAt, isLoggedIn, login, logout }
})
