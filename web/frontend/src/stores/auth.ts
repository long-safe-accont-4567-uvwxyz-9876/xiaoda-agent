import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api } from '../api'
import { getWsClient } from '../api/ws'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const expiresAt = ref(Number(localStorage.getItem('expires_at')) || 0)

  const isLoggedIn = computed(() => !!token.value && Date.now() / 1000 < expiresAt.value)

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
    getWsClient().disconnect()
  }

  return { token, expiresAt, isLoggedIn, login, logout }
})
