<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount } from 'vue'
import {
  NButton, NSwitch, NInput, NSelect, NTabs, NTabPane, NTag,
  NPopconfirm, NProgress, useMessage,
} from 'naive-ui'
import { get, post, put, del } from '../api'
import { getWsClient } from '../api/ws'
import { useUiStore } from '../stores/ui'
import { t } from '../i18n'

const message = useMessage()
const ui = useUiStore()
const ws = getWsClient()

// TTS
const ttsText = ref('')
const ttsVoice = ref('nahida')
const ttsStyle = ref<string | null>(null)
const voices = ref<Array<{ label: string; value: string }>>([])
const styles = ref<Array<{ label: string; value: string }>>([])
const ttsResult = ref('')
const ttsLoading = ref(false)

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
    const v = await get('/media/tts/voices')
    voices.value = v.voices.map((x: any) => ({ label: `${x.id}${x.description ? ' · ' + x.description.slice(0, 16) : ''}`, value: x.id }))
    styles.value = v.styles.map((s: string) => ({ label: s, value: s }))
    const cfg = await get('/media/tts/config')
    ui.autoSpeak = cfg.auto_speak
    ttsVoice.value = cfg.default_voice || 'nahida'
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
  ttsLoading.value = true
  try {
    const r = await post('/media/tts', {
      text: ttsText.value, voice: ttsVoice.value, style: ttsStyle.value || '',
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

async function setDefaultVoice(v: string) {
  ttsVoice.value = v
  try {
    await put('/media/tts/config', { default_voice: v })
  } catch { /* */ }
}

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
          <div class="glass-panel panel main">
            <n-input v-model:value="ttsText" type="textarea" :rows="4"
                     :placeholder="t('mediaView.ttsInputPh')" maxlength="500" show-count />
            <div class="tts-controls">
              <n-select v-model:value="ttsVoice" :options="voices" :placeholder="t('mediaView.voicePh')"
                        style="max-width: 220px" @update:value="setDefaultVoice" />
              <n-select v-model:value="ttsStyle" :options="styles" :placeholder="t('mediaView.emotionPh')"
                        clearable style="max-width: 180px" />
              <n-button type="primary" :loading="ttsLoading" @click="synthesize">🎵 {{ t('mediaView.synthesize') }}</n-button>
            </div>
            <audio v-if="ttsResult" :src="ttsResult" controls autoplay class="tts-player"></audio>
          </div>
          <div class="glass-panel panel side">
            <h4>{{ t('mediaView.readSettings') }}</h4>
            <label class="cfg">
              {{ t('mediaView.autoSpeak') }}
              <n-switch :value="ui.autoSpeak" @update:value="setAutoSpeak" />
            </label>
            <p class="cfg-hint">{{ t('mediaView.autoSpeakDesc') }}</p>
          </div>
        </div>
      </n-tab-pane>

      <n-tab-pane name="image" :tab="t('mediaView.imageGen')">
        <div class="glass-panel panel">
          <n-input v-model:value="imagePrompt" type="textarea" :rows="3"
                   :placeholder="t('mediaView.imagePromptPh')" />
          <n-button type="primary" style="margin-top: 10px"
                    :loading="submitting === 'image'" @click="submitTask('image')">
            🎨 {{ t('mediaView.submit') }}
          </n-button>
        </div>
      </n-tab-pane>

      <n-tab-pane name="video" :tab="t('mediaView.videoGen')">
        <div class="glass-panel panel">
          <p class="queue-hint">{{ t('mediaView.videoHint') }}
            当前队列 {{ tasks.filter(t => t.status === 'queued' || t.status === 'running').length }} {{ t('mediaView.queueCount') }}。</p>
          <n-input v-model:value="videoPrompt" type="textarea" :rows="3"
                   :placeholder="t('mediaView.videoPromptPh')" />
          <n-button type="primary" style="margin-top: 10px"
                    :loading="submitting === 'video'" @click="submitTask('video')">
            🎬 {{ t('mediaView.submit') }}
          </n-button>
        </div>
      </n-tab-pane>
    </n-tabs>

    <section class="glass-panel section">
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
    </section>

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
        <div v-for="g in gallery" :key="g.name" class="gallery-card">
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
        </div>
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
  transform: perspective(600px) translateZ(8px);
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
