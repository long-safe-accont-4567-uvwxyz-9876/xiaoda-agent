<script setup lang="ts">
/**
 * 画像 Tab：版本头 + 整合按钮 + 画像正文（Markdown）+ 变更日志折叠。
 * 数据经 props 注入；整合动作向上 emit，由视图层触发 useInsightPortrait。
 */
import { NButton, NCollapse, NCollapseItem, NTag } from 'naive-ui'
import Tilt3D from '../fx/Tilt3D.vue'
import { renderMarkdown } from '../../utils/markdown'
import { t } from '../../i18n'
import type { PortraitData, PortraitHistoryEntry } from './types'

defineProps<{
  portrait: PortraitData
  history: PortraitHistoryEntry[]
  consolidating: boolean
}>()

const emit = defineEmits<{
  (e: 'consolidate'): void
}>()
</script>

<template>
  <div>
    <div class="portrait-head">
      <span v-if="portrait.version">{{ t('insightView.versionLabel') }} v{{ portrait.version }} ·
        {{ new Date(Number(portrait.created_at || 0) * 1000).toLocaleString('zh-CN') }}</span>
      <n-button size="small" type="primary" :loading="consolidating" @click="emit('consolidate')">
        {{ t('insightView.consolidateBtn') }}
      </n-button>
    </div>
    <Tilt3D :max-x="4" :max-y="6"><div class="glass-panel portrait-card md-body"
         v-html="renderMarkdown(portrait.content || t('insightView.noPortrait'))"></div></Tilt3D>
    <n-collapse style="margin-top: 12px">
      <n-collapse-item :title="t('insightView.changeLog')" name="log">
        <div v-for="h in history" :key="h.version" class="history-row">
          <n-tag size="small" :bordered="false">v{{ h.version }}</n-tag>
          <span class="history-log">{{ h.change_log || t('insightView.noDesc') }}</span>
          <span class="history-time">{{ new Date((h.created_at ?? 0) * 1000).toLocaleString('zh-CN') }}</span>
        </div>
      </n-collapse-item>
    </n-collapse>
  </div>
</template>

<style scoped>
.portrait-head {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 10px; font-size: 13px; color: var(--moon-dim);
}
.portrait-card { padding: 18px 22px; line-height: 1.8; }

.history-row { display: flex; align-items: center; gap: 10px; padding: 4px 0; font-size: 13px; }
.history-log { flex: 1; color: var(--moon-dim); }
.history-time { font-size: 11px; color: var(--moon-dim); }

:deep(.md-body p) { margin-bottom: 8px; }
:deep(.md-body h1), :deep(.md-body h2), :deep(.md-body h3) {
  color: var(--dendro); margin: 10px 0 6px; font-size: 16px;
}
:deep(.md-body ul) { padding-left: 20px; }
</style>
