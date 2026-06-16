import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import { routes } from './routes'

// 预加载 setup 和 login，确保 Vite 不会在 CI 构建时跳过这些 chunk
import('./views/SetupWizardView.vue')
import('./views/LoginView.vue')

const pinia = createPinia()
const router = createRouter({
  history: createWebHashHistory(),
  routes,
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
app.mount('#app')
