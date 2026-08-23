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
  position: relative;
  z-index: var(--z-sidebar);
  width: var(--sidebar-width);
  height: 100dvh;
  overflow: hidden;
  flex-shrink: 0;
  border-right: 1px solid var(--line);
  background: linear-gradient(180deg, rgba(9, 21, 15, 0.94), rgba(7, 17, 12, 0.88));
  box-shadow: 10px 0 34px rgba(0, 0, 0, 0.18);
  backdrop-filter: blur(18px) saturate(1.08);
  -webkit-backdrop-filter: blur(18px) saturate(1.08);
  transition: width var(--motion-normal) var(--ease-out), box-shadow var(--motion-normal);
}

.sidebar::after {
  content: '';
  position: absolute;
  inset: 0 0 0 auto;
  width: 1px;
  background: linear-gradient(180deg, transparent 4%, rgba(145, 232, 102, 0.24) 28%, rgba(85, 217, 178, 0.13) 70%, transparent 96%);
  pointer-events: none;
}

.sidebar.expanded {
  width: var(--sidebar-expanded);
  box-shadow: 18px 0 48px rgba(0, 0, 0, 0.26);
}

@media (hover: hover) and (pointer: fine) {
  .sidebar { transition: width var(--motion-normal) var(--ease-out), box-shadow var(--motion-normal); }
}

.mobile-close {
  position: absolute;
  top: 14px;
  right: 12px;
  display: grid;
  width: 32px;
  height: 32px;
  place-items: center;
  border: 1px solid var(--line);
  border-radius: var(--control-radius);
  background: rgba(16, 34, 25, 0.72);
  color: var(--moon-dim);
  cursor: pointer;
  font-size: 14px;
  transition: color var(--motion-fast), background-color var(--motion-fast), border-color var(--motion-fast);
}

.mobile-close:hover {
  border-color: var(--line-strong);
  background: rgba(28, 50, 38, 0.82);
  color: var(--moon);
}

.sidebar-inner {
  position: relative;
  display: flex;
  height: 100%;
  flex-direction: column;
  padding: 12px 0;
}

.sidebar-logo {
  display: flex;
  min-height: 54px;
  align-items: center;
  gap: 11px;
  margin: 0 9px 10px;
  padding: 7px 8px 14px;
  border-bottom: 1px solid var(--line-soft);
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
  min-height: 0;
  flex: 1;
  flex-direction: column;
  gap: 3px;
  padding: 2px 9px;
  overflow-y: auto;
  overscroll-behavior: contain;
  -webkit-overflow-scrolling: touch;
}

.nav-item {
  position: relative;
  display: flex;
  min-height: 40px;
  align-items: center;
  gap: 11px;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: var(--control-radius);
  color: var(--moon-dim);
  text-decoration: none;
  white-space: nowrap;
  transition: background-color var(--motion-fast), border-color var(--motion-fast), color var(--motion-fast), transform var(--motion-fast) var(--ease-out);
}

.nav-item:hover {
  border-color: var(--line-soft);
  background: rgba(145, 232, 102, 0.065);
  color: var(--moon);
  transform: translateX(2px);
}

.nav-item:focus-visible {
  outline: 2px solid var(--dendro-bright);
  outline-offset: -1px;
}

.nav-item:active {
  transform: translateX(1px) scale(0.985);
  transition-duration: 70ms;
}

.nav-item:hover .nav-icon {
  color: var(--dendro-bright);
}

.nav-icon {
  color: currentColor;
  transition: color var(--motion-fast);
}

.nav-item.router-link-exact-active {
  border-color: rgba(145, 232, 102, 0.18);
  background: linear-gradient(90deg, rgba(145, 232, 102, 0.14), rgba(85, 217, 178, 0.045));
  color: var(--dendro-bright);
}

.nav-item.router-link-exact-active .nav-glow {
  position: absolute;
  top: 8px;
  bottom: 8px;
  left: -1px;
  width: 2px;
  border-radius: 0 2px 2px 0;
  background: linear-gradient(180deg, var(--dendro-bright), var(--jade));
  box-shadow: 0 0 9px rgba(145, 232, 102, 0.52);
}

.nav-icon {
  display: flex;
  width: 28px;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
}

.nav-label {
  overflow: hidden;
  font-size: 13px;
  font-weight: 500;
  text-overflow: ellipsis;
}

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
    top: 0;
    left: 0;
    z-index: var(--z-sidebar);
    width: var(--sidebar-mobile-width);
    border-right-color: rgba(145, 232, 102, 0.22);
    transform: translateX(-105%);
    transition: transform var(--motion-normal) var(--ease-out);
  }

  .sidebar.mobile-open {
    width: var(--sidebar-mobile-width);
    transform: translateX(0);
  }

  .nav-item {
    min-height: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .sidebar { transition: none; }
  .sidebar.expanded { animation: none; }
}
</style>