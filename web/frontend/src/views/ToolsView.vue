<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import {
  NButton, NSwitch, NModal, NInput, NInputNumber, NSelect, NTag, NPopconfirm, useMessage,
} from 'naive-ui'
import { get, put, post, del } from '../api'

const message = useMessage()
const tools = ref<any[]>([])
const search = ref('')
const categoryFilter = ref<string | null>(null)
const sourceFilter = ref<string | null>(null)

const showDebug = ref(false)
const debugTool = ref<any>(null)
const debugArgs = ref<Record<string, any>>({})
const debugResult = ref<any>(null)
const debugging = ref(false)

onMounted(load)

async function load() {
  try {
    tools.value = await get<any[]>('/tools')
  } catch (e: any) {
    message.error(e.message)
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
</script>

<template>
  <div class="tools-view">
    <div class="view-header">
      <h2>🛠 Skills 工具</h2>
      <span class="count">{{ filtered.length }} / {{ tools.length }}</span>
    </div>

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
        </div>
      </div>
    </div>

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
</style>
