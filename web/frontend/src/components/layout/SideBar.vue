<script setup lang="ts">
import SumeruIcon from '../fx/SumeruIcon.vue'
import DendroEmblem from '../fx/DendroEmblem.vue'
import { t, state as i18nState } from '../../i18n'

defineProps<{ expanded: boolean }>()
const emit = defineEmits<{ 'update:expanded': [value: boolean] }>()

const navItems = [
  { icon: 'chat', labelKey: 'nav.chat', route: '/' },
  { icon: 'agents', labelKey: 'nav.agents', route: '/settings/agents' },
  { icon: 'models', labelKey: 'nav.models', route: '/settings/models' },
  { icon: 'tools', labelKey: 'nav.tools', route: '/settings/tools' },
  { icon: 'mcp', labelKey: 'nav.mcp', route: '/settings/mcp' },
  { icon: 'plugins', labelKey: 'nav.plugins', route: '/settings/plugins' },
  { icon: 'insight', labelKey: 'nav.insight', route: '/insight' },
  { icon: 'schedule', labelKey: 'nav.schedule', route: '/schedule' },
  { icon: 'media', labelKey: 'nav.media', route: '/media' },
  { icon: 'health', labelKey: 'nav.health', route: '/health' },
  { icon: 'dashboard', labelKey: 'nav.dashboard', route: '/dashboard' },
  { icon: 'settings', labelKey: 'nav.settings', route: '/settings/system' },
]
</script>

<template>
  <nav class="sidebar" :class="{ expanded }"
       @mouseenter="emit('update:expanded', true)"
       @mouseleave="emit('update:expanded', false)">
    <div class="sidebar-inner">
      <div class="sidebar-logo">
        <DendroEmblem :size="30" spin />
        <span v-if="expanded" class="logo-text">{{ t('brand') }}</span>
      </div>

      <div class="nav-items">
        <router-link
          v-for="item in navItems"
          :key="item.route"
          :to="item.route"
          class="nav-item"
          :title="t(item.labelKey)"
        >
          <span class="nav-icon"><SumeruIcon :name="item.icon" :size="20" /></span>
          <span v-if="expanded" class="nav-label">{{ t(item.labelKey) }}</span>
          <span class="nav-glow"></span>
        </router-link>
      </div>

      <div class="sidebar-foot" v-if="expanded">
        <span class="foot-text">{{ t('tagline') }}</span>
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
