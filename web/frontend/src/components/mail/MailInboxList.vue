<script setup lang="ts">
/**
 * 收件箱预览卡：最近邮件列表（未读高亮）。数据经 props 注入，刷新向上 emit。
 */
import { NButton, NEmpty, NSpin, NTag } from 'naive-ui'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import { fmtTime, senderDisplay } from './format'
import type { InboxMail } from './types'

defineProps<{
  items: InboxMail[]
  loading: boolean
}>()

const emit = defineEmits<{
  (e: 'refresh'): void
}>()
</script>

<template>
  <section class="glass-panel section animate-slide-up">
    <div class="section-head">
      <h3>{{ t('mailView.inboxCard') }}</h3>
      <n-button size="small" :loading="loading" @click="emit('refresh')">{{ t('refresh') }}</n-button>
    </div>
    <p class="apikey-desc">{{ t('mailView.inboxHint') }}</p>
    <n-spin :show="loading">
      <div v-if="items.length" class="mail-list">
        <Tilt3D v-for="m in items" :key="m.message_id"><div class="mail-item" :class="{ unread: !m.is_read }">
          <div class="mail-from">
            <span class="from-text" :title="senderDisplay(m)">{{ senderDisplay(m) }}</span>
            <n-tag v-if="!m.is_read" size="tiny" type="warning" round>{{ t('mailView.unread') }}</n-tag>
            <n-tag v-else size="tiny" round>{{ t('mailView.read') }}</n-tag>
          </div>
          <div class="mail-subject" :title="m.subject">{{ m.subject || t('mailView.noSubject') }}</div>
          <div class="mail-time mono">{{ fmtTime(m.created_at) }}</div>
        </div></Tilt3D>
      </div>
      <n-empty v-else :description="t('mailView.inboxEmpty')" style="padding: 32px 0" />
    </n-spin>
  </section>
</template>

<style scoped>
.section { padding: 16px 18px; margin-bottom: 14px; }
.section-head { display: flex; align-items: center; justify-content: space-between; }
.section-head h3 { font-size: 14px; color: var(--dendro); margin: 0; }

.apikey-desc { font-size: 12.5px; color: var(--wisdom); margin: 0 0 12px; }

/* 收件箱列表 */
.mail-list { display: flex; flex-direction: column; gap: 6px; }
.mail-item {
  display: grid;
  grid-template-columns: 1.4fr 2fr auto;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid transparent;
  background: rgba(10, 24, 16, 0.3);
  transition: background 0.2s, border-color 0.2s, transform 0.2s var(--ease-out);
}
.mail-item:hover {
  background: rgba(143, 229, 96, 0.08);
  border-color: var(--glass-border);
  transform: translateX(2px);
}
.mail-item.unread { background: rgba(232, 213, 163, 0.07); border-left: 3px solid var(--wisdom); }
.mail-from {
  display: flex; align-items: center; gap: 8px;
  min-width: 0;
}
.from-text {
  font-size: 13px; color: var(--moon);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  flex: 1;
}
.mail-item.unread .from-text { font-weight: 600; }
.mail-subject {
  font-size: 13px; color: var(--moon-dim);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mail-item.unread .mail-subject { color: var(--moon); }
.mail-time { font-size: 11.5px; color: var(--moon-dim); white-space: nowrap; }
.mono { font-family: 'JetBrains Mono', monospace; font-size: 13px; }

@media (max-width: 640px) {
  .mail-item { grid-template-columns: 1fr; gap: 4px; }
}
</style>
