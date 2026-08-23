<script setup lang="ts">
/**
 * 表情包管理 Tab：上传（选图/描述/情绪 + 本地预览）与列表删除。
 * 挂载时按 agentName 拉取列表（编辑器每次打开都会重新挂载本组件）。
 */
import { ref, watch, onBeforeUnmount } from 'vue'
import { NButton, NInput, NSelect, NSpin, NTag, NImage, NEmpty, NPopconfirm, useMessage } from 'naive-ui'
import { api } from '../../api'
import { useAuthStore } from '../../stores/auth'
import { t } from '../../i18n'

const props = defineProps<{ agentName: string }>()

const message = useMessage()
const auth = useAuthStore()

const stickerList = ref<Array<{ name: string; description: string; emotion: string; url: string }>>([])
const stickerEmotions = ref<string[]>([])
const stickerLoading = ref(false)
const stickerUploading = ref(false)
const stickerFile = ref<File | null>(null)
const stickerDesc = ref('')
const stickerEmotion = ref('happy')
const stickerInput = ref<HTMLInputElement | null>(null)

let stickerObjectUrl = ''
const createObjectURL = (f: File) => {
  if (stickerObjectUrl) URL.revokeObjectURL(stickerObjectUrl)
  stickerObjectUrl = URL.createObjectURL(f)
  return stickerObjectUrl
}

async function loadStickers() {
  if (!props.agentName) return
  stickerLoading.value = true
  try {
    const data = await api.listStickers(props.agentName)
    stickerList.value = data.stickers || []
    stickerEmotions.value = data.emotions || []
  } catch {
    stickerList.value = []
  } finally {
    stickerLoading.value = false
  }
}

watch(() => props.agentName, () => loadStickers(), { immediate: true })

function onStickerFilePick(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 8 * 1024 * 1024) {
    message.error(t('agentsView.imgTooLarge'))
    input.value = ''
    return
  }
  stickerFile.value = file
}

async function uploadSticker() {
  if (!stickerFile.value || !stickerDesc.value.trim()) {
    message.warning(t('agentsView.stickerWarn'))
    return
  }
  stickerUploading.value = true
  try {
    await api.uploadSticker(props.agentName, stickerFile.value, stickerDesc.value.trim(), stickerEmotion.value)
    message.success(t('agentsView.stickerAdded'))
    if (stickerObjectUrl) { URL.revokeObjectURL(stickerObjectUrl); stickerObjectUrl = '' }
    stickerFile.value = null
    stickerDesc.value = ''
    if (stickerInput.value) stickerInput.value.value = ''
    await loadStickers()
  } catch (e: any) {
    message.error(e.message)
  } finally {
    stickerUploading.value = false
  }
}

async function removeSticker(filename: string) {
  try {
    await api.deleteSticker(props.agentName, filename)
    message.success(t('agentsView.stickerDeleted'))
    await loadStickers()
  } catch (e: any) {
    message.error(e.message)
  }
}

onBeforeUnmount(() => {
  if (stickerObjectUrl) { URL.revokeObjectURL(stickerObjectUrl); stickerObjectUrl = '' }
})
</script>

<template>
  <div class="sticker-section">
    <!-- 上传区域 -->
    <div class="sticker-upload glass-panel">
      <div class="sticker-upload-title">{{ t('agentsView.addSticker') }}</div>
      <div class="sticker-upload-row">
        <input ref="stickerInput" type="file" accept="image/png,image/jpeg,image/gif,image/webp"
               style="display: none" @change="onStickerFilePick" />
        <n-button size="small" @click="stickerInput?.click()">
          {{ stickerFile ? stickerFile.name : t('agentsView.selectImage') }}
        </n-button>
        <n-input v-model:value="stickerDesc" size="small" :placeholder="t('agentsView.stickerDescPh')"
                 style="flex: 1; min-width: 120px;" />
        <n-select v-model:value="stickerEmotion" size="small" style="width: 120px"
                  :options="(stickerEmotions.length ? stickerEmotions : ['happy','excited','love','shy','sad','angry','surprised','confused','thinking','playful','moved','neutral','pout','fear','anxious','curious','greeting']).map(e => ({ label: e, value: e }))" />
        <n-button type="primary" size="small" :loading="stickerUploading" :disabled="!stickerFile || !stickerDesc.trim()"
                  @click="uploadSticker">
          {{ t('agentsView.upload') }}
        </n-button>
      </div>
      <div v-if="stickerFile" class="sticker-upload-preview">
        <img :src="createObjectURL(stickerFile)" alt="preview" />
        <span class="sticker-preview-info">{{ stickerDesc || t('agentsView.noDesc') }} · {{ stickerEmotion }}</span>
      </div>
    </div>

    <!-- 表情包列表 -->
    <n-spin :show="stickerLoading">
      <div v-if="stickerList.length" class="sticker-grid">
        <div v-for="s in stickerList" :key="s.name" class="sticker-card">
          <n-image :src="s.url + '?token=' + auth.token" width="100" height="100" object-fit="cover"
                   :fallback-src="''" lazy class="sticker-img" />
          <div class="sticker-info">
            <span class="sticker-desc">{{ s.description }}</span>
            <n-tag size="tiny" :bordered="false">{{ s.emotion }}</n-tag>
          </div>
          <n-popconfirm @positive-click="removeSticker(s.name)">
            <template #trigger>
              <n-button size="tiny" type="error" quaternary class="sticker-del">{{ t('agentsView.delete') }}</n-button>
            </template>
            {{ t('agentsView.stickerDeleteConfirm') }}
          </n-popconfirm>
        </div>
      </div>
      <n-empty v-else :description="t('agentsView.stickerEmpty')" style="padding: 32px 0" />
    </n-spin>
  </div>
</template>

<style scoped>
/* ── 表情包管理 ── */
.sticker-section { display: flex; flex-direction: column; gap: 14px; }
.sticker-upload { padding: 12px 14px; }
.sticker-upload-title { font-size: 13px; font-weight: 600; color: var(--dendro); margin-bottom: 8px; }
.sticker-upload-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.sticker-upload-preview {
  display: flex; align-items: center; gap: 10px; margin-top: 10px;
}
.sticker-upload-preview img {
  width: 64px; height: 64px; object-fit: cover; border-radius: 8px;
  border: 1px solid var(--glass-border);
}
.sticker-preview-info { font-size: 12px; color: var(--moon-dim); }

.sticker-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
}
.sticker-card {
  display: flex; flex-direction: column; align-items: center;
  padding: 8px; border-radius: 10px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--glass-border);
  position: relative;
}
.sticker-card:hover { border-color: rgba(127, 214, 80, 0.3); }
.sticker-img { border-radius: 8px; overflow: hidden; }
.sticker-info {
  display: flex; flex-direction: column; align-items: center;
  gap: 4px; margin-top: 6px; width: 100%;
}
.sticker-desc {
  font-size: 11px; color: var(--moon); text-align: center;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  max-width: 120px;
}
.sticker-del { margin-top: 4px; }
</style>
