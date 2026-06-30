<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { useChatStore } from '../../stores/chat'
import { getWsClient } from '../../api/ws'
import { get } from '../../api'
import type { WsEvent } from '../../api/ws'
import { t } from '../../i18n'

import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { WebLinksAddon } from '@xterm/addon-web-links'
import '@xterm/xterm/css/xterm.css'

const chat = useChatStore()
const ws = getWsClient()

const panelOpen = ref(false)
const showNewDialog = ref(false)
const newShellType = ref('bash')

// ── OS 检测（服务端） ──
const serverOs = ref('linux')
const isWindows = computed(() => serverOs.value === 'windows')

get<{ os: string; shell: string }>('/system/os').then(res => {
  serverOs.value = res.os
  newShellType.value = res.shell
}).catch(() => {})

// ── 多终端会话 ──
interface TermSession {
  id: string
  name: string
  shell: string
  terminal: Terminal
  fitAddon: FitAddon
  alive: boolean
  container: HTMLDivElement | null
}

const sessions = ref<TermSession[]>([])
const activeSessionId = ref('')

const activeSession = computed(() =>
  sessions.value.find(s => s.id === activeSessionId.value) || null
)

const shellOptions = computed(() => isWindows.value
  ? [
      { value: 'cmd',       label: 'CMD',      icon: '\\' },
      { value: 'powershell', label: 'PowerShell', icon: 'PS' },
      { value: 'wsl',       label: 'WSL',      icon: '$' },
      { value: 'bash',      label: 'Bash',     icon: '$' },
    ]
  : [
      { value: 'bash',  label: 'Bash',  icon: '$' },
      { value: 'zsh',   label: 'Zsh',   icon: '%' },
      { value: 'python', label: 'Python', icon: '»' },
      { value: 'node',  label: 'Node',  icon: '>' },
    ]
)

// ── xterm 主题（匹配纳西妲暗绿风格） ──
const TERM_THEME = {
  background: '#060e0a',
  foreground: '#f2f7ee',
  cursor: '#7fd650',
  cursorAccent: '#060e0a',
  selectionBackground: 'rgba(127, 214, 80, 0.25)',
  selectionForeground: '#f2f7ee',
  black: '#0a120c',
  red: '#d96a5f',
  green: '#7fd650',
  yellow: '#e8d5a3',
  blue: '#5fb3d9',
  magenta: '#d97fd9',
  cyan: '#5fd9c4',
  white: '#f2f7ee',
  brightBlack: '#3a5a42',
  brightRed: '#ff8a80',
  brightGreen: '#a8e878',
  brightYellow: '#f0e0b0',
  brightBlue: '#80d0f0',
  brightMagenta: '#f0a0f0',
  brightCyan: '#80f0e0',
  brightWhite: '#ffffff',
}

// ── WebSocket 事件路由 ──
function onTerminalOutput(e: WsEvent) {
  const termSid = e.term_sid as string
  const session = sessions.value.find(s => s.id === termSid)
  if (!session) return
  session.terminal.write(e.data as string)
}

function onTerminalExit(e: WsEvent) {
  const termSid = e.term_sid as string
  const session = sessions.value.find(s => s.id === termSid)
  if (!session) return
  session.alive = false
  session.terminal.writeln(`\r\n\x1b[38;2;117;106;95m[exited with code ${(e.returncode as number) || 0}]\x1b[0m`)
}

// ── 粘贴处理 ──
// 直接拦截 Ctrl+V keydown，用 clipboard API 读取内容发送到 PTY
// 这比监听 paste 事件更可靠（paste 事件可能被 xterm.js 内部拦截）
function _onDocKeyDown(e: KeyboardEvent) {
  if (!panelOpen.value) return
  // Ctrl+V 或 Cmd+V (Mac)
  if (!(e.ctrlKey || e.metaKey) || e.key !== 'v') return
  // 检查事件是否发生在终端面板内
  const target = e.target as HTMLElement
  if (!target.closest('.term-panel')) return
  const s = activeSession.value
  if (!s || !s.alive) return

  e.preventDefault()
  e.stopPropagation()

  // 优先用 clipboardData（paste 事件里的），否则用 API
  // keydown 事件没有 clipboardData，用 API
  navigator.clipboard.readText().then(text => {
    if (text) {
      ws.send({ type: 'terminal_input', term_sid: s.id, data: text })
    }
  }).catch(() => {})
}

// 按钮粘贴
async function handlePaste() {
  const s = activeSession.value
  if (!s || !s.alive) return
  try {
    const text = await navigator.clipboard.readText()
    if (text) {
      ws.send({ type: 'terminal_input', term_sid: s.id, data: text })
    }
  } catch {}
}

onMounted(() => {
  ws.on('terminal_output', onTerminalOutput)
  ws.on('terminal_exit', onTerminalExit)
  document.addEventListener('keydown', _onDocKeyDown, true)
})

onUnmounted(() => {
  ws.off('terminal_output', onTerminalOutput)
  ws.off('terminal_exit', onTerminalExit)
  document.removeEventListener('keydown', _onDocKeyDown, true)
  for (const s of sessions.value) {
    s.terminal.dispose()
  }
})

// ── 会话管理 ──
function createSession(shell: string) {
  const id = `ts-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`
  const shellLabels: Record<string, string> = {
    bash: 'bash', zsh: 'zsh', python: 'python', node: 'node',
    cmd: 'cmd', powershell: 'powershell', wsl: 'wsl',
  }

  const terminal = new Terminal({
    theme: TERM_THEME,
    fontFamily: "'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace",
    fontSize: 14,
    lineHeight: 1.3,
    cursorBlink: true,
    cursorStyle: 'bar',
    scrollback: 10000,
    allowProposedApi: true,
    convertEol: true,
  })
  const fitAddon = new FitAddon()
  terminal.loadAddon(fitAddon)
  terminal.loadAddon(new WebLinksAddon())

  const session: TermSession = {
    id, name: `${shellLabels[shell] || shell} #${sessions.value.length + 1}`,
    shell, terminal, fitAddon, alive: true, container: null,
  }
  sessions.value.push(session)
  activeSessionId.value = id
  showNewDialog.value = false

  // xterm.js 输入 → WS
  terminal.onData((data: string) => {
    if (!session.alive) return
    ws.send({ type: 'terminal_input', term_sid: id, data })
  })

  // 挂载到 DOM（等 nextTick 后找到容器）
  nextTick(() => {
    mountTerminal(session, shell)
  })
}

function mountTerminal(session: TermSession, shell: string) {
  const container = document.getElementById(`term-viewport-${session.id}`)
  if (!container) {
    setTimeout(() => mountTerminal(session, shell), 50)
    return
  }
  session.container = container as HTMLDivElement
  session.terminal.open(container)

  // 双重延迟确保容器尺寸稳定（DOM 渲染 + CSS 布局完成）
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      session.fitAddon.fit()

      const dims = session.fitAddon.proposeDimensions()
      if (dims) {
        ws.send({ type: 'terminal_start', term_sid: session.id, shell, cols: dims.cols, rows: dims.rows })
      } else {
        ws.send({ type: 'terminal_start', term_sid: session.id, shell, cols: 80, rows: 24 })
      }

      // ResizeObserver 保持尺寸同步
      const resizeObs = new ResizeObserver(() => {
        if (!session.alive) return
        try {
          session.fitAddon.fit()
          const d = session.fitAddon.proposeDimensions()
          if (d) {
            ws.send({ type: 'terminal_resize', term_sid: session.id, cols: d.cols, rows: d.rows })
          }
        } catch {}
      })
      resizeObs.observe(container)

      session.terminal.focus()
    })
  })
}

function closeSession(id: string) {
  const idx = sessions.value.findIndex(s => s.id === id)
  if (idx < 0) return
  const s = sessions.value[idx]
  s.alive = false
  ws.send({ type: 'terminal_kill', term_sid: id })
  s.terminal.dispose()
  sessions.value.splice(idx, 1)
  if (activeSessionId.value === id) {
    activeSessionId.value = sessions.value.length
      ? sessions.value[Math.max(0, idx - 1)].id
      : ''
  }
}

function switchSession(id: string) {
  activeSessionId.value = id
  nextTick(() => {
    const s = sessions.value.find(s => s.id === id)
    if (s) {
      s.fitAddon.fit()
      s.terminal.focus()
    }
  })
}

function refocusTerminal() {
  const s = activeSession.value
  if (s) {
    s.terminal.focus()
  }
}

watch(panelOpen, (v) => {
  nextTick(() => {
    const s = activeSession.value
    if (!s) return
    const el = document.getElementById('term-viewport-' + s.id)
    if (!el) return
    if (v) {
      el.style.display = ''
      // 等两帧让 DOM 尺寸稳定后再 fit
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          s.fitAddon.fit()
          s.terminal.focus()
        })
      })
    } else {
      // 关闭时隐藏活跃终端容器，防止后台渲染问题
      el.style.display = 'none'
    }
  })
})

function onPanelOpened() {
  // 面板动画结束后再 fit + focus，确保容器尺寸稳定
  const s = activeSession.value
  if (s) {
    setTimeout(() => {
      s.fitAddon.fit()
      s.terminal.focus()
    }, 50)
  }
}
</script>

<template>
  <Teleport to="body">
    <!-- 右下角浮动按钮 -->
    <transition name="fab-scale">
      <div v-if="!panelOpen" class="term-fab" @click="panelOpen = true" :title="t('chatTerminal.title')">
        <span class="fab-icon">▎>_</span>
        <span v-if="sessions.some(s => s.alive)" class="fab-dot"></span>
      </div>
    </transition>

    <!-- 右侧滑出面板（v-show 保持 DOM 存活，关闭后重新打开内容不丢失） -->
    <transition name="panel-slide" @after-enter="onPanelOpened">
      <div v-show="panelOpen" class="term-panel">
        <!-- 标题栏 -->
        <div class="panel-header">
          <span class="header-title">
            <span class="header-icon">▎>_</span>
            {{ t('chatTerminal.title') }}
          </span>
          <span class="header-actions">
            <button v-if="activeSession" class="header-btn" @click="handlePaste" title="粘贴 (Ctrl+V)">📋</button>
            <span class="header-os">{{ isWindows ? 'Windows' : 'Linux' }}</span>
          </span>
        </div>

        <!-- Tab 栏 -->
        <div class="panel-tabs">
          <div class="tabs-scroll">
            <div v-for="s in sessions" :key="s.id"
                 class="tab-item" :class="{ active: s.id === activeSessionId, alive: s.alive }"
                 @click="switchSession(s.id)">
              <span class="tab-icon">{{ s.shell === 'bash' ? '$' : s.shell === 'zsh' ? '%' : s.shell === 'python' ? '»' : s.shell === 'cmd' ? '\\' : '>' }}</span>
              <span class="tab-name">{{ s.name }}</span>
              <span v-if="!s.alive" class="tab-dead">dead</span>
              <button class="tab-close" @click.stop="closeSession(s.id)" :title="t('chatTerminal.close')">✕</button>
            </div>
          </div>
          <div class="tab-actions">
            <button class="tab-add" @click="showNewDialog = !showNewDialog" :title="t('chatTerminal.newTerm')">+</button>
            <button class="tab-close-panel" @click="panelOpen = false" :title="t('chatTerminal.close')">✕</button>
          </div>
        </div>

        <!-- 新建终端对话框 -->
        <transition name="dialog-slide">
          <div v-if="showNewDialog" class="new-term-dialog">
            <div class="dialog-title">{{ t('chatTerminal.newTermType') }}</div>
            <div class="shell-grid">
              <button v-for="opt in shellOptions" :key="opt.value"
                      class="shell-option"
                      :class="{ selected: newShellType === opt.value }"
                      @click="newShellType = opt.value">
                <span class="shell-icon">{{ opt.icon }}</span>
                <span class="shell-label">{{ opt.label }}</span>
              </button>
            </div>
            <button class="create-btn" @click="createSession(newShellType)">
              {{ t('chatTerminal.create') }}
            </button>
          </div>
        </transition>

        <!-- 终端视口：每个 session 一个独立容器 -->
        <div class="term-viewport-area" @click="refocusTerminal">
          <div v-if="!activeSession" class="term-empty">
            <div class="empty-icon">▎>_</div>
            <div class="empty-text">{{ t('chatTerminal.empty') }}</div>
            <button class="empty-btn" @click="showNewDialog = true">
              {{ t('chatTerminal.newTerm') }}
            </button>
          </div>
          <div v-for="s in sessions" :key="s.id"
               :id="'term-viewport-' + s.id"
               class="term-viewport"
               :class="{ visible: s.id === activeSessionId }">
          </div>
        </div>
      </div>
    </transition>
  </Teleport>
</template>

<style scoped>
/* ── 浮动按钮 ── */
.term-fab {
  position: fixed;
  right: 20px;
  bottom: 80px;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: rgba(8, 18, 12, 0.9);
  border: 1px solid rgba(127, 214, 80, 0.25);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  z-index: 998;
  transition: border-color 0.25s, box-shadow 0.25s, transform 0.2s;
  backdrop-filter: blur(8px);
}
.term-fab:hover {
  border-color: rgba(127, 214, 80, 0.5);
  box-shadow: 0 0 16px rgba(127, 214, 80, 0.2);
  transform: translateY(-2px);
}
.fab-icon {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--dendro, #7fd650);
  letter-spacing: -1px;
}
.fab-dot {
  position: absolute;
  top: -2px;
  right: -2px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--dendro, #7fd650);
}
.fab-scale-enter-active { transition: transform 0.35s cubic-bezier(0.32, 0.72, 0, 1), opacity 0.25s; }
.fab-scale-leave-active { transition: transform 0.2s ease, opacity 0.15s; }
.fab-scale-enter-from, .fab-scale-leave-to { transform: scale(0.5); opacity: 0; }

/* ── 右侧面板 ── */
.term-panel {
  position: fixed;
  right: 0; top: 0; bottom: 0;
  width: 560px;
  max-width: 90vw;
  background: #060e0a;
  border-left: 1px solid rgba(127, 214, 80, 0.15);
  display: flex;
  flex-direction: column;
  z-index: 999;
  box-shadow: -8px 0 32px rgba(0, 0, 0, 0.5);
}
.panel-slide-enter-active { transition: transform 0.4s cubic-bezier(0.32, 0.72, 0, 1); }
.panel-slide-leave-active { transition: transform 0.3s cubic-bezier(0.32, 0.72, 0, 1); }
.panel-slide-enter-from, .panel-slide-leave-to { transform: translateX(100%); }

/* ── 标题栏 ── */
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 14px;
  border-bottom: 1px solid rgba(127, 214, 80, 0.1);
  flex-shrink: 0;
  background: rgba(8, 18, 12, 0.5);
}
.header-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  color: var(--moon, #f2f7ee);
  font-weight: 600;
}
.header-icon {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--dendro, #7fd650);
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 6px;
}
.header-btn {
  background: none;
  border: 1px solid rgba(127, 214, 80, 0.15);
  border-radius: 4px;
  cursor: pointer;
  font-size: 12px;
  padding: 2px 6px;
  transition: border-color 0.15s, background 0.15s;
  line-height: 1;
}
.header-btn:hover {
  border-color: rgba(127, 214, 80, 0.4);
  background: rgba(127, 214, 80, 0.08);
}
.header-os {
  font-size: 10px;
  color: var(--moon-dim, rgba(242, 247, 238, 0.3));
  font-family: 'JetBrains Mono', monospace;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(127, 214, 80, 0.06);
}

/* ── Tab 栏 ── */
.panel-tabs {
  display: flex;
  align-items: center;
  border-bottom: 1px solid rgba(127, 214, 80, 0.1);
  flex-shrink: 0;
  min-height: 34px;
}
.tabs-scroll {
  flex: 1;
  display: flex;
  overflow-x: auto;
  scrollbar-width: none;
}
.tabs-scroll::-webkit-scrollbar { display: none; }
.tab-item {
  display: flex;
  align-items: center;
  gap: 5px;
  padding: 6px 10px;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: background 0.15s, border-color 0.2s;
  white-space: nowrap;
  flex-shrink: 0;
}
.tab-item:hover { background: rgba(127, 214, 80, 0.06); }
.tab-item.active { border-bottom-color: var(--dendro, #7fd650); }
.tab-icon {
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  color: var(--dendro, #7fd650);
  opacity: 0.7;
}
.tab-name {
  font-size: 11.5px;
  color: var(--moon-dim, rgba(242, 247, 238, 0.5));
}
.tab-item.active .tab-name { color: var(--moon, #f2f7ee); font-weight: 500; }
.tab-dead { font-size: 9px; color: #d96a5f; opacity: 0.6; }
.tab-close {
  background: none; border: none;
  cursor: pointer;
  color: rgba(242, 247, 238, 0.2);
  font-size: 10px;
  padding: 2px 3px;
  border-radius: 4px;
  transition: color 0.15s, background 0.15s;
  margin-left: 2px;
}
.tab-close:hover { color: #d96a5f; background: rgba(217, 106, 95, 0.15); }
.tab-actions {
  display: flex;
  align-items: center;
  gap: 2px;
  padding: 0 8px;
  flex-shrink: 0;
}
.tab-add, .tab-close-panel {
  background: none; border: none;
  cursor: pointer;
  color: var(--moon-dim, rgba(242, 247, 238, 0.35));
  font-size: 15px;
  padding: 4px 7px;
  border-radius: 6px;
  transition: color 0.15s, background 0.15s;
}
.tab-add:hover { color: var(--dendro, #7fd650); background: rgba(127, 214, 80, 0.1); }
.tab-close-panel:hover { color: #d96a5f; background: rgba(217, 106, 95, 0.1); }

/* ── 新建终端对话框 ── */
.new-term-dialog {
  padding: 10px 14px 12px;
  border-bottom: 1px solid rgba(127, 214, 80, 0.1);
  background: rgba(10, 24, 16, 0.5);
  flex-shrink: 0;
}
.dialog-slide-enter-active { transition: max-height 0.3s cubic-bezier(0.32, 0.72, 0, 1), opacity 0.2s; max-height: 200px; }
.dialog-slide-leave-active { transition: max-height 0.2s cubic-bezier(0.32, 0.72, 0, 1), opacity 0.15s; max-height: 200px; }
.dialog-slide-enter-from, .dialog-slide-leave-to { max-height: 0; opacity: 0; }
.dialog-title {
  font-size: 11px;
  color: var(--moon-dim, rgba(242, 247, 238, 0.4));
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 8px;
}
.shell-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 6px;
  margin-bottom: 8px;
}
.shell-option {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  padding: 8px 4px;
  border-radius: 8px;
  border: 1px solid rgba(127, 214, 80, 0.1);
  background: rgba(8, 18, 12, 0.6);
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s;
}
.shell-option:hover { border-color: rgba(127, 214, 80, 0.3); }
.shell-option.selected { border-color: var(--dendro, #7fd650); background: rgba(127, 214, 80, 0.1); }
.shell-icon {
  font-family: 'JetBrains Mono', monospace;
  font-size: 14px;
  color: var(--dendro, #7fd650);
}
.shell-label {
  font-size: 10px;
  color: var(--moon-dim, rgba(242, 247, 238, 0.5));
}
.create-btn {
  width: 100%;
  padding: 7px;
  border-radius: 8px;
  border: 1px solid rgba(127, 214, 80, 0.25);
  background: rgba(127, 214, 80, 0.1);
  color: var(--dendro, #7fd650);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.2s, border-color 0.2s;
}
.create-btn:hover { background: rgba(127, 214, 80, 0.18); border-color: var(--dendro, #7fd650); }

/* ── 终端视口区域 ── */
.term-viewport-area {
  flex: 1;
  overflow: hidden;
  position: relative;
}

/* 每个终端独立容器 */
.term-viewport {
  position: absolute;
  inset: 0;
  visibility: hidden;
  pointer-events: none;
}
.term-viewport.visible {
  visibility: visible;
  pointer-events: auto;
}

/* xterm.js 容器全局样式 — 让终端填满视口，背景透明 */
.term-viewport :deep(.xterm) {
  height: 100% !important;
  padding: 0;
  background: transparent !important;
}
.term-viewport :deep(.xterm-viewport) {
  overflow-y: auto !important;
  background: transparent !important;
}
.term-viewport :deep(.xterm-screen) {
  width: 100% !important;
  height: 100% !important;
}

.term-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  height: 100%;
  min-height: 200px;
  color: rgba(242, 247, 238, 0.25);
}
.empty-icon {
  font-family: 'JetBrains Mono', monospace;
  font-size: 24px;
  color: var(--dendro, #7fd650);
  opacity: 0.3;
  animation: breathe 3s ease-in-out infinite;
}
@keyframes breathe { 0%, 100% { opacity: 0.2; } 50% { opacity: 0.4; } }
.empty-text { font-size: 12px; }
.empty-btn {
  padding: 6px 16px;
  border-radius: 8px;
  border: 1px solid rgba(127, 214, 80, 0.25);
  background: rgba(127, 214, 80, 0.08);
  color: var(--dendro, #7fd650);
  font-size: 12px;
  cursor: pointer;
  transition: background 0.2s, border-color 0.2s;
}
.empty-btn:hover { background: rgba(127, 214, 80, 0.15); border-color: var(--dendro, #7fd650); }

@media (max-width: 600px) { .term-panel { width: 100vw; } }
</style>
