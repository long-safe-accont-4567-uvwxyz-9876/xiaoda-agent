<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { NButton, NTag, NSpin, useMessage } from 'naive-ui'
import { getDisclaimerStatus, agreeDisclaimer } from '../api'
import { t } from '../i18n'

const message = useMessage()

const loading = ref(true)
const agreed = ref(false)
const agreedAt = ref('')
const agreeing = ref(false)

onMounted(async () => {
  await loadStatus()
})

async function loadStatus() {
  loading.value = true
  try {
    const status = await getDisclaimerStatus()
    agreed.value = !!status.agreed
    agreedAt.value = status.agreed_at || ''
  } catch (e: any) {
    message.error(e.message)
  } finally {
    loading.value = false
  }
}

async function handleAgree() {
  agreeing.value = true
  try {
    await agreeDisclaimer(true)
    agreed.value = true
    agreedAt.value = new Date().toISOString()
    message.success(t('disclaimer.agreed'))
  } catch (e: any) {
    message.error(e.message)
  } finally {
    agreeing.value = false
  }
}

function formatTime(ts: string): string {
  if (!ts) return ''
  try {
    const d = new Date(ts)
    return d.toLocaleString()
  } catch {
    return ts
  }
}
</script>

<template>
  <div class="disclaimer-page">
    <div class="disclaimer-card">
      <h1 class="page-title">── {{ t('disclaimer.title') }} ──</h1>

      <n-spin :show="loading">
        <div class="disclaimer-body">
          <div class="disclaimer-scroll">
            <pre class="disclaimer-text">{{ t('disclaimer.content') }}</pre>
          </div>

          <div class="signature-section">
            <p class="signature-text">{{ t('brand_signature.full') }}</p>
          </div>

          <div class="status-section" v-if="!loading">
            <div v-if="agreed" class="agreed-status">
              <n-tag type="success" size="medium" round>
                ✓ {{ t('disclaimer.agreed') }}
              </n-tag>
              <span class="agreed-time" v-if="agreedAt">
                {{ t('disclaimer.agreedAt') }}：{{ formatTime(agreedAt) }}
              </span>
            </div>
            <div v-else class="not-agreed-status">
              <n-tag type="warning" size="medium" round>
                {{ t('disclaimer.notAgreed') }}
              </n-tag>
            </div>
          </div>

          <div class="action-section" v-if="!loading && !agreed">
            <n-button
              type="primary"
              size="large"
              :loading="agreeing"
              @click="handleAgree"
            >
              {{ t('disclaimer.agreeButton') }}
            </n-button>
          </div>
        </div>
      </n-spin>
    </div>
  </div>
</template>

<style scoped>
.disclaimer-page {
  padding: 24px;
  max-width: 760px;
  margin: 0 auto;
}

.disclaimer-card {
  background: rgba(15, 31, 23, 0.55);
  backdrop-filter: blur(12px);
  border: 1px solid var(--glass-border);
  border-radius: 16px;
  padding: 32px 36px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}

.page-title {
  font-size: 20px;
  color: var(--wisdom);
  font-family: 'Noto Serif SC', serif;
  text-align: center;
  margin: 0 0 24px;
  letter-spacing: 2px;
}

.disclaimer-body {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.disclaimer-scroll {
  max-height: 420px;
  overflow-y: auto;
  background: rgba(0, 0, 0, 0.25);
  border-radius: 10px;
  padding: 18px 22px;
  border: 1px solid var(--glass-border);
}

.disclaimer-text {
  font-size: 14px;
  color: var(--moon-dim);
  font-family: 'Noto Sans SC', sans-serif;
  white-space: pre-wrap;
  line-height: 1.9;
  margin: 0;
}

.signature-section {
  text-align: center;
  padding: 12px 0;
  border-top: 1px solid var(--glass-border);
}

.signature-text {
  font-size: 13px;
  color: var(--dendro);
  font-family: 'Noto Serif SC', serif;
  margin: 0;
  opacity: 0.85;
  letter-spacing: 1px;
}

.status-section {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 16px;
  padding: 8px 0;
}

.agreed-status {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

.agreed-time {
  font-size: 12px;
  color: var(--moon-dim);
  font-family: 'Noto Serif SC', serif;
  opacity: 0.7;
}

.not-agreed-status {
  display: flex;
  justify-content: center;
}

.action-section {
  display: flex;
  justify-content: center;
  padding: 8px 0 4px;
}

.action-section :deep(.n-button--primary-type) {
  background: var(--dendro);
  color: #fff;
  font-family: 'Noto Serif SC', serif;
  letter-spacing: 1px;
}
</style>
