import type { RouteRecordRaw } from 'vue-router'
import { useAuthStore } from './stores/auth'

export const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('./views/LoginView.vue'),
  },
  {
    path: '/setup',
    name: 'setup',
    component: () => import('./views/SetupWizardView.vue'),
  },
  {
    path: '/setup/profile',
    name: 'setup-profile',
    component: () => import('./views/UserProfileSetupView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/',
    component: () => import('./components/layout/AppLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      { path: '', name: 'chat', alias: 'chat', component: () => import('./views/ChatView.vue') },
      // /chat 是文档与用户习惯写法（CLAUDE.md 亦如此记载），重定向到根聊天页，
      // 避免静默白屏（2026-08-25 全路由扫屏发现）
      { path: 'chat', redirect: { name: 'chat' } },
      { path: 'insight', name: 'insight', component: () => import('./views/InsightView.vue') },
      { path: 'schedule', name: 'schedule', component: () => import('./views/ScheduleView.vue') },
      { path: 'media', name: 'media', component: () => import('./views/MediaView.vue') },
      { path: 'health', name: 'health', component: () => import('./views/HealthView.vue') },
      { path: 'local-deploy', name: 'local-deploy', component: () => import('./views/LocalDeployView.vue') },
      { path: 'dashboard', name: 'dashboard', component: () => import('./views/DashboardView.vue') },
      { path: 'settings/agents', name: 'agents', component: () => import('./views/AgentsView.vue') },
      { path: 'settings/models', name: 'models', component: () => import('./views/ModelsView.vue') },
      { path: 'settings/tools', name: 'tools', component: () => import('./views/ToolsView.vue') },
      { path: 'settings/search-engines', name: 'search-engines', component: () => import('./views/SearchEnginesView.vue') },
      { path: 'settings/mcp', name: 'mcp', component: () => import('./views/McpView.vue') },
      { path: 'settings/plugins', name: 'plugins', component: () => import('./views/PluginsView.vue') },
      { path: 'settings/mail', name: 'mail', component: () => import('./views/MailView.vue') },
      { path: 'settings/system', name: 'settings', component: () => import('./views/SettingsView.vue') },
      { path: 'retrieval', name: 'retrieval', component: () => import('./views/RetrievalView.vue') },
      { path: 'workflows', name: 'workflows', component: () => import('./views/WorkflowView.vue') },
      { path: 'disclaimer', name: 'disclaimer', component: () => import('./views/DisclaimerView.vue') },
      { path: 'sponsor', name: 'sponsor', component: () => import('./views/SponsorView.vue') },
    ],
  },
  {
    // 404 兜底：未知路径一律回聊天主页（此前未知 hash 直接白屏且零反馈）
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]