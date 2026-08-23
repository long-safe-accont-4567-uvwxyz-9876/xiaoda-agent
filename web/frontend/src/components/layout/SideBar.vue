<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import SumeruIcon from '../fx/SumeruIcon.vue'
import DendroEmblem from '../fx/DendroEmblem.vue'
import { t } from '../../i18n'

const props = defineProps<{ expanded: boolean; mobileOpen?: boolean }>()
const emit = defineEmits<{
  'update:expanded': [value: boolean]
  close: []
}>()

const navItems = [
  { icon: 'chat', labelKey: 'nav.chat', route: '/' },
  { icon: 'agents', labelKey: 'nav.agents', route: '/settings/agents' },
  { icon: 'models', labelKey: 'nav.models', route: '/settings/models' },
  { icon: 'tools', labelKey: 'nav.tools', route: '/settings/tools' },
  { icon: 'search', labelKey: 'nav.searchEngines', route: '/settings/search-engines' },
  { icon: 'mcp', labelKey: 'nav.mcp', route: '/settings/mcp' },
  { icon: 'flow', labelKey: 'nav.workflows', route: '/workflows' },
  { icon: 'plugins', labelKey: 'nav.plugins', route: '/settings/plugins' },
  { icon: 'insight', labelKey: 'nav.insight', route: '/insight' },
  { icon: 'search', labelKey: 'nav.retrieval', route: '/retrieval' },
  { icon: 'schedule', labelKey: 'nav.schedule', route: '/schedule' },
  { icon: 'mail', labelKey: 'nav.mail', route: '/settings/mail' },
  { icon: 'media', labelKey: 'nav.media', route: '/media' },
  { icon: 'health', labelKey: 'nav.health', route: '/health' },
  { icon: 'dashboard', labelKey: 'nav.dashboard', route: '/dashboard' },
  { icon: 'chip', labelKey: 'nav.localDeploy', route: '/local-deploy' },
  { icon: 'settings', labelKey: 'nav.settings', route: '/settings/system' },
  { icon: 'alert', labelKey: 'nav.disclaimer', route: '/disclaimer' },
]

const isMobileViewport = ref(false)
const hoverCapable = ref(true)

function updateViewport() {
  isMobileViewport.value = window.innerWidth <= 768
  hoverCapable.value = window.matchMedia('(hover: hover) and (pointer: fine)').matches
}

onMounted(() => {
  updateViewport()
  window.addEventListener('resize', updateViewport)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', updateViewport)
})

const showLabels = computed(() => props.expanded || !!props.mobileOpen)
// 移动端浮层关闭时，从 Tab 序与辅助技术中移除侧栏，仅靠 transform 位移不够
const focusIsolated = computed(() => isMobileViewport.value && props.mobileOpen === false)

function onEnter() {
  if (!isMobileViewport.value && hoverCapable.value) {
    emit('update:expanded', true)
  }
}

function onLeave() {
  if (!isMobileViewport.value) {
    emit('update:expanded', false)
  }
}
</script>

<template>
  <nav
    id="app-sidebar"
    class="sidebar"
    :class="{ expanded, 'mobile-open': mobileOpen }"
    :aria-label="t('nav.mainNavigation')"
    :inert="focusIsolated"
    :aria-hidden="focusIsolated || undefined"
    @mouseenter="onEnter"
    @mouseleave="onLeave"
  >
    <div class="sidebar-inner">
      <div class="sidebar-logo">
        <DendroEmblem :size="30" spin />
        <span v-if="showLabels" class="logo-text">{{ t('brand') }}</span>
      </div>

      <button
        v-if="mobileOpen"
        type="button"
        class="mobile-close"
        :aria-label="t('nav.closeNavigation')"
        @click="emit('close')"
      >✕</button>

      <div class="nav-items">
        <router-link
          v-for="item in navItems"
          :key="item.route"
          :to="item.route"
          class="nav-item"
          :title="t(item.labelKey)"
        >
          <span class="nav-icon"><SumeruIcon :name="item.icon" :size="20" /></span>
          <span v-if="showLabels" class="nav-label">{{ t(item.labelKey) }}</span>
          <span class="nav-glow"></span>
        </router-link>
      </div>

      <div class="sidebar-foot" v-if="showLabels">
        <router-link to="/sponsor" class="sponsor-entry" :title="t('sponsor.navTitle')">
          <span class="sponsor-icon"><SumeruIcon name="tea" :size="14" variant="duo" interactive /></span>
          <span class="sponsor-label">{{ t('sponsor.navTitle') }}</span>
        </router-link>
        <span class="foot-text">{{ t('tagline') }}</span>
        <span class="foot-signature">{{ t('brand_signature.full') }}</span>
      </div>
      <router-link v-else to="/sponsor" class="sponsor-entry-collapsed" :title="t('sponsor.navTitle')">
        <span class="sponsor-icon"><SumeruIcon name="tea" :size="18" variant="duo" interactive /></span>
      </router-link>
    </div>
  </nav>
</template>

<style scoped>
.sidebar {
  width: var(--sidebar-width);
  height: 100dvh;
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

/* 触屏（粗指针）不应触发 hover 展开；仅精细指针设备允许 */
@media (hover: hover) and (pointer: fine) {
  .sidebar { transition: width 0.3s cubic-bezier(0.4, 0, 0.2, 1); }
}

.mobile-close {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 32px;
  height: 32px;
  border: 1px solid var(--glass-border);
  border-radius: 8px;
  background: rgba(15, 31, 23, 0.6);
  color: var(--moon);
  cursor: pointer;
  font-size: 15px;
}

.sidebar-inner {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 12px 0;
  position: relative;
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
  font-size: 18px;
  font-weight: 700;
  white-space: nowrap;
  font-family: 'Noto Serif SC', serif;
  background: var(--gradient-dendro, linear-gradient(135deg, #b8ff85, #8fe560 45%, #4fd6a5));
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  color: transparent;
}

.nav-items {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 0 8px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 9px 12px;
  border-radius: 10px;
  color: var(--moon-dim);
  text-decoration: none;
  transition: background 0.25s, color 0.25s, transform 0.25s var(--ease-spring, var(--ease-out)), box-shadow 0.25s;
  white-space: nowrap;
  position: relative;
}

.nav-item:hover {
  background: rgba(127, 214, 80, 0.1);
  color: var(--moon);
  transform: translateX(3px);
}

.nav-item:active {
  transform: translateX(3px) scale(0.97);
  transition-duration: 0.08s;
}

.nav-item:hover .nav-icon {
  transform: rotate(-8deg) scale(1.12);
}

.nav-icon {
  transition: transform 0.25s var(--ease-spring, var(--ease-out));
}

.nav-item.router-link-exact-active {
  background: linear-gradient(90deg, rgba(127, 214, 80, 0.22), rgba(127, 214, 80, 0.06));
  color: var(--dendro);
  box-shadow: inset 0 0 16px rgba(127, 214, 80, 0.06), 0 0 12px rgba(127, 214, 80, 0.08);
}

.nav-item.router-link-exact-active .nav-glow {
  position: absolute;
  left: 0; top: 20%; bottom: 20%;
  width: 3px;
  border-radius: 2px;
  background: linear-gradient(180deg, var(--dendro-bright, #b8ff85), var(--jade, #4fd6a5));
  box-shadow: 0 0 10px var(--dendro);
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
  padding: 12px 16px 14px;
  border-top: 1px solid var(--glass-border);
}
.sponsor-entry {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  margin-bottom: 10px;
  border-radius: 8px;
  color: rgba(232, 213, 163, 0.55);
  text-decoration: none;
  font-size: 11px;
  font-family: 'Noto Serif SC', serif;
  letter-spacing: 0.5px;
  transition: background 0.2s, color 0.2s;
}
.sponsor-entry:hover {
  background: rgba(127, 214, 80, 0.08);
  color: rgba(232, 213, 163, 0.75);
}
.sponsor-entry.router-link-active {
  background: rgba(127, 214, 80, 0.12);
  color: var(--dendro);
}
.sponsor-entry-collapsed {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 8px;
  margin: 8px auto 0;
  border-radius: 8px;
  color: rgba(232, 213, 163, 0.55);
  text-decoration: none;
  transition: background 0.2s, color 0.2s;
}
.sponsor-entry-collapsed:hover {
  background: rgba(127, 214, 80, 0.08);
  color: rgba(232, 213, 163, 0.75);
}
.sponsor-icon {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}
.sponsor-label {
  white-space: nowrap;
}
.foot-text {
  font-size: 11px;
  color: rgba(232, 213, 163, 0.55);
  font-family: 'Noto Serif SC', serif;
  white-space: normal;
  line-height: 1.6;
}
.foot-signature {
  display: block;
  margin-top: 6px;
  font-size: 10px;
  color: rgba(232, 213, 163, 0.4);
  font-family: 'Noto Serif SC', serif;
  letter-spacing: 1px;
}

@media (max-width: 768px) {
  .sidebar {
    position: fixed;
    left: 0;
    top: 0;
    width: var(--sidebar-mobile-width, min(82vw, 320px));
    transform: translateX(-105%);
    transition: transform 0.25s var(--ease-out);
    /* 移动端侧栏在 overlay 之上：overlay z-index=70 遮住侧栏会导致点击导航项时
       事件打到 overlay 而非 router-link，表现为"能点但不导航"。 */
    z-index: 80;
  }
  .sidebar.mobile-open {
    transform: translateX(0);
    width: var(--sidebar-mobile-width, min(82vw, 320px));
  }
}

@media (prefers-reduced-motion: reduce) {
  .sidebar { transition: none; }
  .sidebar.expanded { animation: none; }
}
</style>