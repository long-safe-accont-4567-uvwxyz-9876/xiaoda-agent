import type { RouteRecordRaw } from 'vue-router'

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
    path: '/',
    component: () => import('./components/layout/AppLayout.vue'),
    children: [
      { path: '', name: 'chat', component: () => import('./views/ChatView.vue') },
      { path: 'insight', name: 'insight', component: () => import('./views/InsightView.vue') },
      { path: 'schedule', name: 'schedule', component: () => import('./views/ScheduleView.vue') },
      { path: 'media', name: 'media', component: () => import('./views/MediaView.vue') },
      { path: 'health', name: 'health', component: () => import('./views/HealthView.vue') },
      { path: 'dashboard', name: 'dashboard', component: () => import('./views/DashboardView.vue') },
      { path: 'settings/agents', name: 'agents', component: () => import('./views/AgentsView.vue') },
      { path: 'settings/models', name: 'models', component: () => import('./views/ModelsView.vue') },
      { path: 'settings/tools', name: 'tools', component: () => import('./views/ToolsView.vue') },
      { path: 'settings/mcp', name: 'mcp', component: () => import('./views/McpView.vue') },
      { path: 'settings/plugins', name: 'plugins', component: () => import('./views/PluginsView.vue') },
      { path: 'settings/system', name: 'settings', component: () => import('./views/SettingsView.vue') },
    ],
  },
]
