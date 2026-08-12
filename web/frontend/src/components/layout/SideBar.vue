<script setup lang="ts">
import { computed, ref, onMounted, onBeforeUnmount } from 'vue'
import SumeruIcon from '../fx/SumeruIcon.vue'
import DendroEmblem from '../fx/DendroEmblem.vue'
import { t, tf, state as i18nState } from '../../i18n'

const props = defineProps<{ expanded: boolean; mobileOpen: boolean }>()
const emit = defineEmits<{
  'update:expanded': [value: boolean]
  'close': []
}>()

// 移动视口下侧栏以浮层形式呈现；关闭时须从 Tab 序与辅助技术中移除。
const isMobileViewport = ref(false)
// 粗指针（触屏）设备不应触发 hover 展开，避免误展开与抖动。
const isFinePointer = ref(true)
let mqViewport: MediaQueryList | null = null
let mqPointer: MediaQueryList | null = null
const syncViewport = () => { isMobileViewport.value = !!mqViewport?.matches }
const syncPointer = () => { isFinePointer.value = !!mqPointer?.matches }

onMounted(() => {
  if (typeof window === 'undefined' || !window.matchMedia) return
  mqViewport = window.matchMedia('(max-width: 768px)')
  mqPointer = window.matchMedia('(pointer: fine)')
  syncViewport(); syncPointer()
  mqViewport.addEventListener('change', syncViewport)
  mqPointer.addEventListener('change', syncPointer)
})
onBeforeUnmount(() => {
  mqViewport?.removeEventListener('change', syncViewport)
  mqPointer?.removeEventListener('change', syncPointer)
})

// 仅在移动浮层关闭时隔离焦点；桌面常驻侧栏始终可交互。
const focusIsolated = computed(() => isMobileViewport.value && !props.mobileOpen)
// 仅在细指针（鼠标/触控板）且非移动视口时启用 hover 展开。
const hoverCapable = computed(() => isFinePointer.value && !isMobileViewport.value)
function onEnter() { if (hoverCapable.value) emit('update:expanded', true) }
function onLeave() { if (hoverCapable.value) emit('update:expanded', false) }

const navItems = [
  { icon: 'chat', labelKey: 'nav.chat', route: '/' },
  { icon: 'agents', labelKey: 'nav.agents', route: '/settings/agents' },
  { icon: 'models', labelKey: 'nav.models', route: '/settings/models' },
  { icon: 'tools', labelKey: 'nav.tools', route: '/settings/tools' },
  { icon: 'mcp', labelKey: 'nav.mcp', route: '/settings/mcp' },
  { icon: 'flow', labelKey: 'nav.workflows', route: '/workflows' },
  { icon: 'plugins', labelKey: 'nav.plugins', route: '/settings/plugins' },
  { icon: 'insight', labelKey: 'nav.insight', route: '/insight' },
  { icon: 'schedule', labelKey: 'nav.schedule', route: '/schedule' },
  { icon: 'mail', labelKey: 'nav.mail', route: '/settings/mail' },
  { icon: 'media', labelKey: 'nav.media', route: '/media' },
  { icon: 'health', labelKey: 'nav.health', route: '/health' },
  { icon: 'dashboard', labelKey: 'nav.dashboard', route: '/dashboard' },
  { icon: 'chip', labelKey: 'nav.localDeploy', route: '/local-deploy' },
  { icon: 'settings', labelKey: 'nav.settings', route: '/settings/system' },
  { icon: 'alert', labelKey: 'nav.disclaimer', route: '/disclaimer' },
]
</script>

<template>
  <nav id="app-sidebar" class="sidebar" :class="{ expanded, 'mobile-open': mobileOpen }"
       :aria-label="t('nav.mainNavigation')"
       :inert="focusIsolated"
       :aria-hidden="focusIsolated || undefined"
       @mouseenter="onEnter"
       @mouseleave="onLeave">
    <div class="sidebar-inner">
      <div class="sidebar-logo">
        <DendroEmblem :size="30" spin />
        <span v-if="expanded || mobileOpen" class="logo-text">{{ t('brand') }}</span>
        <button class="sidebar-close" type="button" :aria-label="t('nav.closeNavigation')"
                @click="emit('close')">×</button>
      </div>

      <div class="nav-items">
        <router-link
          v-for="item in navItems"
          :key="item.route"
          :to="item.route"
          class="nav-item"
          :title="t(item.labelKey)"
          @click="emit('close')"
        >
          <span class="nav-icon"><SumeruIcon :name="item.icon" :size="20" /></span>
          <span v-if="expanded || mobileOpen" class="nav-label">{{ t(item.labelKey) }}</span>
          <span class="nav-glow"></span>
        </router-link>
      </div>

      <div class="sidebar-foot" v-if="expanded || mobileOpen">
        <router-link to="/sponsor" class="sponsor-entry" :title="t('sponsor.navTitle')" @click="emit('close')">
          <span class="sponsor-icon"><SumeruIcon name="tea" :size="14" /></span>
          <span class="sponsor-label">{{ t('sponsor.navTitle') }}</span>
        </router-link>
        <span class="foot-text">{{ t('tagline') }}</span>
        <span class="foot-signature">{{ t('brand_signature.full') }}</span>
      </div>
      <router-link v-else to="/sponsor" class="sponsor-entry-collapsed" :title="t('sponsor.navTitle')" @click="emit('close')">
        <span class="sponsor-icon"><SumeruIcon name="tea" :size="18" /></span>
      </router-link>
    </div>
  </nav>
</template>

<style scoped>
.sidebar {
  width: var(--sidebar-width);
  height: 100vh;
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

.sidebar-close {
  display: none;
  margin-left: auto;
  width: 36px;
  height: 36px;
  border: 1px solid var(--glass-border);
  border-radius: 9px;
  background: rgba(20, 40, 28, 0.6);
  color: var(--moon);
  font-size: 24px;
  cursor: pointer;
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
    width: var(--sidebar-mobile-width);
    z-index: var(--z-sidebar);
    transform: translateX(-100%);
    transition: transform var(--motion-normal) var(--ease-smooth);
  }
  .sidebar.expanded { width: var(--sidebar-mobile-width); animation: none; }
  .sidebar.mobile-open { transform: translateX(0); }
  .sidebar-close { display: inline-flex; align-items: center; justify-content: center; }
}

@media (prefers-reduced-motion: reduce) {
  .sidebar, .sidebar.expanded, .nav-item, .nav-icon { transition: none; animation: none; }
  .nav-item:hover, .nav-item:active, .nav-item:hover .nav-icon { transform: none; }
}
</style>
