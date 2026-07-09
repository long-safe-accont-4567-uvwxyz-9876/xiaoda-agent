<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import {
  NButton, NSwitch, NModal, NForm, NFormItem, NInput, NInputNumber,
  NSlider, NRadioGroup, NRadio, NCheckboxGroup, NCheckbox, NTimePicker,
  NTag, NPopconfirm, useMessage,
} from 'naive-ui'
import { get, post, put, del } from '../api'
import { t, tf } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'

const message = useMessage()

const config = ref<any>({ enabled: true, greeting_max_per_day: 3 })
let _dndSeq = 0
const dndPeriods = ref<Array<{ start: string; end: string; _key: number }>>([])
const greetings = ref<any[]>([])
const history = ref<any[]>([])

const showForm = ref(false)
const isCreate = ref(true)
const form = ref<any>({})
const testing = ref(false)

const weekLabels = computed(() => [1, 2, 3, 4, 5, 6, 7].map(i => t(`scheduleView.weekLabel${i}`)))

onMounted(loadAll)

async function loadAll() {
  try {
    config.value = await get('/schedule/config')
    dndPeriods.value = (config.value.dnd_periods || []).map((p: any) => ({ ...p, _key: ++_dndSeq }))
    greetings.value = await get<any[]>('/schedule/greetings')
    history.value = await get<any[]>('/schedule/history?days=7')
  } catch (e: any) {
    message.error(e.message)
  }
}

async function saveConfig() {
  try {
    await put('/schedule/config', {
      enabled: config.value.enabled,
      greeting_max_per_day: config.value.greeting_max_per_day,
    })
    message.success(t('scheduleView.effectDone'))
  } catch (e: any) { message.error(e.message) }
}

async function saveDnd() {
  try {
    const cleaned = dndPeriods.value.map(p => ({ start: p.start, end: p.end }))
    const result = await put('/schedule/dnd', { periods: cleaned })
    dndPeriods.value = result.map((p: any) => ({ ...p, _key: ++_dndSeq }))
    message.success(t('scheduleView.quietUpdated'))
  } catch (e: any) { message.error(e.message) }
}

function addDnd() {
  dndPeriods.value.push({ start: '23:00', end: '08:00', _key: ++_dndSeq })
}

function removeDnd(i: number) {
  dndPeriods.value.splice(i, 1)
  saveDnd()
}

function safeJsonParse(text: string, fallback: any) {
  try { return JSON.parse(text) } catch { return fallback }
}

function openForm(g: any | null) {
  isCreate.value = !g
  form.value = g
    ? { ...g, days: safeJsonParse(g.days || '[]', []), channels: safeJsonParse(g.channels || '["web"]', ['web']) }
    : { type: 'fixed', time: '08:30', window_start: '09:00', window_end: '22:00',
        count_per_day: 2, days: [1, 2, 3, 4, 5, 6, 7], channels: ['web'], prompt_hint: '' }
  showForm.value = true
}

async function saveGreeting() {
  try {
    if (isCreate.value) {
      await post('/schedule/greetings', form.value)
      message.success(t('scheduleView.planCreated'))
    } else {
      await put(`/schedule/greetings/${form.value.id}`, form.value)
      message.success(t('scheduleView.planUpdated'))
    }
    showForm.value = false
    await loadAll()
  } catch (e: any) { message.error(e.message) }
}

async function toggleGreeting(g: any, value: boolean) {
  try {
    await put(`/schedule/greetings/${g.id}`, { enabled: value })
    g.enabled = value ? 1 : 0
    message.success(tf('scheduleView.planToggled', !!value))
  } catch (e: any) { message.error(e.message) }
}

async function removeGreeting(id: number) {
  try {
    await del(`/schedule/greetings/${id}`)
    message.success(t('scheduleView.planDeleted'))
    await loadAll()
  } catch (e: any) { message.error(e.message) }
}

async function testFire(channels: string[] = ['web']) {
  testing.value = true
  try {
    const r = await post('/schedule/test-greeting', { prompt_hint: '', channels })
    if (!r.sent) {
      message.warning(r.message || t('scheduleView.allChannelsFailed'))
    } else {
      for (const [ch, res] of Object.entries<any>(r.channels || {})) {
        if (res.ok) message.success(`${ch === 'qq' ? 'QQ' : 'Web'} ${t('scheduleView.delivered')}：「${r.text}」`)
        else message.error(`${ch === 'qq' ? 'QQ' : 'Web'} ${t('scheduleView.channelFailed')}: ${res.error || t('scheduleView.unknownReason')}`)
      }
    }
    history.value = await get<any[]>('/schedule/history?days=7')
  } catch (e: any) {
    message.error(e.message)
  } finally {
    testing.value = false
  }
}

function describeDays(daysJson: string): string {
  let days: number[]
  try { days = JSON.parse(daysJson || '[]') } catch { days = [] }
  if (days.length === 7) return t('scheduleView.everyday')
  return t('scheduleView.weekPrefix') + days.map(d => weekLabels.value[d - 1] || d).join('/')
}

function parseChannels(channelsJson: string): string {
  try { return JSON.parse(channelsJson || '[]').join('+') } catch { return '?' }
}

const reasonLabel: Record<string, string> = {
  fixed: t('scheduleView.fixedLabel'), random: t('scheduleView.randomLabel'), idle: t('scheduleView.idleLabel'), manual_test: t('scheduleView.testLabel'),
}
</script>

<template>
  <div class="schedule-view">
    <h2 class="view-title">⏰ {{ t('scheduleView.title') }}</h2>

    <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
      <h3>{{ t('scheduleView.masterSwitch') }}</h3>
      <div class="config-row">
        <label class="cfg">
          {{ t('scheduleView.proactive') }}
          <n-switch v-model:value="config.enabled" @update:value="saveConfig" />
        </label>
        <label class="cfg wide">
          {{ t('scheduleView.dailyLimit') }} {{ config.greeting_max_per_day }} {{ t('scheduleView.greetingMaxUnit') }}
          <n-slider v-model:value="config.greeting_max_per_day" :min="0" :max="10"
                    style="width: 180px" @update:value="saveConfig" />
        </label>
        <n-button size="small" :loading="testing" @click="testFire(['web'])">{{ t('scheduleView.testWeb') }}</n-button>
        <n-button size="small" :loading="testing" @click="testFire(['qq'])">📱 {{ t('scheduleView.testQQ') }}</n-button>
      </div>
    </section></Tilt3D>

    <section class="glass-panel section">
      <div class="section-head">
        <h3>{{ t('scheduleView.plans') }}</h3>
        <n-button size="small" type="primary" @click="openForm(null)">＋ {{ t('scheduleView.addPlan') }}</n-button>
      </div>
      <div class="greeting-list">
        <Tilt3D v-for="g in greetings" :key="g.id"><div class="greeting-card">
          <div class="g-main">
            <span class="g-icon">{{ g.type === 'fixed' ? '⏰' : '🎲' }}</span>
            <span class="g-desc">
              <template v-if="g.type === 'fixed'">{{ describeDays(g.days) }} {{ g.time }}</template>
              <template v-else>{{ describeDays(g.days) }} {{ g.window_start }}~{{ g.window_end }} {{ tf('scheduleView.randomTimes', g.count_per_day) }}</template>
            </span>
            <n-tag v-if="g.prompt_hint" size="tiny" :bordered="false">{{ g.prompt_hint }}</n-tag>
            <n-tag size="tiny" type="info" :bordered="false">{{ parseChannels(g.channels) }}</n-tag>
          </div>
          <div class="g-ops">
            <n-switch size="small" :value="!!g.enabled"
                      @update:value="(v: boolean) => toggleGreeting(g, v)" />
            <n-button size="tiny" @click="openForm(g)">{{ t('scheduleView.edit') }}</n-button>
            <n-popconfirm @positive-click="removeGreeting(g.id)">
              <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('scheduleView.delete') }}</n-button></template>
              {{ t('scheduleView.deleteConfirm') }}
            </n-popconfirm>
          </div>
        </div></Tilt3D>
        <div v-if="!greetings.length" class="empty-hint">{{ t('scheduleView.noPlans') }}</div>
      </div>
    </section>

    <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
      <div class="section-head">
        <h3>{{ t('scheduleView.quietHoursTitle') }} <span class="hint">{{ t('scheduleView.quietHoursDesc') }}</span></h3>
        <n-button size="small" @click="addDnd">{{ t('scheduleView.addSlot') }}</n-button>
      </div>
      <div class="dnd-list">
        <div v-for="(p, i) in dndPeriods" :key="p._key" class="dnd-row">
          <n-time-picker :formatted-value="p.start" format="HH:mm" value-format="HH:mm"
                         @update:formatted-value="(v: string | null) => { if (v) { p.start = v; saveDnd() } }" />
          <span>—</span>
          <n-time-picker :formatted-value="p.end" format="HH:mm" value-format="HH:mm"
                         @update:formatted-value="(v: string | null) => { if (v) { p.end = v; saveDnd() } }" />
          <n-button size="tiny" type="error" quaternary @click="removeDnd(i)">{{ t('scheduleView.remove') }}</n-button>
        </div>
        <div v-if="!dndPeriods.length" class="empty-hint">{{ t('scheduleView.noQuietHours') }}</div>
      </div>
    </section></Tilt3D>

    <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
      <h3>{{ t('scheduleView.sent7d') }}</h3>
      <div class="history-list">
        <div v-for="h in history" :key="h.id" class="history-row">
          <span class="h-time">{{ new Date(h.fired_at * 1000).toLocaleString('zh-CN') }}</span>
          <n-tag size="tiny" :bordered="false">{{ reasonLabel[h.reason] || h.reason }}</n-tag>
          <span class="h-content">{{ h.content }}</span>
          <span class="h-channel">{{ h.channel }}</span>
        </div>
        <div v-if="!history.length" class="empty-hint">{{ t('scheduleView.noRecords') }}</div>
      </div>
    </section></Tilt3D>

    <n-modal v-model:show="showForm" preset="card"
             :title="isCreate ? t('scheduleView.addPlanTitle') : t('scheduleView.editPlanTitle')"
             style="width: min(540px, 94vw)">
      <n-form label-placement="left" label-width="100">
        <n-form-item :label="t('scheduleView.type')">
          <n-radio-group v-model:value="form.type">
            <n-radio value="fixed">{{ t('scheduleView.fixed') }}</n-radio>
            <n-radio value="random">{{ t('scheduleView.random') }}</n-radio>
          </n-radio-group>
        </n-form-item>
        <n-form-item v-if="form.type === 'fixed'" :label="t('scheduleView.time')">
          <n-time-picker v-model:formatted-value="form.time" format="HH:mm" value-format="HH:mm" />
        </n-form-item>
        <template v-else>
          <n-form-item :label="t('scheduleView.window')">
            <n-time-picker v-model:formatted-value="form.window_start" format="HH:mm" value-format="HH:mm" />
            <span style="margin: 0 8px">{{ t('scheduleView.to') }}</span>
            <n-time-picker v-model:formatted-value="form.window_end" format="HH:mm" value-format="HH:mm" />
          </n-form-item>
          <n-form-item :label="t('scheduleView.dailyCount')">
            <n-input-number v-model:value="form.count_per_day" :min="1" :max="10" />
          </n-form-item>
        </template>
        <n-form-item :label="t('scheduleView.weekday')">
          <n-checkbox-group v-model:value="form.days">
            <n-checkbox v-for="(label, i) in weekLabels" :key="i" :value="i + 1" :label="label" />
          </n-checkbox-group>
        </n-form-item>
        <n-form-item :label="t('scheduleView.topic')">
          <n-input v-model:value="form.prompt_hint" :placeholder="t('scheduleView.topicPh')" />
        </n-form-item>
        <n-form-item :label="t('scheduleView.channel')">
          <n-checkbox-group v-model:value="form.channels">
            <n-checkbox value="web" label="Web" />
            <n-checkbox value="qq" label="QQ" />
          </n-checkbox-group>
        </n-form-item>
      </n-form>
      <template #footer>
        <div style="display:flex; justify-content:flex-end; gap:10px">
          <n-button @click="showForm = false">{{ t('cancel') }}</n-button>
          <n-button type="primary" @click="saveGreeting">{{ t('save') }}</n-button>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.view-title { font-family: 'Noto Serif SC', serif; margin-bottom: 14px; }

.section { padding: 16px 18px; margin-bottom: 14px; }
.section h3 { font-size: 14px; color: var(--dendro); margin-bottom: 12px; }
.section-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.section-head h3 { margin: 0; }
.hint { font-size: 11.5px; color: var(--moon-dim); font-weight: 400; margin-left: 8px; }

.config-row { display: flex; align-items: center; gap: 26px; flex-wrap: wrap; }
.cfg { display: flex; align-items: center; gap: 10px; font-size: 13.5px; }
.cfg.wide { gap: 14px; }

.greeting-list { display: flex; flex-direction: column; gap: 8px; }
.greeting-card {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; padding: 10px 12px; border-radius: 10px;
  border: 1px solid var(--glass-border); flex-wrap: wrap;
}
.g-main { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.g-icon { font-size: 16px; }
.g-desc { font-size: 13.5px; }
.g-ops { display: flex; align-items: center; gap: 8px; }

.dnd-list { display: flex; flex-direction: column; gap: 8px; }
.dnd-row { display: flex; align-items: center; gap: 10px; }

.history-list { display: flex; flex-direction: column; gap: 4px; max-height: 280px; overflow-y: auto; }
.history-row {
  display: flex; align-items: center; gap: 10px;
  font-size: 12.5px; padding: 5px 4px;
  border-bottom: 1px solid rgba(127, 214, 80, 0.06);
}
.h-time { color: var(--moon-dim); font-family: 'JetBrains Mono', monospace; font-size: 11px; flex-shrink: 0; }
.h-content { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.h-channel { color: var(--moon-dim); font-size: 11px; }

.empty-hint { color: var(--moon-dim); font-size: 13px; padding: 8px 0; }
</style>