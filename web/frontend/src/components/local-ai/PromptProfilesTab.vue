<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { NButton, NEmpty, NInput, NSelect, NTag, useMessage } from 'naive-ui'
import { fetchModelNodes } from '../../api/localAi'
import {
  fetchPromptProfiles, isSweep, promotePromptProfile, rollbackPromptProfile,
  runPromptAb, stagePromptProfile,
} from '../../api/promptProfiles'
import type {
  PromptAbResult, PromptAbSweep, PromptProfilesInfo,
} from '../../api/promptProfiles'

const message = useMessage()

type AbRun = PromptAbResult | PromptAbSweep
const SWEEP_OPTIONS = [
  { label: '当前路由', value: 'current' },
  { label: 'API 免费模型', value: 'api' },
  { label: '本地模型', value: 'local' },
  { label: 'API + 本地（分路标定）', value: 'api_local' },
]
const RUNS_OPTIONS = [
  { label: '单轮', value: 1 },
  { label: '3 轮聚合', value: 3 },
]

const nodeOptions = ref<Array<{ label: string; value: string }>>([])
const selectedNodeId = ref('')
const info = ref<PromptProfilesInfo | null>(null)
const loading = ref(false)
const runningId = ref('')
const promotingId = ref('')
const abResults = ref<Record<string, AbRun>>({})
const sweepMode = ref('current')
const runsMode = ref(1)

const editingId = ref('')
const editVersion = ref('')
const editSystem = ref('')
const editUser = ref('')
const staging = ref(false)
const showCases = ref(false)

async function loadNodes() {
  try {
    const nodes = await fetchModelNodes()
    nodeOptions.value = nodes
      .filter(n => n.kind === 'generative')
      .map(n => ({ label: n.name || n.id, value: n.id }))
  } catch {
    message.warning('功能节点清单加载失败')
  }
}

async function loadProfiles() {
  if (!selectedNodeId.value) return
  loading.value = true
  abResults.value = {}
  editingId.value = ''
  try {
    info.value = await fetchPromptProfiles(selectedNodeId.value)
  } catch (e) {
    info.value = null
    message.warning(`提示词概览加载失败：${errText(e)}`)
  } finally {
    loading.value = false
  }
}

function errText(e: unknown): string {
  const detail = (e as { detail?: unknown; message?: unknown }) ?? {}
  if (typeof detail.detail === 'string') return detail.detail
  if (typeof detail.message === 'string') return detail.message
  return String(e)
}

function sweepBackends(): string[] | undefined {
  if (sweepMode.value === 'api') return ['api']
  if (sweepMode.value === 'local') return ['local']
  if (sweepMode.value === 'api_local') return ['api', 'local']
  return undefined
}

async function startAb(promptId: string) {
  runningId.value = promptId
  try {
    const result = await runPromptAb(promptId, sweepBackends(), runsMode.value)
    abResults.value = { ...abResults.value, [promptId]: result }
    if (result.gate.passed) {
      message.success(`${promptId} A/B 跑分通过门禁`)
    } else if ((result as PromptAbResult).no_candidate_under_test) {
      message.info(`${promptId} 无 staged 候选，门禁不判定（仅基线测量）`)
    } else {
      message.warning(`${promptId} 未过门禁：${result.gate.reasons.join('；')}`)
    }
  } catch (e) {
    message.warning(`A/B 跑分失败：${errText(e)}`)
  } finally {
    runningId.value = ''
  }
}

interface AbRow {
  backend: string
  report: PromptAbResult['report']
  passed: boolean
  reasons: string[]
}

function abRows(result: AbRun): AbRow[] {
  if (isSweep(result)) {
    return result.sweeps.map(s => ({
      backend: s.backend,
      report: s.report,
      passed: s.gate.passed,
      reasons: s.gate.reasons,
    }))
  }
  return [{
    backend: result.backend,
    report: result.report,
    passed: result.gate.passed,
    reasons: result.gate.reasons,
  }]
}

function worstReport(result: AbRun): PromptAbResult['report'] | undefined {
  const rows = abRows(result)
  if (!rows.length) return undefined
  return rows.reduce((worst, row) => (
    row.report.candidate.schema_rate < worst.report.candidate.schema_rate ? row : worst
  ), rows[0]).report
}

async function promote(promptId: string) {
  const result = abResults.value[promptId]
  if (!result) return
  promotingId.value = promptId
  try {
    await promotePromptProfile(promptId, worstReport(result))
    message.success(`${promptId} 已晋级 production`)
    await loadProfiles()
  } catch (e) {
    message.warning(`晋级被拒绝：${errText(e)}`)
  } finally {
    promotingId.value = ''
  }
}

async function rollback(promptId: string) {
  try {
    await rollbackPromptProfile(promptId)
    message.success(`${promptId} 已回滚到上一版本`)
    await loadProfiles()
  } catch (e) {
    message.warning(`回滚失败：${errText(e)}`)
  }
}

function openStage(promptId: string) {
  editingId.value = promptId
  const current = info.value?.profiles.find(p => p.prompt_id === promptId)
  const base = current?.overridden ? current.version : '1.0.0'
  const minor = base.split('.')
  minor[2] = String(Number(minor[2] ?? 0) + 1)
  editVersion.value = minor.join('.')
  editSystem.value = ''
  editUser.value = ''
}

async function doStage() {
  if (!editingId.value) return
  if (!editVersion.value.trim() || !editUser.value.trim()) {
    message.warning('版本号与 user 模板必填')
    return
  }
  staging.value = true
  try {
    await stagePromptProfile(editingId.value, {
      version: editVersion.value.trim(),
      system_template: editSystem.value,
      user_template: editUser.value,
      output_schema: { type: 'string' },
    })
    message.success('候选版已进入 staging，先跑 A/B 再晋级')
    editingId.value = ''
    await loadProfiles()
  } catch (e) {
    message.warning(`staging 失败：${errText(e)}`)
  } finally {
    staging.value = false
  }
}

function pct(v: number): string {
  return `${Math.round(v * 100)}%`
}

onMounted(async () => {
  await loadNodes()
})
</script>

<template>
  <div class="prompt-profiles">
    <div class="pp-header">
      <div>
        <h3>提示词治理</h3>
        <p>每个生成节点的业务提示词在此版本化：修改先进 staging，用 golden cases 真实跑分对比基线，过门禁才能晋级，出问题一键回滚。</p>
      </div>
      <n-button :loading="loading" @click="loadProfiles">刷新</n-button>
    </div>

    <n-select
      v-model:value="selectedNodeId"
      :options="nodeOptions"
      placeholder="选择功能节点"
      filterable
      class="pp-node-select"
      @update:value="loadProfiles"
    />

    <template v-if="info && info.profiles.length">
      <section v-for="profile in info.profiles" :key="profile.prompt_id" class="pp-card">
        <div class="pp-head">
          <div class="pp-title">
            <span class="pp-id">{{ profile.prompt_id }}</span>
            <n-tag size="small" :type="profile.status === 'production' ? 'success' : 'warning'" :bordered="false">
              {{ profile.status }}
            </n-tag>
            <n-tag v-if="profile.overridden" size="small" type="info" :bordered="false">已覆盖</n-tag>
            <n-tag v-if="profile.staged" size="small" type="warning" :bordered="false">待晋级</n-tag>
          </div>
          <span class="pp-hash">v{{ profile.version }} · {{ profile.template_hash }}</span>
        </div>

        <div class="pp-actions">
          <n-button size="tiny" :loading="runningId === profile.prompt_id" @click="startAb(profile.prompt_id)">
            A/B 跑分
          </n-button>
          <n-select
            v-model:value="sweepMode" size="tiny"
            :options="SWEEP_OPTIONS" class="pp-sweep-select"
          />
          <n-select
            v-model:value="runsMode" size="tiny"
            :options="RUNS_OPTIONS" class="pp-runs-select"
          />
          <n-button
            size="tiny" type="primary" ghost
            :disabled="!abResults[profile.prompt_id]?.gate.passed"
            :loading="promotingId === profile.prompt_id"
            @click="promote(profile.prompt_id)"
          >
            晋级
          </n-button>
          <n-button size="tiny" :disabled="!profile.overridden" @click="rollback(profile.prompt_id)">回滚</n-button>
          <n-button size="tiny" @click="openStage(profile.prompt_id)">编辑候选版</n-button>
        </div>

        <div v-if="abResults[profile.prompt_id]" class="pp-ab">
          <div v-for="row in abRows(abResults[profile.prompt_id])" :key="row.backend" class="pp-rates">
            <span class="pp-backend">{{ row.backend }}</span>
            <span>基线 schema {{ pct(row.report.baseline.schema_rate) }} / 字面量 {{ pct(row.report.baseline.golden_rate) }}</span>
            <span>候选 schema {{ pct(row.report.candidate.schema_rate) }} / 字面量 {{ pct(row.report.candidate.golden_rate) }}</span>
            <span
              v-if="(abResults[profile.prompt_id] as PromptAbResult).per_run_candidate_all_ok?.length"
              class="pp-per-run"
            >逐轮：<span
              v-for="(ok, i) in (abResults[profile.prompt_id] as PromptAbResult).per_run_candidate_all_ok"
              :key="i" :class="ok ? 'pp-run-ok' : 'pp-run-bad'"
            >{{ ok ? '✓' : '✗' }}</span></span>
            <n-tag size="small" :type="row.passed ? 'success' : 'error'" :bordered="false">
              {{ row.passed ? '通过' : '未过' }}
            </n-tag>
          </div>
          <p v-if="abResults[profile.prompt_id].gate.reasons.some(r => r.includes('regressions'))" class="pp-warn">
            存在回归 case，详见门禁原因
          </p>
          <n-tag size="small" :type="abResults[profile.prompt_id].gate.passed ? 'success' : 'error'" :bordered="false">
            {{ abResults[profile.prompt_id].gate.passed ? '门禁通过' : '门禁未过' }}
          </n-tag>
          <ul v-if="abResults[profile.prompt_id].gate.reasons.length" class="pp-reasons">
            <li v-for="reason in abResults[profile.prompt_id].gate.reasons" :key="reason">{{ reason }}</li>
          </ul>
        </div>

        <div v-if="editingId === profile.prompt_id" class="pp-editor">
          <n-input v-model:value="editVersion" size="small" placeholder="候选版本号，如 1.0.1" />
          <n-input v-model:value="editSystem" size="small" type="text" placeholder="system 模板（可选，禁止插入变量）" />
          <n-input
            v-model:value="editUser" size="small" type="textarea"
            :autosize="{ minRows: 3, maxRows: 10 }"
            placeholder="user 模板，用 {变量名} 引用 golden case 变量"
          />
          <div class="pp-editor-row">
            <n-button size="tiny" type="primary" :loading="staging" @click="doStage">提交 staging</n-button>
            <n-button size="tiny" @click="editingId = ''">取消</n-button>
          </div>
        </div>
      </section>

      <div class="pp-cases-toggle">
        <n-button size="tiny" quaternary @click="showCases = !showCases">
          {{ showCases ? '收起' : '查看' }} golden cases（{{ info.golden_cases.length }}）
        </n-button>
      </div>
      <section v-if="showCases" class="pp-cases">
        <div v-for="c in info.golden_cases" :key="c.case_id" class="pp-case">
          <span class="pp-case-id">{{ c.case_id }}</span>
          <span class="pp-case-vars">{{ Object.entries(c.variables).map(([k, v]) => `${k}=${v}`).join(' | ') }}</span>
        </div>
      </section>
    </template>
    <n-empty
      v-else-if="info && !info.profiles.length"
      description="该节点没有可治理的生成提示词（编码/语音节点无业务 prompt）"
    />
    <n-empty v-else-if="!info" description="选择节点后展示其提示词 profile 与 golden cases" />
  </div>
</template>

<style scoped>
.prompt-profiles { display: flex; flex-direction: column; gap: 14px; }
.pp-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.pp-header h3 { margin: 0 0 4px; font-size: 16px; }
.pp-header p { margin: 0; font-size: 12px; color: var(--tx-3, #888); max-width: 560px; line-height: 1.6; }
.pp-node-select { max-width: 280px; }
.pp-card { border: 1px solid var(--bd-1, #e5e5e5); border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; }
.pp-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
.pp-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pp-id { font-weight: 600; font-size: 13px; }
.pp-hash { font-size: 11px; color: var(--tx-3, #999); font-family: monospace; }
.pp-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.pp-ab { background: var(--bg-2, #f7f7f8); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
.pp-rates { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
.pp-backend { font-family: monospace; font-weight: 600; min-width: 64px; }
.pp-sweep-select { width: 190px; }
.pp-runs-select { width: 110px; }
.pp-per-run { font-family: monospace; }
.pp-run-ok { color: #18a058; }
.pp-run-bad { color: #d03050; }
.pp-warn { margin: 0; color: #d03050; }
.pp-reasons { margin: 0; padding-left: 18px; color: #d03050; }
.pp-editor { display: flex; flex-direction: column; gap: 8px; }
.pp-editor-row { display: flex; gap: 8px; }
.pp-cases-toggle { display: flex; justify-content: flex-end; }
.pp-cases { display: flex; flex-direction: column; gap: 6px; }
.pp-case { font-size: 12px; display: flex; flex-direction: column; gap: 2px; padding: 6px 8px; background: var(--bg-2, #f7f7f8); border-radius: 6px; }
.pp-case-id { font-family: monospace; font-weight: 600; }
.pp-case-vars { color: var(--tx-3, #777); word-break: break-all; }
@media (max-width: 600px) { .pp-node-select { max-width: 100%; } }
</style>
