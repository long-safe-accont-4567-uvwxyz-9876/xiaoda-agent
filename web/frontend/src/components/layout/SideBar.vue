<script setup lang="ts">
import SumeruIcon from '../fx/SumeruIcon.vue'
import DendroEmblem from '../fx/DendroEmblem.vue'

defineProps<{ expanded: boolean }>()
const emit = defineEmits<{ 'update:expanded': [value: boolean] }>()

const navItems = [
  { icon: 'chat', label: '对话', route: '/' },
  { icon: 'agents', label: 'Agent 管理', route: '/settings/agents' },
  { icon: 'models', label: '模型与凭证', route: '/settings/models' },
  { icon: 'tools', label: 'Skills 工具', route: '/settings/tools' },
  { icon: 'mcp', label: 'MCP 服务', route: '/settings/mcp' },
  { icon: 'plugins', label: '插件管理', route: '/settings/plugins' },
  { icon: 'insight', label: '内在世界', route: '/insight' },
  { icon: 'schedule', label: '定时与问候', route: '/schedule' },
  { icon: 'media', label: '媒体工坊', route: '/media' },
  { icon: 'health', label: '测试中心', route: '/health' },
  { icon: 'dashboard', label: '仪表盘', route: '/dashboard' },
  { icon: 'settings', label: '系统设置', route: '/settings/system' },
]
</script>

<template>
  <nav class="sidebar" :class="{ expanded }"
       @mouseenter="emit('update:expanded', true)"
       @mouseleave="emit('update:expanded', false)">
    <div class="sidebar-inner">
      <div class="sidebar-logo">
        <DendroEmblem :size="30" spin />
        <span v-if="expanded" class="logo-text">Nahida Agent</span>
      </div>

      <div class="nav-items">
        <router-link
          v-for="item in navItems"
          :key="item.route"
          :to="item.route"
          class="nav-item"
          :title="item.label"
        >
          <span class="nav-icon"><SumeruIcon :name="item.icon" :size="20" /></span>
          <span v-if="expanded" class="nav-label">{{ item.label }}</span>
          <span class="nav-glow"></span>
        </router-link>
      </div>

      <div class="sidebar-foot" v-if="expanded">
        <span class="foot-text">「知识是智慧的种子」</span>
      </div>
    </div>
  </nav>
</template>

<style scoped>
.sidebar {
  width: var(--sidebar-width);
  height: 100vh;
  background: rgba(15, 31, 23, 0.7);
  backdrop-filter: blur(10px);
  border-right: 1px solid var(--glass-border);
  transition: width 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  overflow: hidden;
  flex-shrink: 0;
  z-index: 10;
}

.sidebar.expanded {
  width: var(--sidebar-expanded);
  animation: door-open 0.3s ease-out;
}

@keyframes door-open {
  from { transform: perspective(800px) rotateY(4deg); }
  to { transform: perspective(800px) rotateY(0); }
}

.sidebar-inner {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 12px 0;
}

.sidebar-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px 20px;
  border-bottom: 1px solid var(--glass-border);
  margin-bottom: 12px;
  min-height: 52px;
}

.logo-icon { font-size: 24px; flex-shrink: 0; }
.logo-text {
  color: var(--dendro);
  font-size: 18px;
  font-weight: 700;
  white-space: nowrap;
  font-family: 'Noto Serif SC', serif;
}

.nav-items {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0 8px;
  overflow-y: auto;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 12px;
  border-radius: 10px;
  color: var(--moon-dim);
  text-decoration: none;
  transition: background 0.2s, color 0.2s, transform 0.2s var(--ease-out);
  white-space: nowrap;
  position: relative;
}

.nav-item:hover {
  background: rgba(127, 214, 80, 0.1);
  color: var(--moon);
  transform: translateX(2px);
}

.nav-item.router-link-exact-active {
  background: linear-gradient(90deg, rgba(127, 214, 80, 0.22), rgba(127, 214, 80, 0.06));
  color: var(--dendro);
}

.nav-item.router-link-exact-active .nav-glow {
  position: absolute;
  left: 0; top: 20%; bottom: 20%;
  width: 3px;
  border-radius: 2px;
  background: var(--dendro);
  box-shadow: 0 0 8px var(--dendro);
}

.nav-icon {
  flex-shrink: 0;
  width: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.nav-label { font-size: 14px; }

.sidebar-foot {
  margin-top: auto;
  padding: 14px 16px;
  border-top: 1px solid var(--glass-border);
}
.foot-text {
  font-size: 11px;
  color: rgba(232, 213, 163, 0.55);
  font-family: 'Noto Serif SC', serif;
  white-space: normal;
  line-height: 1.6;
}

@media (max-width: 768px) {
  .sidebar { position: fixed; left: 0; top: 0; }
}
</style>
