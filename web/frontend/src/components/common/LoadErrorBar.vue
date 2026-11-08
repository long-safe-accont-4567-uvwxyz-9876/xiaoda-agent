<script setup lang="ts">
/** 通用"加载失败 + 重试"横条：把手写空态换成可区分的错误态（审查 C1）。 */
import { NButton } from 'naive-ui'
import { t } from '../../i18n'

defineProps<{ message: string; retryLabel?: string }>()
const emit = defineEmits<{ retry: [] }>()
</script>

<template>
  <div class="load-error-bar" role="alert">
    <span class="bar-msg">{{ message }}</span>
    <n-button size="tiny" tertiary @click="emit('retry')">{{ retryLabel || t('errors.retry') }}</n-button>
  </div>
</template>

<style scoped>
.load-error-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 12px;
  margin-bottom: 12px;
  border-radius: 10px;
  border: 1px solid rgba(217, 106, 95, 0.35);
  background: rgba(217, 106, 95, 0.08);
  font-size: 12.5px;
  color: var(--alert, #d96a5f);
}
.bar-msg { overflow: hidden; text-overflow: ellipsis; }
</style>
