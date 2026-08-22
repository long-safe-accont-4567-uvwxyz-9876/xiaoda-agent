<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, onDeactivated, onActivated } from 'vue'
import SumeruIcon from '../../components/fx/SumeruIcon.vue'
import { NTag, NButton, NInput, NSwitch, NSpin, NEmpty, useMessage } from 'naive-ui'
import {
  jspaceGetStatus, jspaceGetSignals, jspaceGetDirections,
  jspaceGetInterventions, jspaceDecompose, jspaceGetConfig, jspaceSetConfig,
  type JSpaceStatus, type JSpaceSignalEntry, type JSpaceDirection,
  type IntentFactorResult, type JSpaceConfig,
} from '../../api'
import { t } from '../../i18n'

const POLL_INTERVAL_MS = 3000

const INTENT_CSS_VARS: Record<string, string> = {
  knowledge: 'var(--jspace-knowledge)',
  emotional: 'var(--jspace-emotional)',
  safety: 'var(--jspace-safety)',
  creative: 'var(--jspace-creative)',
  factual: 'var(--jspace-factual)',
  social: 'var(--jspace-social)',
  procedural: 'var(--jspace-procedural)',
}

const props = defineProps<{ compact?: boolean }>()

const message = useMessage()
const loading = ref(false)
const status = ref<JSpaceStatus | null>(null)
const signals = ref<JSpaceSignalEntry[]>([])
const directions = ref<Record<string, JSpaceDirection>>({})
const interventions = ref<{ rules: any[]; convergence: any; history: any[] }>({ rules: [], convergence: {}, history: [] })
const config = ref<JSpaceConfig>({ enabled: true, signal_max_history: 1000, intent_use_llm: true, intent_llm_timeout: 10 })
const activeSection = ref('signals')

const decomposeText = ref('')
const decomposing = ref(false)
const decomposeResult = ref<{ factors: IntentFactorResult[]; residual: number; dominant: string | null; sparsity: number } | null>(null)

const signalTypes = computed(() => {
  const types = new Set(signals.value.map(s => s.signal_type))
  return Array.from(types).sort()
})

const filteredSignals = computed(() => {
  if (!selectedSignalType.value) return signals.value
  return signals.value.filter(s => s.signal_type === selectedSignalType.value)
})
const selectedSignalType = ref('')

let pollTimer: ReturnType<typeof setInterval> | null = null

async function loadAll() {
  loading.value = true
  try {
    const [s, sig, dir, inv, cfg] = await Promise.all([
      jspaceGetStatus().catch(() => null),
      jspaceGetSignals('', 100).catch(() => null),
      jspaceGetDirections().catch(() => null),
      jspaceGetInterventions().catch(() => null),
      jspaceGetConfig().catch(() => null),
    ])
    if (s) status.value = s
    if (sig && 'entries' in (sig as object)) signals.value = (sig as any).entries
    if (dir && 'directions' in (dir as object)) directions.value = (dir as any).directions
    if (inv) interventions.value = inv as any
    if (cfg) config.value = cfg
  } catch (error) {
    message.error(String(error))
  } finally {
    loading.value = false
  }
}

async function doDecompose() {
  if (!decomposeText.value.trim()) return
  decomposing.value = true
  try {
    decomposeResult.value = await jspaceDecompose(decomposeText.value, config.value.intent_use_llm)
  } catch (error) {
    message.error(String(error))
  } finally {
    decomposing.value = false
  }
}

async function saveConfig() {
  try {
    await jspaceSetConfig(config.value)
    message.success(t('jspace.save'))
  } catch (error) {
    message.error(String(error))
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(() => {
    jspaceGetSignals('', 100)
      .then(r => { if (r && 'entries' in (r as object)) signals.value = (r as any).entries })
      .catch(() => {})
    jspaceGetStatus()
      .then(r => { if (r) status.value = r })
      .catch(() => {})
  }, POLL_INTERVAL_MS)
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

onMounted(() => { loadAll(); startPolling() })
onBeforeUnmount(stopPolling)
onDeactivated(stopPolling)
onActivated(startPolling)

function formatTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString()
}
</script>

<template>
  <div class="jspace-panel" :class="{ compact }">
    <n-spin :show="loading && !status">
      <div v-if="status" class="status-bar">
        <n-tag :type="status.enabled ? 'success' : 'default'" size="small" round>
          {{ status.enabled ? t('jspace.enabled') : t('jspace.disabled') }}
        </n-tag>
        <span class="status-item" v-if="status.signal_stream.active">
          {{ t('jspace.signalStream') }}: {{ status.signal_stream.buffer_size }}
        </span>
        <span class="status-item" v-if="status.direction_registry.active">
          {{ t('jspace.directionRegistry') }}: {{ status.direction_registry.directions.length }}
        </span>
        <span class="status-item" v-if="status.intervention_loop.active">
          {{ t('jspace.interventionLoop') }}: {{ status.intervention_loop.rules_count }} {{ t('jspace.rulesCount') }}
        </span>
      </div>

      <div class="section-tabs">
        <button v-for="sec in ['signals', 'directions', 'interventions', 'decompose', 'config']" :key="sec"
          :class="['sec-btn', { active: activeSection === sec }]"
          @click="activeSection = sec">
          {{ t(`jspace.${sec}`) }}
        </button>
      </div>

      <section v-if="activeSection === 'signals'" class="section">
        <div class="filter-row" v-if="signalTypes.length">
          <n-tag v-for="st in signalTypes" :key="st" size="tiny" round
            :type="selectedSignalType === st ? 'primary' : 'default'"
            class="signal-filter" @click="selectedSignalType = selectedSignalType === st ? '' : st">
            {{ st }}
          </n-tag>
        </div>
        <div v-if="filteredSignals.length" class="signal-list">
          <div v-for="sig in filteredSignals.slice(-20).reverse()" :key="sig.timestamp + sig.signal_type" class="signal-row">
            <n-tag size="tiny" round type="info">{{ sig.signal_type }}</n-tag>
            <span class="sig-value">{{ sig.value.toFixed(3) }}</span>
            <span class="sig-source">{{ sig.source }}</span>
            <span class="sig-time">{{ formatTime(sig.timestamp) }}</span>
          </div>
        </div>
        <n-empty v-else :description="t('jspace.noData')" size="small" />
      </section>

      <section v-if="activeSection === 'directions'" class="section">
        <div v-if="Object.keys(directions).length" class="direction-list">
          <div v-for="(dir, name) in directions" :key="name" class="direction-card">
            <div class="dir-name">{{ name }}</div>
            <div class="dir-dims">
              <n-tag v-for="(val, dim) in dir.dimensions" :key="dim" size="tiny" round
                :type="val > 0 ? 'success' : val < 0 ? 'error' : 'default'">
                {{ dim }}: {{ val > 0 ? '+' : '' }}{{ val.toFixed(2) }}
              </n-tag>
            </div>
            <div class="dir-meta">|v| = {{ dir.magnitude.toFixed(3) }} · {{ dir.source }}</div>
          </div>
        </div>
        <n-empty v-else :description="t('jspace.noData')" size="small" />
      </section>

      <section v-if="activeSection === 'interventions'" class="section">
        <div v-if="interventions.rules.length" class="rule-list">
          <div v-for="rule in interventions.rules" :key="rule.signal_type + rule.direction_name" class="rule-card">
            <n-tag size="tiny" round type="warning">{{ rule.signal_type }}</n-tag>
            <span>{{ rule.trigger_above ? '>' : '<' }} {{ rule.threshold }}</span>
            <span>→ {{ rule.direction_name }} × {{ rule.alpha }}</span>
            <n-tag size="tiny" round>{{ rule.mode }}</n-tag>
          </div>
        </div>
        <div v-if="interventions.convergence" class="convergence-info">
          <n-tag :type="interventions.convergence.converging ? 'success' : 'error'" size="small" round>
            {{ interventions.convergence.converging ? t('jspace.converging') : t('jspace.diverging') }}
          </n-tag>
          <span v-if="interventions.convergence.trend != null">
            trend: {{ interventions.convergence.trend.toFixed(4) }}
          </span>
        </div>
        <n-empty v-if="!interventions.rules.length" :description="t('jspace.noData')" size="small" />
      </section>

      <section v-if="activeSection === 'decompose'" class="section">
        <div class="decompose-input">
          <n-input v-model:value="decomposeText" type="textarea" :rows="3"
            :placeholder="t('jspace.decomposeInput')" />
          <n-button :loading="decomposing" type="primary" size="small" @click="doDecompose">
            {{ t('jspace.decomposeButton') }}
          </n-button>
        </div>
        <div v-if="decomposeResult" class="decompose-result">
          <div class="result-meta">
            <n-tag v-if="decomposeResult.dominant" size="small" round type="success">
              {{ t('jspace.dominantIntent') }}: {{ decomposeResult.dominant }}
            </n-tag>
            <span>{{ t('jspace.sparsity') }}: {{ decomposeResult.sparsity.toFixed(3) }}</span>
            <span>{{ t('jspace.residual') }}: {{ decomposeResult.residual.toFixed(3) }}</span>
          </div>
          <div class="factor-bars">
            <div v-for="factor in decomposeResult.factors" :key="factor.name" class="factor-bar">
              <span class="factor-name">{{ factor.name }}</span>
              <div class="bar-track">
                <div class="bar-fill" :class="'bar-' + factor.name" :style="{ width: `${factor.activation * 100}%` }"></div>
              </div>
              <span class="factor-val">{{ factor.activation.toFixed(2) }}</span>
              <span v-if="factor.evidence" class="factor-evidence" :title="factor.evidence"><SumeruIcon name="note" :size="13" variant="duo" tone="edit" interactive /></span>
            </div>
          </div>
        </div>
      </section>

      <section v-if="activeSection === 'config'" class="section">
        <div class="config-form">
          <label class="config-row">
            <span>{{ t('jspace.configEnabled') }}</span>
            <n-switch v-model:value="config.enabled" />
          </label>
          <label class="config-row">
            <span>{{ t('jspace.configSignalHistory') }}</span>
            <n-input-number v-model:value="config.signal_max_history" size="small" style="width: 80px" />
          </label>
          <label class="config-row">
            <span>{{ t('jspace.configIntentLlm') }}</span>
            <n-switch v-model:value="config.intent_use_llm" />
          </label>
          <label class="config-row">
            <span>{{ t('jspace.configIntentTimeout') }}</span>
            <n-input-number v-model:value="config.intent_llm_timeout" size="small" style="width: 80px" />
          </label>
          <n-button type="primary" size="small" @click="saveConfig">{{ t('jspace.save') }}</n-button>
        </div>
      </section>
    </n-spin>
  </div>
</template>

<style scoped>
.jspace-panel {
  --jspace-knowledge: #4fc3f7;
  --jspace-emotional: #f48fb1;
  --jspace-safety: #ff8a65;
  --jspace-creative: #ce93d8;
  --jspace-factual: #81c784;
  --jspace-social: #ffd54f;
  --jspace-procedural: #90a4ae;
  font-size: 13px;
}
.jspace-panel.compact { font-size: 12px; }
.status-bar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.status-item { color: var(--moon-dim); font-size: 12px; }
.section-tabs { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
.sec-btn { padding: 3px 10px; border: 1px solid var(--moon-border, #333); border-radius: 12px; background: transparent; color: var(--moon-dim); cursor: pointer; font-size: 12px; transition: all .2s; }
.sec-btn:hover { border-color: var(--moon-accent, #7c6fff); color: var(--moon-fg); }
.sec-btn.active { background: var(--moon-accent, #7c6fff); color: #fff; border-color: var(--moon-accent, #7c6fff); }
.section { min-height: 120px; }
.filter-row { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 8px; }
.signal-filter { cursor: pointer; }
.signal-list { display: flex; flex-direction: column; gap: 4px; }
.signal-row { display: flex; align-items: center; gap: 6px; padding: 2px 0; }
.sig-value { font-weight: 600; min-width: 50px; }
.sig-source { color: var(--moon-dim); font-size: 11px; }
.sig-time { color: var(--moon-dim); font-size: 11px; margin-left: auto; }
.direction-list { display: flex; flex-direction: column; gap: 8px; }
.direction-card { padding: 8px; border: 1px solid var(--moon-border, #333); border-radius: 6px; }
.dir-name { font-weight: 600; margin-bottom: 4px; }
.dir-dims { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 4px; }
.dir-meta { color: var(--moon-dim); font-size: 11px; }
.rule-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; }
.rule-card { display: flex; align-items: center; gap: 6px; padding: 4px 8px; border: 1px solid var(--moon-border, #333); border-radius: 4px; }
.convergence-info { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.decompose-input { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; }
.result-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
.factor-bars { display: flex; flex-direction: column; gap: 4px; }
.factor-bar { display: flex; align-items: center; gap: 6px; }
.factor-name { min-width: 70px; font-size: 12px; }
.bar-track { flex: 1; height: 8px; background: var(--moon-bg-soft, #1a1a2e); border-radius: 4px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 4px; transition: width .3s; }
.bar-knowledge { background: var(--jspace-knowledge); }
.bar-emotional { background: var(--jspace-emotional); }
.bar-safety { background: var(--jspace-safety); }
.bar-creative { background: var(--jspace-creative); }
.bar-factual { background: var(--jspace-factual); }
.bar-social { background: var(--jspace-social); }
.bar-procedural { background: var(--jspace-procedural); }
.factor-val { min-width: 30px; font-size: 11px; text-align: right; }
.factor-evidence { cursor: help; }
.config-form { display: flex; flex-direction: column; gap: 10px; }
.config-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
</style>