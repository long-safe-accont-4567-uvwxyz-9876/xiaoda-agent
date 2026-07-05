<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import {
  NButton, NSwitch, NInput, NSelect, NTabs, NTabPane, NTag,
  NPopconfirm, NProgress, useMessage,
} from 'naive-ui'
import { get, post, put, del, api } from '../api'
import { getWsClient } from '../api/ws'
import { useUiStore } from '../stores/ui'
import { t } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'

const message = useMessage()
const ui = useUiStore()
const ws = getWsClient()

// TTS
const ttsText = ref('')
const ttsAgent = ref('nahida')  // 选择哪个 agent 来合成
const ttsStyle = ref<string | null>(null)
const voiceGroups = ref<Record<string, Array<{ name: string; voice_ref: string }>>>({})
const styles = ref<Array<{ label: string; value: string }>>([])
const ttsResult = ref('')
const ttsLoading = ref(false)

// 参考音频管理
const voiceUploading = ref(false)
const voiceInputEl = ref<HTMLInputElement | null>(null)
const selectedAgent = ref<string>('')  // 当前选中的 agent
// agent 列表（从 /agents 加载）
const agentList = ref<Array<{ name: string; display_name: string; voice_ref: string | null }>>([])

// 图/视频
const imagePrompt = ref('')
const videoPrompt = ref('')
const submitting = ref('')
const tasks = ref<any[]>([])

// 画廊
const galleryType = ref<'image' | 'video' | 'audio'>('image')
const gallery = ref<any[]>([])

onMounted(async () => {
  try {
    const agents = await get<any[]>('/agents')
    agentList.value = agents.map(a => ({ name: a.name, display_name: a.display_name || a.name, voice_ref: a.voice_ref }))
    if (agentList.value.length) selectedAgent.value = agentList.value[0].name
  } catch { /* */ }
  await loadVoices()
  try {
    const cfg = await get('/media/tts/config')
    ui.autoSpeak = cfg.auto_speak
    // 默认选择 nahida agent（其 voice_ref 由管理区设置）
    if (!ttsAgent.value || !agentList.value.find(a => a.name === ttsAgent.value)) {
      ttsAgent.value = agentList.value[0]?.name || 'nahida'
    }
  } catch { /* TTS 可能未配置 */ }
  loadTasks()
  loadGallery()
  ws.on('media_task_update', onTaskUpdate)
})

onBeforeUnmount(() => ws.off('media_task_update', onTaskUpdate))

function onTaskUpdate(e: any) {
  const t = tasks.value.find(x => x.id === e.task_id)
  if (t) {
    t.status = e.status
    t.progress = e.progress
    if (e.result_url) t.result_path = e.result_url
    if (e.error) t.error = e.error
  } else {
    loadTasks()
  }
  if (e.status === 'done') {
    message.success(t('mediaView.genDone'))
    loadGallery()
  }
  if (e.status === 'failed' && e.error) message.error(`${t('mediaView.genFailed')}：${e.error}`)
}

async function synthesize() {
  if (!ttsText.value.trim()) return
  // 查找所选 agent 的 voice_ref
  const agent = agentList.value.find(a => a.name === ttsAgent.value)
  const voiceRef = agent?.voice_ref
  if (!voiceRef) {
    message.error(t('mediaView.noVoiceForAgent'))
    return
  }
  ttsLoading.value = true
  try {
    const r = await post('/media/tts', {
      text: ttsText.value, voice: voiceRef, style: ttsStyle.value || '',
    })
    ttsResult.value = r.audio_url
    if (r.cached) message.info(t('mediaView.cacheHit'))
    loadGallery()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    ttsLoading.value = false
  }
}

async function setAutoSpeak(v: boolean) {
  try {
    await ui.setAutoSpeak(v)
    message.success(`自动朗读已${v ? t('mediaView.autoSpeakOn') : t('mediaView.autoSpeakOff')} ✓`)
  } catch (e: any) { message.error(e.message) }
}

async function loadVoices() {
  try {
    const v = await get('/media/tts/voices')
    voiceGroups.value = v.groups || {}
    styles.value = v.styles.map((s: string) => ({ label: s, value: s }))
  } catch { /* */ }
}

function onVoiceFilePick(e: Event) {
  const target = e.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file || !selectedAgent.value) return
  const name = file.name.replace(/\.[^.]+$/, '')
  voiceUploading.value = true
  const formData = new FormData()
  formData.append('name', name)
  formData.append('file', file)
  api.uploadVoiceRef(selectedAgent.value, formData).then(async () => {
    message.success(t('mediaView.voiceUploaded'))
    if (voiceInputEl.value) voiceInputEl.value.value = ''
    await loadVoices()
    await reloadAgentVoiceRef()
  }).catch((err: any) => {
    message.error(err.message)
  }).finally(() => {
    voiceUploading.value = false
  })
}

async function reloadAgentVoiceRef() {
  try {
    const agents = await get<any[]>('/agents')
    agentList.value = agents.map(a => ({ name: a.name, display_name: a.display_name || a.name, voice_ref: a.voice_ref }))
  } catch { /* */ }
}

const currentAgent = computed(() => agentList.value.find(a => a.name === selectedAgent.value))
const currentVoices = computed(() => voiceGroups.value[selectedAgent.value] || [])

async function setAgentVoice(voiceRef: string | null) {
  if (!selectedAgent.value) return
  try {
    await put(`/agents/${selectedAgent.value}`, { voice_ref: voiceRef })
    const a = agentList.value.find(x => x.name === selectedAgent.value)
    if (a) a.voice_ref = voiceRef
    message.success(t('mediaView.voiceSet'))
  } catch (e: any) { message.error(e.message) }
}

async function deleteVoice(name: string) {
  if (!selectedAgent.value) return
  try {
    await del(`/media/tts/voices/${selectedAgent.value}/${name}`)
    message.success(t('mediaView.voiceDeleted'))
    await loadVoices()
  } catch (e: any) { message.error(e.message) }
}

const agentOptions = computed(() =>
  agentList.value.map(a => ({ label: a.display_name, value: a.name }))
)

async function submitTask(kind: 'image' | 'video') {
  const prompt = kind === 'image' ? imagePrompt.value : videoPrompt.value
  if (!prompt.trim()) return
  submitting.value = kind
  try {
    await post(`/media/${kind}`, { prompt })
    message.success(t('mediaView.taskQueued'))
    loadTasks()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    submitting.value = ''
  }
}

async function loadTasks() {
  try { tasks.value = await get<any[]>('/media/tasks?limit=20') } catch { /* */ }
}

async function cancelTask(id: string) {
  try {
    await del(`/media/tasks/${id}`)
    message.success(t('mediaView.cancelled'))
    loadTasks()
  } catch (e: any) { message.error(e.message) }
}

async function loadGallery() {
  try {
    gallery.value = await get<any[]>(`/media/gallery?type=${galleryType.value}&limit=48`)
  } catch (e: any) { message.error(e.message) }
}

async function removeMedia(name: string) {
  try {
    await del(`/media/gallery/${galleryType.value}/${name}`, true)
    gallery.value = gallery.value.filter(g => g.name !== name)
    message.success(t('mediaView.deleted'))
  } catch (e: any) { message.error(e.message) }
}

function openUrl(url: string) {
  window.open(url, '_blank')
}

const statusType: Record<string, any> = {
  queued: 'default', running: 'info', done: 'success', failed: 'error',
}
</script>

<template>
  <div class="media-view">
    <h2 class="view-title">🎙 {{ t('mediaView.title') }}</h2>

    <n-tabs type="line" animated>
      <n-tab-pane name="tts" :tab="t('mediaView.tts')">
        <div class="panel-row">
          <Tilt3D :max-x="4" :max-y="6" style="flex: 2; min-width: 300px"><div class="glass-panel panel main">
            <n-input v-model:value="ttsText" type="textarea" :rows="4"
                     :placeholder="t('mediaView.ttsInputPh')" maxlength="500" show-count />
            <div class="tts-controls">
              <n-select v-model:value="ttsAgent" :options="agentOptions" :placeholder="t('mediaView.agentPh')"
                        style="max-width: 220px" />
              <n-select v-model:value="ttsStyle" :options="styles" :placeholder="t('mediaView.emotionPh')"
                        clearable style="max-width: 180px" />
              <n-button type="primary" :loading="ttsLoading" @click="synthesize">🎵 {{ t('mediaView.synthesize') }}</n-button>
            </div>
            <audio v-if="ttsResult" :src="ttsResult" controls autoplay class="tts-player"></audio>
          </div></Tilt3D>
          <Tilt3D :max-x="4" :max-y="6" style="flex: 1; min-width: 220px"><div class="glass-panel panel side">
            <h4>{{ t('mediaView.readSettings') }}</h4>
            <label class="cfg">
              {{ t('mediaView.autoSpeak') }}
              <n-switch :value="ui.autoSpeak" @update:value="setAutoSpeak" />
            </label>
            <p class="cfg-hint">{{ t('mediaView.autoSpeakDesc') }}</p>
          </div></Tilt3D>
        </div>

        <!-- 参考音频管理：选择 agent 后管理 -->
        <Tilt3D :max-x="4" :max-y="6"><div class="glass-panel panel">
          <h4>{{ t('mediaView.voiceManage') }}</h4>
          <p class="cfg-hint" style="margin-bottom: 12px">{{ t('mediaView.voiceUploadHint') }}</p>

          <!-- Agent 选择器 -->
          <div class="voice-select-row" style="margin-bottom: 14px">
            <n-select v-model:value="selectedAgent" :options="agentOptions"
                      :placeholder="t('mediaView.voiceManage')" style="max-width: 200px" />
          </div>

          <!-- 选中 agent 的参考音频管理 -->
          <div v-if="currentAgent" class="voice-agent-block">
            <div class="voice-agent-header">
              <span class="voice-agent-name">{{ currentAgent.display_name }}</span>
              <n-tag size="tiny" :bordered="false">{{ currentAgent.name }}</n-tag>
              <span class="voice-agent-current">
                {{ currentAgent.voice_ref ? currentAgent.voice_ref.split('/').pop() : t('mediaView.noVoice') }}
              </span>
            </div>
            <div class="voice-agent-body">
              <div class="tts-controls" style="margin-bottom: 8px">
                <input ref="voiceInputEl" type="file" accept="audio/mpeg,audio/wav" style="display: none"
                       @change="onVoiceFilePick" />
                <n-button size="small" :loading="voiceUploading" @click="voiceInputEl?.click()">
                  📁 {{ t('mediaView.selectAudio') }}
                </n-button>
              </div>
              <div class="voice-select-row">
                <n-select :value="currentAgent.voice_ref"
                          :options="currentVoices.map(v => ({ label: v.name, value: v.voice_ref }))"
                          :placeholder="t('mediaView.noVoice')" size="small" clearable
                          style="max-width: 240px"
                          @update:value="(v: any) => setAgentVoice(v)" />
              </div>
              <div class="voice-list">
                <div v-for="v in currentVoices" :key="v.voice_ref" class="voice-item">
                  <span class="voice-name" :class="{ active: currentAgent.voice_ref === v.voice_ref }">{{ v.name }}</span>
                  <n-popconfirm @positive-click="deleteVoice(v.name)">
                    <template #trigger>
                      <n-button size="tiny" type="error" quaternary>🗑</n-button>
                    </template>
                    {{ t('mediaView.voiceDeleteConfirm') }}
                  </n-popconfirm>
                </div>
                <div v-if="!currentVoices.length" class="empty-hint" style="padding: 4px 0; text-align: left">
                  {{ t('mediaView.noVoices') }}
                </div>
              </div>
            </div>
          </div>
        </div></Tilt3D>
      </n-tab-pane>

      <n-tab-pane name="image" :tab="t('mediaView.imageGen')">
        <Tilt3D :max-x="4" :max-y="6"><div class="glass-panel panel">
          <n-input v-model:value="imagePrompt" type="textarea" :rows="3"
                   :placeholder="t('mediaView.imagePromptPh')" />
          <n-button type="primary" style="margin-top: 10px"
                    :loading="submitting === 'image'" @click="submitTask('image')">
            🎨 {{ t('mediaView.submit') }}
          </n-button>
        </div></Tilt3D>
      </n-tab-pane>

      <n-tab-pane name="video" :tab="t('mediaView.videoGen')">
        <Tilt3D :max-x="4" :max-y="6"><div class="glass-panel panel">
          <p class="queue-hint">{{ t('mediaView.videoHint') }}
            当前队列 {{ tasks.filter(t => t.status === 'queued' || t.status === 'running').length }} {{ t('mediaView.queueCount') }}。</p>
          <n-input v-model:value="videoPrompt" type="textarea" :rows="3"
                   :placeholder="t('mediaView.videoPromptPh')" />
          <n-button type="primary" style="margin-top: 10px"
                    :loading="submitting === 'video'" @click="submitTask('video')">
            🎬 {{ t('mediaView.submit') }}
          </n-button>
        </div></Tilt3D>
      </n-tab-pane>
    </n-tabs>

    <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section">
      <h3>{{ t('mediaView.taskQueue') }}</h3>
      <div class="task-list">
        <div v-for="task in tasks" :key="task.id" class="task-row">
          <n-tag size="small" :type="statusType[task.status]" :bordered="false">{{ task.status }}</n-tag>
          <span class="task-kind">{{ task.kind }}</span>
          <span class="task-prompt">{{ task.prompt }}</span>
          <n-progress v-if="task.status === 'running'" type="line" :percentage="Math.round((task.progress || 0) * 100)"
                      style="max-width: 140px" :height="6" />
          <span v-if="task.error" class="task-error">{{ task.error }}</span>
          <a v-if="task.result_path && task.status === 'done'" :href="task.result_path" target="_blank" class="task-link">{{ t('mediaView.view') }}</a>
          <n-button v-if="task.status === 'queued'" size="tiny" quaternary @click="cancelTask(task.id)">{{ t('cancel') }}</n-button>
        </div>
        <div v-if="!tasks.length" class="empty-hint">{{ t('mediaView.queueEmpty') }}</div>
      </div>
    </section></Tilt3D>

    <section class="glass-panel section">
      <div class="gallery-head">
        <h3>{{ t('mediaView.gallery') }}</h3>
        <n-tabs type="segment" size="small" v-model:value="galleryType"
                @update:value="loadGallery" style="max-width: 280px">
          <n-tab-pane name="image" :tab="t('mediaView.image')" />
          <n-tab-pane name="video" :tab="t('mediaView.video')" />
          <n-tab-pane name="audio" :tab="t('mediaView.audio')" />
        </n-tabs>
      </div>
      <div class="gallery-grid">
        <Tilt3D v-for="g in gallery" :key="g.name"><div class="gallery-card">
          <img v-if="galleryType === 'image'" :src="g.url" loading="lazy" @click="openUrl(g.url)" />
          <video v-else-if="galleryType === 'video'" :src="g.url" controls preload="metadata"></video>
          <audio v-else :src="g.url" controls></audio>
          <div class="gallery-meta">
            <span class="g-name">{{ g.name }}</span>
            <n-popconfirm @positive-click="removeMedia(g.name)">
              <template #trigger><button class="g-del">🗑</button></template>
              {{ t('mediaView.deleteConfirm') }}
            </n-popconfirm>
          </div>
        </div></Tilt3D>
        <div v-if="!gallery.length" class="empty-hint">{{ t('mediaView.emptyGallery') }}</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.view-title { font-family: 'Noto Serif SC', serif; margin-bottom: 14px; }

.panel-row { display: flex; gap: 14px; flex-wrap: wrap; }
.panel { padding: 16px 18px; }
.panel.main { flex: 2; min-width: 300px; }
.panel.side { flex: 1; min-width: 220px; }
.panel h4 { font-size: 13px; color: var(--dendro); margin-bottom: 10px; }

.tts-controls { display: flex; gap: 10px; margin-top: 12px; flex-wrap: wrap; }
.tts-player { width: 100%; margin-top: 12px; }

.cfg { display: flex; align-items: center; justify-content: space-between; font-size: 13.5px; }
.cfg-hint { font-size: 12px; color: var(--moon-dim); margin-top: 10px; line-height: 1.6; }

.voice-list { display: flex; flex-direction: column; gap: 4px; }
.voice-item { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 13px; }
.voice-name { font-family: 'JetBrains Mono', monospace; min-width: 80px; }
.empty-hint { font-size: 13px; color: var(--moon-dim); padding: 12px 0; text-align: center; }
.voice-agent-list { display: flex; flex-direction: column; gap: 10px; }
.voice-agent-block { padding: 10px 12px; border: 1px solid var(--moon-edge, rgba(255,255,255,.08)); border-radius: 8px; }
.voice-agent-header { display: flex; align-items: center; gap: 8px; }
.voice-agent-name { font-size: 14px; font-weight: 500; }
.voice-agent-current { margin-left: auto; font-size: 12px; color: var(--moon-dim); font-family: 'JetBrains Mono', monospace; }
.voice-arrow { font-size: 10px; color: var(--moon-dim); }
.voice-agent-body { margin-top: 10px; }
.voice-select-row { margin-bottom: 8px; }
.voice-name.active { color: var(--wisdom, #5b8c5a); font-weight: 500; }

.queue-hint { font-size: 12.5px; color: var(--wisdom); margin-bottom: 10px; }

.section { padding: 16px 18px; margin-top: 14px; }
.section h3 { font-size: 14px; color: var(--dendro); margin-bottom: 10px; }

.task-list { display: flex; flex-direction: column; gap: 6px; }
.task-row {
  display: flex; align-items: center; gap: 10px;
  font-size: 13px; padding: 6px 4px;
  border-bottom: 1px solid rgba(127, 214, 80, 0.06);
  flex-wrap: wrap;
}
.task-kind { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--wisdom); }
.task-prompt { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 120px; }
.task-error { color: var(--alert); font-size: 12px; }
.task-link { color: var(--dendro); font-size: 12px; }

.gallery-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 12px; }
.gallery-head h3 { margin: 0; }

.gallery-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}

.gallery-card {
  border-radius: 10px; overflow: hidden;
  border: 1px solid var(--glass-border);
  background: rgba(15, 31, 23, 0.4);
  transition: transform 0.2s var(--ease-out), border-color 0.2s;
}
.gallery-card:hover {
  border-color: rgba(127, 214, 80, 0.4);
}
.gallery-card img { width: 100%; height: 140px; object-fit: cover; cursor: zoom-in; display: block; }
.gallery-card video { width: 100%; display: block; }
.gallery-card audio { width: 100%; padding: 8px; }

.gallery-meta {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 10px; font-size: 11px; color: var(--moon-dim);
}
.g-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: 'JetBrains Mono', monospace; }
.g-del { background: none; border: none; cursor: pointer; opacity: 0.6; }
.g-del:hover { opacity: 1; }

.empty-hint { color: var(--moon-dim); font-size: 13px; padding: 16px 0; text-align: center; grid-column: 1 / -1; }
</style>
