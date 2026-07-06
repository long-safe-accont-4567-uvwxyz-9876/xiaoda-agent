import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import { routes } from './routes'
import { useAuthStore } from './stores/auth'
import i18n from './i18n'
import { loadAgentNames } from './utils/agentNames'

const pinia = createPinia()
const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// 路由守卫：需要认证的路由必须已登录
router.beforeEach((to, _from, next) => {
  if (to.meta?.requiresAuth) {
    const auth = useAuthStore()
    if (!auth.isLoggedIn) {
      // 未登录时跳转登录页
      next({ name: 'login' })
      return
    }
  }
  next()
})

// 部署更新后旧页面引用的懒加载 chunk 会 404，导致导航静默失败——
// 检测到 chunk 加载错误时强制刷新一次拿新 index.html
router.onError((error, to) => {
  const msg = String(error?.message || error)
  if (/failed to fetch dynamically imported module|loading.*chunk|import/i.test(msg)) {
    const key = 'chunk-reload-ts'
    const last = Number(sessionStorage.getItem(key) || 0)
    if (Date.now() - last > 10_000) {
      sessionStorage.setItem(key, String(Date.now()))
      location.href = location.origin + location.pathname + '#' + (to?.fullPath || '/')
      location.reload()
    }
  } else {
    console.error('[router]', error)
  }
})

const app = createApp(App)
app.use(pinia)
app.use(router)
app.use(i18n)
app.mount('#app')

// 非阻塞加载 agent 名称映射表（全局替换用）
// 登录后再加载，避免未登录时 401 导致映射表永远为空
const auth = useAuthStore()
if (auth.isLoggedIn) {
  loadAgentNames()
} else {
  // 监听登录状态变化，登录成功后加载
  const unwatch = auth.$subscribe((_state) => {
    if (auth.isLoggedIn) {
      loadAgentNames()
      unwatch()
    }
  })
}
