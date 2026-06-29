<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import {
  NButton, NSwitch, NModal, NInput, NInputNumber, NSelect, NTag, NPopconfirm, NSpin, NEmpty,
  NTabs, NTabPane, useMessage,
} from 'naive-ui'
import { get, put, post, del } from '../api'

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
      message.success(`工具「${name}」测试通过 (${res.elapsed_ms}ms)`)
    } else if (res.status === 'skip') {
      message.info(res.message)
    } else {
      message.error(`工具「${name}」测试失败: ${res.error}`)
    }
  } catch (e: any) {
    toolTestResult.value[name] = { status: 'error', error: e.message }
    message.error(`测试失败: ` + e.message)
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
    message.success(`${tool.name} 已更新，即时生效 ✓`)
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
    message.error('名称与内容均不能为空')
    return
  }
  savingSkill.value = true
  try {
    await put(`/skills/${skillName.value.trim()}`, { content: skillContent.value })
    message.success(`Skill「${skillName.value}」已保存，下一条消息生效 ✓`)
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
    message.success(`已删除 ${s.name}`)
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
    message.success(`技能「${item.name}」安装成功`)
    await loadSkillMarket()
    await loadSkills()
  } catch (e: any) {
    message.error('安装失败: ' + e.message)
  } finally {
    installingSkill.value[item.id] = false
  }
}

async function uninstallSkillFromMarket(item: any) {
  uninstallingSkill.value[item.id] = true
  try {
    await post('/market/skills/uninstall', { item_id: item.id })
    message.success(`技能「${item.name}」已卸载`)
    await loadSkillMarket()
    await loadSkills()
  } catch (e: any) {
    message.error('卸载失败: ' + e.message)
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
      skillTestResult.value[item.id] = { ok: true, message: `文件有效 (${(found.size/1024).toFixed(1)}KB)` }
      message.success(`技能「${item.name}」验证通过`)
    } else if (found) {
      skillTestResult.value[item.id] = { ok: false, message: '文件内容过短' }
      message.warning(`技能「${item.name}」内容过短`)
    } else {
      skillTestResult.value[item.id] = { ok: false, message: '技能文件未找到' }
      message.error(`技能「${item.name}」文件不存在`)
    }
  } catch (e: any) {
    skillTestResult.value[item.id] = { ok: false, message: e.message }
    message.error(`验证失败: ` + e.message)
  } finally {
    testingSkill.value[item.id] = false
  }
}
</script>

<template>
  <div class="tools-view">
    <div class="view-header">
      <h2>🛠 Skills 工具</h2>
      <span class="count">{{ filtered.length }} / {{ tools.length }}</span>
      <n-tag v-if="toolLimits" size="small"
             :type="toolLimits.enabled >= toolLimits.max_enabled ? 'error' : 'success'"
             :bordered="false">
        LLM 可见 {{ toolLimits.enabled }}/{{ toolLimits.max_enabled }}
      </n-tag>
      <n-tag v-if="toolLimits && toolLimits.remaining <= 5" size="small" type="warning" :bordered="false">
        仅剩 {{ toolLimits.remaining }} 个配额
      </n-tag>
    </div>

    <n-tabs v-model:value="activeTab" type="line" @update:value="onTabChange">
      <!-- ── 已安装 ──────────────────────────────────────── -->
      <n-tab-pane name="installed" tab="已安装">
        <div class="skills-section glass-panel">
          <div class="skills-head">
            <span class="skills-title">📜 Skills（SKILL.md 知识注入）</span>
            <div style="display:flex; gap:8px">
              <n-button size="small" type="primary" @click="skillInput?.click()">⬆ 上传 SKILL.md</n-button>
              <n-button size="small" @click="openSkill(null)">＋ 手写新建</n-button>
              <input ref="skillInput" type="file" accept=".md,text/markdown"
                     style="display:none" @change="uploadSkill" />
            </div>
          </div>
          <div v-if="skills.length" class="skills-list">
            <div v-for="s in skills" :key="s.name" class="skill-chip" @click="openSkill(s)">
              <span class="skill-name">{{ s.name }}</span>
              <span class="skill-size">{{ (s.size / 1024).toFixed(1) }}KB</span>
              <n-popconfirm @positive-click="removeSkill(s)">
                <template #trigger>
                  <button class="skill-del" @click.stop title="删除">✕</button>
                </template>
                删除 Skill「{{ s.name }}」？
              </n-popconfirm>
            </div>
          </div>
          <p v-else class="skills-empty">还没有 Skill。上传 SKILL.md 后其内容会注入系统提示词，助手下一条消息即掌握该技能。</p>
        </div>

        <div class="filters glass-panel">
          <n-input v-model:value="search" placeholder="搜索工具名/描述…" clearable style="max-width: 260px" />
          <n-select v-model:value="categoryFilter" :options="categories" placeholder="分类" clearable style="max-width: 160px" />
          <n-select v-model:value="sourceFilter" :options="sources" placeholder="来源" clearable style="max-width: 160px" />
        </div>

        <div class="tool-list">
          <div v-for="t in filtered" :key="t.name" class="tool-row glass-panel"
               :class="{ disabled: !t.enabled }">
            <span class="perm-dot" :style="{ background: permColor[t.permission] || '#9ca3af' }"
                  :title="`权限等级 ${t.permission}`"></span>
            <div class="tool-main">
              <div class="tool-title">
                <span class="tool-name">{{ t.name }}</span>
                <n-tag size="tiny" :bordered="false">{{ t.category }}</n-tag>
                <n-tag v-if="t.source !== 'builtin'" size="tiny" type="info" :bordered="false">{{ t.source }}</n-tag>
              </div>
              <div class="tool-desc">{{ t.description }}</div>
            </div>
            <div class="tool-controls">
              <label class="ctl">
                频率
                <n-input-number :value="t.max_frequency" size="tiny" :min="0" :max="6000"
                                :show-button="false" style="width: 64px"
                                @update:value="(v: number | null) => v !== null && updateTool(t, { max_frequency: v })" />
              </label>
              <label class="ctl">
                需确认
                <n-switch :value="t.requires_confirmation" size="small"
                          @update:value="(v: boolean) => updateTool(t, { requires_confirmation: v })" />
              </label>
              <label class="ctl">
                启用
                <n-switch :value="t.enabled" size="small"
                          @update:value="(v: boolean) => updateTool(t, { enabled: v })" />
              </label>
              <n-button size="tiny" @click="openDebug(t)">调试</n-button>
              <n-button size="tiny" :loading="testingTool[t.name]"
                        :type="toolTestResult[t.name]?.status === 'ok' ? 'success' :
                               toolTestResult[t.name]?.status === 'fail' ? 'error' : 'default'"
                        @click="testTool(t.name)">
                {{ toolTestResult[t.name]?.status === 'ok' ? '✓ 通过' :
                   toolTestResult[t.name]?.status === 'fail' ? '✕ 失败' : '测试' }}
              </n-button>
            </div>
          </div>
        </div>
      </n-tab-pane>

      <!-- ── 技能市场 ──────────────────────────────────────── -->
      <n-tab-pane name="skillMarket" tab="技能市场">
        <div class="market-toolbar">
          <n-input v-model:value="skillMarketSearch" placeholder="搜索技能..." clearable
                   size="small" style="width: 200px" />
          <n-button size="small" :loading="skillMarketLoading" @click="loadSkillMarket(true)">刷新</n-button>
        </div>
        <p class="market-hint">浏览并一键安装社区公开技能，安装后立即生效（注入系统提示词）。</p>

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
                  已安装 v{{ item.installed_version }}
                </n-tag>
                <span v-else></span>
                <div class="card-actions">
                  <n-button v-if="item.installed" size="tiny"
                            :loading="testingSkill[item.id]"
                            :type="skillTestResult[item.id]?.ok ? 'success' : 'default'"
                            @click="testSkill(item)">
                    {{ skillTestResult[item.id]?.ok ? '✓ 通过' : '测试' }}
                  </n-button>
                  <n-popconfirm v-if="item.installed"
                                @positive-click="uninstallSkillFromMarket(item)">
                    <template #trigger>
                      <n-button size="tiny" type="error" quaternary
                                :loading="uninstallingSkill[item.id]">卸载</n-button>
                    </template>
                    确认卸载「{{ item.name }}」？
                  </n-popconfirm>
                  <n-button size="tiny" type="primary"
                            :loading="installingSkill[item.id]"
                            @click="installSkillFromMarket(item)">
                    {{ item.installed ? '更新' : '安装' }}
                  </n-button>
                </div>
              </div>
            </div>
            <n-empty v-if="!skillMarketLoading && filteredSkillMarket.length === 0"
                     description="暂无可安装的技能" class="empty-state" />
          </div>
        </n-spin>
      </n-tab-pane>
    </n-tabs>

    <n-modal v-model:show="showDebug" preset="card" :title="`调试执行 · ${debugTool?.name}`"
             style="width: min(640px, 94vw); max-height: 85vh; overflow-y: auto">
      <div class="debug-warning">⚠ 将真实执行该工具，操作会写入审计日志</div>
      <div v-for="(v, k) in debugArgs" :key="k" class="debug-field">
        <label class="debug-label mono">{{ k }}</label>
        <n-input v-if="typeof debugArgs[k] === 'string'" v-model:value="debugArgs[k]"
                 :placeholder="debugTool?.schema?.properties?.[k]?.description || ''" />
        <n-input-number v-else-if="typeof debugArgs[k] === 'number'" v-model:value="debugArgs[k]" style="width:100%" />
        <n-switch v-else v-model:value="debugArgs[k]" />
      </div>
      <n-button type="warning" :loading="debugging" style="margin-top: 12px" @click="runDebug">
        执行
      </n-button>
      <div v-if="debugResult" class="debug-result glass-panel" :class="{ failed: !debugResult.success }">
        <div>{{ debugResult.success ? '✓ 成功' : '✗ 失败' }}
          <span v-if="debugResult.elapsed_ms"> · {{ debugResult.elapsed_ms }}ms</span></div>
        <pre class="debug-output">{{ debugResult.data || debugResult.error }}</pre>
      </div>
    </n-modal>

    <n-modal v-model:show="showSkillEditor" preset="card"
             :title="skillIsCreate ? '新建 Skill' : `编辑 Skill · ${skillName}`"
             style="width: min(720px, 94vw); max-height: 88vh; overflow-y: auto">
      <div class="skill-form">
        <n-input v-if="skillIsCreate" v-model:value="skillName"
                 placeholder="skill 名称（字母/数字/下划线/中文）" style="margin-bottom: 10px" />
        <n-input v-model:value="skillContent" type="textarea" :rows="16"
                 placeholder="SKILL.md 全文（Markdown）——描述这项技能的知识、步骤、注意事项" />
      </div>
      <template #footer>
        <div style="display:flex; justify-content:flex-end; gap:10px">
          <n-button @click="showSkillEditor = false">取消</n-button>
          <n-button type="primary" :loading="savingSkill" @click="saveSkill">保存（下一条消息生效）</n-button>
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
  display: flex; align-items: center; gap: 8px;
  padding: 5px 10px; border-radius: 14px; cursor: pointer;
  background: rgba(127, 214, 80, 0.1);
  border: 1px solid var(--glass-border);
  transition: border-color 0.2s;
}
.skill-chip:hover { border-color: rgba(127, 214, 80, 0.45); }
.skill-name { font-size: 13px; font-weight: 600; }
.skill-size { font-size: 11px; color: var(--moon-dim); }
.skill-del {
  background: none; border: none; color: var(--moon-dim);
  cursor: pointer; font-size: 12px; padding: 0 2px;
}
.skill-del:hover { color: var(--alert); }
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
