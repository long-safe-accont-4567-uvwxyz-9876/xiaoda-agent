<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import {
  NButton, NSwitch, NModal, NInput, NInputNumber, NSelect, NTag, NPopconfirm, NSpin, NEmpty,
  NTabs, NTabPane, useMessage,
} from 'naive-ui'
import { get, put, post, del } from '../api'
import { t, tf } from '../i18n'

const message = useMessage()
const tools = ref<any[]>([])
const search = ref('')
const categoryFilter = ref<string | null>(null)
const sourceFilter = ref<string | null>(null)
const toolLimits = ref<any>(null)

const showDebug = ref(false)
const debugTool = ref<any>(null)
const debugArgs = ref<Record<string, any>>({})
const debugResult = ref<any>(null)
const debugging = ref(false)

const testingTool = ref<Record<string, boolean>>({})
const toolTestResult = ref<Record<string, any>>({})
const expandedTool = ref<string | null>(null)

onMounted(load)
onMounted(loadLimits)

// ── Tab 切换 ──────────────────────────────────────────────
const activeTab = ref('installed')

function onTabChange(name: string | number) {
  if (name === 'skillMarket' && skillMarketItems.value.length === 0) loadSkillMarket()
}

async function load() {
  try {
    tools.value = await get<any[]>('/tools')
  } catch (e: any) {
    message.error(e.message)
  }
}

async function loadLimits() {
  try {
    toolLimits.value = await get<any>('/tools/limits')
  } catch { /* 静默 */ }
}

async function testTool(name: string) {
  testingTool.value[name] = true
  toolTestResult.value[name] = null
  try {
    const res = await post<any>(`/tools/${name}/test`, {})
    toolTestResult.value[name] = res
    if (res.status === 'ok') {
      message.success(tf('toolsView.toolTestPassed', name, res.elapsed_ms))
    } else if (res.status === 'skip') {
      message.info(res.message)
    } else {
      message.error(tf('toolsView.toolTestFailed', name, res.error))
    }
  } catch (e: any) {
    toolTestResult.value[name] = { status: 'error', error: e.message }
    message.error(tf('toolsView.testFailed', e.message))
  } finally {
    testingTool.value[name] = false
  }
}

const categories = computed(() =>
  [...new Set(tools.value.map(t => t.category))].sort()
    .map(c => ({ label: c, value: c })))

const sources = computed(() =>
  [...new Set(tools.value.map(t => t.source))].sort()
    .map(s => ({ label: s, value: s })))

const filtered = computed(() =>
  tools.value.filter(t =>
    (!search.value || t.name.includes(search.value) || t.description.includes(search.value)) &&
    (!categoryFilter.value || t.category === categoryFilter.value) &&
    (!sourceFilter.value || t.source === sourceFilter.value)))

async function updateTool(tool: any, patch: Record<string, any>) {
  try {
    const data = await put(`/tools/${tool.name}`, patch)
    Object.assign(tool, data)
    message.success(tf('toolsView.toolUpdated', tool.name))
    // 刷新 LLM 可见性计数
    loadLimits()
  } catch (e: any) {
    message.error(e.message)
    await load()
  }
}

function openDebug(tool: any) {
  debugTool.value = tool
  debugResult.value = null
  const props = tool.schema?.properties || {}
  debugArgs.value = Object.fromEntries(
    Object.entries<any>(props).map(([k, v]) => [
      k, v.type === 'number' || v.type === 'integer' ? 0 : v.type === 'boolean' ? false : '',
    ]))
  showDebug.value = true
}

async function runDebug() {
  debugging.value = true
  debugResult.value = null
  try {
    const args = Object.fromEntries(
      Object.entries(debugArgs.value).filter(([, v]) => v !== '' && v !== null))
    debugResult.value = await post(`/tools/${debugTool.value.name}/invoke`, { args }, true)
  } catch (e: any) {
    debugResult.value = { success: false, error: e.message }
  } finally {
    debugging.value = false
  }
}

const permColor: Record<string, string> = {
  read_only: '#7fd650', read_write: '#e8d5a3', execute: '#d96a5f',
  RO: '#7fd650', RW: '#e8d5a3', E: '#d96a5f',
}

// ── Skills（SKILL.md）──────────────────────────────────────────
const skills = ref<any[]>([])
const skillInput = ref<HTMLInputElement | null>(null)
const showSkillEditor = ref(false)
const skillName = ref('')
const skillContent = ref('')
const skillIsCreate = ref(true)
const savingSkill = ref(false)

onMounted(loadSkills)
onMounted(loadSkillMarket)

async function loadSkills() {
  try {
    skills.value = await get<any[]>('/skills')
  } catch { /* 忽略 */ }
}

function uploadSkill(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  const reader = new FileReader()
  reader.onload = () => {
    // SKILL.md / my-skill.md → 文件名做 skill 名
    skillIsCreate.value = true
    skillName.value = file.name.replace(/\.md$/i, '').replace(/[^\w一-鿿-]/g, '_') || 'skill'
    skillContent.value = String(reader.result || '')
    showSkillEditor.value = true
    input.value = ''
  }
  reader.readAsText(file)
}

function openSkill(s: any | null) {
  skillIsCreate.value = !s
  skillName.value = s?.name || ''
  skillContent.value = ''
  if (s) {
    get<any>(`/skills/${s.name}`)
      .then(d => { skillContent.value = d.content })
      .catch((e: any) => message.error(e.message))
  }
  showSkillEditor.value = true
}

async function saveSkill() {
  if (!skillName.value.trim() || !skillContent.value.trim()) {
    message.error(t('toolsView.nameContentRequired'))
    return
  }
  savingSkill.value = true
  try {
    await put(`/skills/${skillName.value.trim()}`, { content: skillContent.value })
    message.success(tf('toolsView.skillSaved', skillName.value))
    showSkillEditor.value = false
    await loadSkills()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    savingSkill.value = false
  }
}

async function removeSkill(s: any) {
  try {
    await del(`/skills/${s.name}`, true)
    message.success(tf('toolsView.deleted', s.name))
    await loadSkills()
  } catch (e: any) {
    message.error(e.message)
  }
}

async function deleteFromEditor() {
  try {
    await del(`/skills/${skillName.value}`, true)
    message.success(tf('toolsView.deleted', skillName.value))
    showSkillEditor.value = false
    await loadSkills()
  } catch (e: any) {
    message.error(e.message)
  }
}

// ── 技能市场 ──────────────────────────────────────────────
const skillMarketItems = ref<any[]>([])
const skillMarketLoading = ref(false)
const skillMarketSearch = ref('')
const installingSkill = ref<Record<string, boolean>>({})
const uninstallingSkill = ref<Record<string, boolean>>({})
const testingSkill = ref<Record<string, boolean>>({})
const skillTestResult = ref<Record<string, any>>({})

const filteredSkillMarket = computed(() => {
  if (!skillMarketSearch.value.trim()) return skillMarketItems.value
  const q = skillMarketSearch.value.toLowerCase()
  return skillMarketItems.value.filter((i: any) =>
    i.name.toLowerCase().includes(q) ||
    i.description.toLowerCase().includes(q) ||
    (i.tags || []).some((t: string) => t.toLowerCase().includes(q))
  )
})

async function loadSkillMarket(force = false) {
  skillMarketLoading.value = true
  try {
    const data = await get<any>(`/market/skills${force ? '?force=true' : ''}`)
    skillMarketItems.value = data.items || []
  } catch { /* 静默失败 */ } finally {
    skillMarketLoading.value = false
  }
}

async function installSkillFromMarket(item: any) {
  installingSkill.value[item.id] = true
  try {
    await post('/market/skills/install', {
      item_id: item.id,
      download_url: item.download_url,
      version: item.version,
      sha256: item.sha256,
    })
    message.success(tf('toolsView.skillInstallSuccess', item.name))
    await loadSkillMarket()
    await loadSkills()
  } catch (e: any) {
    message.error(tf('toolsView.installFailed', e.message))
  } finally {
    installingSkill.value[item.id] = false
  }
}

async function uninstallSkillFromMarket(item: any) {
  uninstallingSkill.value[item.id] = true
  try {
    await post('/market/skills/uninstall', { item_id: item.id })
    message.success(tf('toolsView.skillUninstalled', item.name))
    await loadSkillMarket()
    await loadSkills()
  } catch (e: any) {
    message.error(tf('toolsView.uninstallFailed', e.message))
  } finally {
    uninstallingSkill.value[item.id] = false
  }
}

async function testSkill(item: any) {
  testingSkill.value[item.id] = true
  skillTestResult.value[item.id] = null
  try {
    // 验证技能文件是否存在且内容有效
    const skillList = await get<any[]>('/skills')
    const found = skillList.find((s: any) => s.name === item.id)
    if (found && found.size > 20) {
      skillTestResult.value[item.id] = { ok: true, message: `${t('toolsView.fileValid')} (${(found.size/1024).toFixed(1)}KB)` }
      message.success(tf('toolsView.skillVerified', item.name))
    } else if (found) {
      skillTestResult.value[item.id] = { ok: false, message: t('toolsView.fileTooShort') }
      message.warning(tf('toolsView.skillTooShort', item.name))
    } else {
      skillTestResult.value[item.id] = { ok: false, message: t('toolsView.fileNotFound') }
      message.error(tf('toolsView.skillFileNotFound', item.name))
    }
  } catch (e: any) {
    skillTestResult.value[item.id] = { ok: false, message: e.message }
    message.error(tf('toolsView.verifyFailed', e.message))
  } finally {
    testingSkill.value[item.id] = false
  }
}
</script>

<template>
  <div class="tools-view">
    <div class="view-header">
      <h2>🛠 {{ t('toolsView.title') }}</h2>
      <span class="count">{{ t('toolsView.total') }} {{ tools.length }} {{ t('toolsView.toolsUnit') }}</span>
      <span v-if="search || categoryFilter || sourceFilter" class="count">
        （{{ t('toolsView.filterLabel') }}: {{ filtered.length }}）
      </span>
      <n-tag v-if="toolLimits" size="small"
             :type="toolLimits.enabled >= toolLimits.max_enabled ? 'error' : 'success'"
             :bordered="false">
        {{ t('toolsView.llmVisible') }} {{ toolLimits.enabled }}/{{ toolLimits.max_enabled }}
      </n-tag>
      <n-tag v-if="toolLimits && toolLimits.remaining <= 5" size="small" type="warning" :bordered="false">
        {{ t('toolsView.quotaRemaining') }} {{ toolLimits.remaining }} {{ t('toolsView.quotaUnit') }}
      </n-tag>
    </div>

    <n-tabs v-model:value="activeTab" type="line" @update:value="onTabChange">
      <!-- ── 已安装 ──────────────────────────────────────── -->
      <n-tab-pane name="installed" :tab="t('installed')">
        <div class="skills-section glass-panel">
          <div class="skills-head">
            <span class="skills-title">📜 Skills（{{ t('toolsView.skillHint') }}）</span>
            <div style="display:flex; gap:8px">
              <n-button size="small" type="primary" @click="skillInput?.click()">⬆ {{ t('toolsView.uploadSkill') }}</n-button>
              <n-button size="small" @click="openSkill(null)">＋ {{ t('toolsView.createNew') }}</n-button>
              <input ref="skillInput" type="file" accept=".md,text/markdown"
                     style="display:none" @change="uploadSkill" />
            </div>
          </div>
          <div v-if="skills.length" class="skills-list">
            <div v-for="s in skills" :key="s.name" class="skill-chip">
              <span class="skill-name" @click="openSkill(s)" style="cursor:pointer;flex:1">{{ s.name }}</span>
              <span class="skill-size">{{ (s.size / 1024).toFixed(1) }}KB</span>
            </div>
          </div>
          <p v-else class="skills-empty">{{ t('toolsView.emptyHint') }}</p>
        </div>

        <div class="filters glass-panel">
          <n-input v-model:value="search" :placeholder="t('toolsView.searchPlaceholder')" clearable style="max-width: 260px" />
          <n-select v-model:value="categoryFilter" :options="categories" :placeholder="t('toolsView.categoryPh')" clearable style="max-width: 160px" />
          <n-select v-model:value="sourceFilter" :options="sources" :placeholder="t('toolsView.sourcePh')" clearable style="max-width: 160px" />
        </div>

        <div class="tool-list">
          <div v-for="tool in filtered" :key="tool.name" class="tool-row glass-panel"
               :class="{ disabled: !tool.enabled }">
            <span class="perm-dot" :style="{ background: permColor[tool.permission] || '#9ca3af' }"
                  :title="`${t('toolsView.permLevel')} ${tool.permission}`"></span>
            <div class="tool-main">
              <div class="tool-title">
                <span class="tool-name">{{ tool.name }}</span>
                <n-tag size="tiny" :bordered="false">{{ tool.category }}</n-tag>
                <n-tag v-if="tool.source !== 'builtin'" size="tiny" type="info" :bordered="false">{{ tool.source }}</n-tag>
              </div>
              <div class="tool-desc">{{ tool.description }}</div>
              <!-- 展开的高级控件 -->
              <div v-if="expandedTool === tool.name" class="tool-advanced">
                <label class="ctl">
                  {{ t('toolsView.frequency') }}
                  <n-input-number :value="tool.max_frequency" size="tiny" :min="0" :max="6000"
                                  :show-button="false" style="width: 64px"
                                  @update:value="(v: number | null) => v !== null && updateTool(tool, { max_frequency: v })" />
                </label>
                <label class="ctl">
                  {{ t('toolsView.needConfirm') }}
                  <n-switch :value="tool.requires_confirmation" size="small"
                            @update:value="(v: boolean) => updateTool(tool, { requires_confirmation: v })" />
                </label>
                <n-button size="tiny" @click="openDebug(tool)">{{ t('toolsView.debug') }}</n-button>
                <n-button size="tiny" :loading="testingTool[tool.name]"
                          :type="toolTestResult[tool.name]?.status === 'ok' ? 'success' :
                                 toolTestResult[tool.name]?.status === 'fail' ? 'error' : 'default'"
                          @click="testTool(tool.name)">
                  {{ toolTestResult[tool.name]?.status === 'ok' ? t('toolsView.pass') :
                     toolTestResult[tool.name]?.status === 'fail' ? t('toolsView.fail') : t('toolsView.test') }}
                </n-button>
                <n-button size="tiny" @click="openDebug(tool)">{{ t('toolsView.edit') }}</n-button>
                <n-button size="tiny" type="error" @click="updateTool(tool, { enabled: false })">{{ t('toolsView.delete') }}</n-button>
              </div>
            </div>
            <div class="tool-actions">
              <n-button size="tiny" quaternary :type="expandedTool === tool.name ? 'primary' : 'default'"
                        @click="expandedTool = expandedTool === tool.name ? null : tool.name">
                {{ expandedTool === tool.name ? t('toolsView.collapse') : '...' }}
              </n-button>
              <n-switch :value="tool.enabled" size="small"
                        @update:value="(v: boolean) => updateTool(tool, { enabled: v })" />
            </div>
          </div>
        </div>
      </n-tab-pane>

      <!-- ── 技能市场 ──────────────────────────────────────── -->
      <n-tab-pane name="skillMarket" :tab="t('toolsView.market')">
        <div class="market-toolbar">
          <n-input v-model:value="skillMarketSearch" :placeholder="t('toolsView.searchMarket')" clearable
                   size="small" style="width: 200px" />
          <n-button size="small" :loading="skillMarketLoading" @click="loadSkillMarket(true)">{{ t('refresh') }}</n-button>
        </div>
        <p class="market-hint">{{ t('toolsView.browseInstall') }}</p>

        <n-spin :show="skillMarketLoading">
          <div class="market-grid">
            <div v-for="item in filteredSkillMarket" :key="item.id"
                 class="market-card glass-panel glass-panel-hover">
              <div class="card-head">
                <span class="card-icon">{{ item.icon || '📝' }}</span>
                <div class="card-title-group">
                  <span class="card-name">{{ item.name }}</span>
                  <div class="card-meta">
                    <span class="card-version">v{{ item.version }}</span>
                    <span v-if="item.author" class="card-author">{{ item.author }}</span>
                  </div>
                </div>
              </div>
              <div class="card-desc">{{ item.description }}</div>
              <div v-if="item.tags?.length" class="card-tags">
                <n-tag v-for="tag in item.tags" :key="tag" size="tiny" :bordered="false" round>{{ tag }}</n-tag>
              </div>
              <div class="card-footer">
                <n-tag v-if="item.installed" size="tiny" type="success" :bordered="false">
                  {{ t('toolsView.installedVersion') }} v{{ item.installed_version }}
                </n-tag>
                <span v-else></span>
                <div class="card-actions">
                  <n-button v-if="item.installed" size="tiny"
                            :loading="testingSkill[item.id]"
                            :type="skillTestResult[item.id]?.ok ? 'success' : 'default'"
                            @click="testSkill(item)">
                    {{ skillTestResult[item.id]?.ok ? t('toolsView.pass') : t('toolsView.test') }}
                  </n-button>
                  <n-popconfirm v-if="item.installed"
                                @positive-click="uninstallSkillFromMarket(item)">
                    <template #trigger>
                      <n-button size="tiny" type="error" quaternary
                                :loading="uninstallingSkill[item.id]">{{ t('uninstall') }}</n-button>
                    </template>
                    {{ t('toolsView.confirmUninstall') }}「{{ item.name }}」？
                  </n-popconfirm>
                  <n-button size="tiny" type="primary"
                            :loading="installingSkill[item.id]"
                            @click="installSkillFromMarket(item)">
                    {{ item.installed ? t('update') : t('install') }}
                  </n-button>
                </div>
              </div>
            </div>
            <n-empty v-if="!skillMarketLoading && filteredSkillMarket.length === 0"
                     :description="t('toolsView.marketEmpty')" class="empty-state" />
          </div>
        </n-spin>
      </n-tab-pane>
    </n-tabs>

    <n-modal v-model:show="showDebug" preset="card" :title="`${t('toolsView.debugExecute')} · ${debugTool?.name}`"
             style="width: min(640px, 94vw); max-height: 85vh; overflow-y: auto">
      <div class="debug-warning">{{ t('toolsView.debugWarning') }}</div>
      <div v-for="(v, k) in debugArgs" :key="k" class="debug-field">
        <label class="debug-label mono">{{ k }}</label>
        <n-input v-if="typeof debugArgs[k] === 'string'" v-model:value="debugArgs[k]"
                 :placeholder="debugTool?.schema?.properties?.[k]?.description || ''" />
        <n-input-number v-else-if="typeof debugArgs[k] === 'number'" v-model:value="debugArgs[k]" style="width:100%" />
        <n-switch v-else v-model:value="debugArgs[k]" />
      </div>
      <n-button type="warning" :loading="debugging" style="margin-top: 12px" @click="runDebug">
        {{ t('toolsView.execute') }}
      </n-button>
      <div v-if="debugResult" class="debug-result glass-panel" :class="{ failed: !debugResult.success }">
        <div>{{ debugResult.success ? t('toolsView.success') : t('toolsView.fail') }}
          <span v-if="debugResult.elapsed_ms"> · {{ debugResult.elapsed_ms }}ms</span></div>
        <pre class="debug-output">{{ debugResult.data || debugResult.error }}</pre>
      </div>
    </n-modal>

    <n-modal v-model:show="showSkillEditor" preset="card"
             :title="skillIsCreate ? t('toolsView.newSkill') : `${t('toolsView.editSkill')} · ${skillName}`"
             style="width: min(720px, 94vw); max-height: 88vh; overflow-y: auto">
      <div class="skill-form">
        <n-input v-if="skillIsCreate" v-model:value="skillName"
                 :placeholder="t('toolsView.skillNamePh')" style="margin-bottom: 10px" />
        <n-input v-model:value="skillContent" type="textarea" :rows="16"
                 :placeholder="t('toolsView.skillContentPh')" />
      </div>
      <template #footer>
        <div style="display:flex; justify-content:space-between; align-items:center">
          <n-popconfirm v-if="!skillIsCreate" @positive-click="deleteFromEditor">
            <template #trigger>
              <n-button type="error" quaternary>{{ t('toolsView.deleteSkill') }}</n-button>
            </template>
            {{ t('toolsView.deleteSkillConfirm') }}「{{ skillName }}」？
          </n-popconfirm>
          <span v-else></span>
          <div style="display:flex; gap:10px">
            <n-button @click="showSkillEditor = false">{{ t('cancel') }}</n-button>
            <n-button type="primary" :loading="savingSkill" @click="saveSkill">{{ t('toolsView.saveNextMsg') }}</n-button>
          </div>
        </div>
      </template>
    </n-modal>
  </div>
</template>

<style scoped>
.view-header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 14px; }
.view-header h2 { font-family: 'Noto Serif SC', serif; }
.count { color: var(--moon-dim); font-size: 13px; }

.filters {
  display: flex; gap: 10px; padding: 12px 14px; margin-bottom: 12px;
  flex-wrap: wrap;
}

.skills-section { padding: 12px 14px; margin-bottom: 12px; }
.skills-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap;
}
.skills-title { font-weight: 600; color: var(--dendro); font-size: 14px; }
.skills-list { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
.skill-chip {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 10px; border-radius: 14px;
  background: rgba(127, 214, 80, 0.1);
  border: 1px solid var(--glass-border);
  transition: border-color 0.2s;
}
.skill-chip:hover { border-color: rgba(127, 214, 80, 0.45); }
.skill-name { font-size: 13px; font-weight: 600; }
.skill-size { font-size: 11px; color: var(--moon-dim); }
.skill-btn { font-size: 12px; padding: 2px 8px; border-radius: 6px; cursor: pointer; background: rgba(127,214,80,0.15); color: #7fd650; border: 1px solid rgba(127,214,80,0.3); user-select: none; }
.skill-btn:hover { background: rgba(127,214,80,0.3); }
.skill-btn-del { background: rgba(217,106,95,0.15); color: #d96a5f; border-color: rgba(217,106,95,0.3); }
.skill-btn-del:hover { background: rgba(217,106,95,0.3); }
.skills-empty { font-size: 12.5px; color: var(--moon-dim); margin-top: 8px; }

.tool-list { display: flex; flex-direction: column; gap: 8px; }

.tool-row {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 14px;
  transition: opacity 0.2s;
}
.tool-row.disabled { opacity: 0.45; }

.perm-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; cursor: help; }

.tool-main { flex: 1; min-width: 0; }
.tool-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tool-name { font-family: 'JetBrains Mono', monospace; font-size: 13.5px; color: var(--moon); }
.tool-desc {
  font-size: 12px; color: var(--moon-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  margin-top: 2px;
}

.tool-controls { display: flex; align-items: center; gap: 14px; flex-shrink: 0; }
.tool-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.tool-advanced {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-top: 8px; padding-top: 8px;
  border-top: 1px solid var(--glass-border);
}
.ctl { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--moon-dim); }

.debug-warning {
  background: rgba(217, 106, 95, 0.12);
  border: 1px solid rgba(217, 106, 95, 0.3);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
  color: var(--alert);
  margin-bottom: 14px;
}

.debug-field { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.debug-label { min-width: 120px; font-size: 13px; }
.mono { font-family: 'JetBrains Mono', monospace; }

.debug-result { margin-top: 14px; padding: 12px; border-color: rgba(127, 214, 80, 0.4); }
.debug-result.failed { border-color: var(--alert); }
.debug-output {
  margin-top: 8px; font-size: 12px; white-space: pre-wrap; word-break: break-all;
  max-height: 280px; overflow-y: auto;
  font-family: 'JetBrains Mono', monospace; color: var(--moon-dim);
}

@media (max-width: 768px) {
  .tool-row { flex-wrap: wrap; }
  .tool-controls { width: 100%; justify-content: flex-end; }
}

/* ── 市场通用 ─────────────────────────────────────────── */
.market-toolbar {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 8px; padding-top: 4px;
}
.market-hint { font-size: 12.5px; color: var(--moon-dim); margin-bottom: 12px; }

.market-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}
.market-card { padding: 12px 14px; }
.card-head { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 6px; }
.card-icon { font-size: 24px; flex-shrink: 0; line-height: 1; }
.card-title-group { flex: 1; min-width: 0; }
.card-name { font-weight: 600; font-size: 14px; display: block; }
.card-meta { display: flex; align-items: center; gap: 6px; margin-top: 1px; }
.card-version { font-size: 11px; color: var(--moon-dim); }
.card-author { font-size: 11px; color: var(--moon-dim); }
.card-desc {
  font-size: 12.5px; color: var(--moon-dim); margin-bottom: 6px;
  display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden;
}
.card-tags { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 6px; }
.card-footer { display: flex; align-items: center; justify-content: space-between; }
.card-actions { display: flex; gap: 6px; }
.empty-state { grid-column: 1 / -1; padding: 40px 0; }
</style>
