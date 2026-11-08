import { createApp } from 'vue'
import { loginLocationWithRedirect } from './utils/sessionRedirect'
import { ensureStorageSchema } from './utils/storageSchema'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import { routes } from './routes'
import { useAuthStore } from './stores/auth'
import i18n from './i18n'
import { loadAgentNames } from './utils/agentNames'
import { reportError } from './utils/errorLog'

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
      // 审查 A2：把访问目标带进登录页，登录后回到原页面而不是首页
      next(loginLocationWithRedirect(to.fullPath))
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

// 审查 D1：键值全是裸标量且没有版本号，改类型后老用户会读到旧值。启动时按
// STORAGE_SPEC 校验一遍，类型不符的直接丢弃（回到默认值）并写回新版本号。
ensureStorageSchema(localStorage)

const app = createApp(App)
// 全局错误边界（审计 P0-1）：组件渲染/生命周期里抛出的错误统一记录，
// 不再让一条脏消息把整页打白。错误详情只进 console 与环形缓冲，
// 界面上由 App.vue / ChatView 的 onErrorCaptured 给可读占位。
app.config.errorHandler = (err, instance, info) => {
  reportError(err, info, instance)
}
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
