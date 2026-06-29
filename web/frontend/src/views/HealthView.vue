<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { NButton, useMessage } from 'naive-ui'
import { get, post } from '../api'
import { getWsClient } from '../api/ws'
import Tilt3D from '../components/fx/Tilt3D.vue'
import { t } from '../i18n'

const message = useMessage()
const ws = getWsClient()

interface ProbeCard {
  id: string
  label: string
  detail: string
  state: 'idle' | 'running' | 'ok' | 'fail'
  latency?: number
  info?: string
  audioUrl?: string
  flipped?: boolean
}

const probes = ref<ProbeCard[]>([])
const runningAll = ref(false)
const lastReport = ref<any>(null)

onMounted(async () => {
  await load()
  ws.on('health_progress', onProgress)
  ws.on('health_done', onDone)
})

onBeforeUnmount(() => {
  ws.off('health_progress', onProgress)
  ws.off('health_done', onDone)
})

async function load() {
  try {
    const list = await get<any[]>('/health/probes')
    probes.value = list.map(p => ({ ...p, state: 'idle' as const }))
    lastReport.value = await get('/health/report')
  } catch (e: any) {
    message.error(e.message)
  }
}

function onProgress(e: any) {
  const card = probes.value.find(p => p.id === e.item)
  if (card) {
    card.state = e.ok ? 'ok' : 'fail'
    card.latency = e.latency_ms
    card.info = e.detail
    card.flipped = true
  }
}

function onDone(e: any) {
  runningAll.value = false
  message.success(`全量自检完成：${e.passed}/${e.total} 通过`)
  get('/health/report').then(r => { lastReport.value = r }).catch(() => {})
}

async function runOne(card: ProbeCard) {
  card.state = 'running'
  card.flipped = false
  try {
    const res = await post(`/health/test/${card.id}`)
    card.state = res.ok ? 'ok' : 'fail'
    card.latency = res.latency_ms
    card.info = res.error || res.reply_excerpt || res.note || ''
    card.audioUrl = res.audio_url
    card.flipped = true
  } catch (e: any) {
    card.state = 'fail'
    card.info = e.message
    card.flipped = true
  }
}

async function runAll() {
  runningAll.value = true
  for (const p of probes.value) { p.state = 'running'; p.flipped = false }
  try {
    await post('/health/test-all')
  } catch (e: any) {
    runningAll.value = false
    message.error(e.message)
  }
}

const stateIcon: Record<string, string> = {
  idle: '◌', running: '⏳', ok: '✓', fail: '✗',
}
</script>

<template>
  <div class="health-view">
    <div class="view-header">
      <h2>🩺 {{ t('healthView.title') }}</h2>
      <div class="header-right">
        <span v-if="lastReport?.run_at" class="last-report">
          {{ t('healthView.lastCheck') }}：{{ new Date(lastReport.run_at * 1000).toLocaleString('zh-CN') }}
          · {{ lastReport.passed }}/{{ lastReport.total }} {{ t('healthView.pass') }}
        </span>
        <n-button type="primary" :loading="runningAll" @click="runAll">🩺 {{ t('healthView.fullCheck') }}</n-button>
      </div>
    </div>

    <div class="probe-grid">
      <Tilt3D v-for="p in probes" :key="p.id">
        <div class="probe-card glass-panel" :class="[p.state, { flipped: p.flipped }]">
          <div class="card-face front" v-if="!p.flipped">
            <div class="probe-head">
              <span class="probe-light" :class="p.state"></span>
              <span class="probe-label">{{ p.label }}</span>
            </div>
            <div class="probe-detail mono">{{ p.detail }}</div>
            <n-button size="tiny" :loading="p.state === 'running'" @click="runOne(p)">{{ t('healthView.unitTest') }}</n-button>
          </div>
          <div class="card-face back" v-else>
            <div class="probe-head">
              <span class="probe-light" :class="p.state"></span>
              <span class="probe-label">{{ p.label }}</span>
              <span class="probe-result">{{ stateIcon[p.state] }}</span>
            </div>
            <div class="probe-info">
              <span v-if="p.latency != null" class="latency">{{ p.latency }}ms</span>
              <span class="info-text" :class="{ error: p.state === 'fail' }">{{ p.info }}</span>
            </div>
            <audio v-if="p.audioUrl" :src="p.audioUrl" controls class="probe-audio"></audio>
            <n-button size="tiny" quaternary @click="runOne(p)">{{ t('healthView.retest') }}</n-button>
          </div>
        </div>
      </Tilt3D>
    </div>
  </div>
</template>

<style scoped>
.view-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 16px; flex-wrap: wrap; gap: 10px;
}
.view-header h2 { font-family: 'Noto Serif SC', serif; }
.header-right { display: flex; align-items: center; gap: 14px; }
.last-report { font-size: 12.5px; color: var(--moon-dim); }

.probe-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}

.probe-card {
  padding: 14px 16px;
  min-height: 110px;
  display: flex;
  flex-direction: column;
  transition: border-color 0.3s, transform 0.5s;
  transform-style: preserve-3d;
}
.probe-card.flipped { animation: flip-in 0.5s var(--ease-out); }
.probe-card.ok { border-color: rgba(127, 214, 80, 0.45); }
.probe-card.fail { border-color: rgba(217, 106, 95, 0.55); }

@keyframes flip-in {
  from { transform: perspective(800px) rotateY(180deg); opacity: 0.4; }
  to { transform: perspective(800px) rotateY(0); opacity: 1; }
}

.card-face { display: flex; flex-direction: column; gap: 8px; flex: 1; }

.probe-head { display: flex; align-items: center; gap: 8px; }
.probe-label { font-size: 13.5px; font-weight: 600; flex: 1; }
.probe-result { font-size: 18px; font-weight: 700; }
.probe-card.ok .probe-result { color: var(--dendro); }
.probe-card.fail .probe-result { color: var(--alert); }

.probe-light {
  width: 9px; height: 9px; border-radius: 50%;
  background: #555; flex-shrink: 0;
}
.probe-light.running { background: var(--wisdom); animation: breathe 1s ease-in-out infinite; }
.probe-light.ok { background: var(--dendro); box-shadow: 0 0 8px var(--dendro); }
.probe-light.fail { background: var(--alert); box-shadow: 0 0 8px var(--alert); }

.probe-detail { font-size: 11.5px; color: var(--moon-dim); flex: 1; }
.mono { font-family: 'JetBrains Mono', monospace; }

.probe-info { flex: 1; font-size: 12px; }
.latency { color: var(--wisdom); margin-right: 8px; font-family: 'JetBrains Mono', monospace; }
.info-text { color: var(--moon-dim); word-break: break-all; }
.info-text.error { color: var(--alert); }

.probe-audio { width: 100%; height: 32px; }

@keyframes breathe {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 1; }
}
</style>
