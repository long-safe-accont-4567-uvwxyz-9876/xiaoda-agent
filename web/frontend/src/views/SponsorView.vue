<script setup lang="ts">
import { t } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'
import SumeruIcon from '../components/fx/SumeruIcon.vue'

const wechatImg = '/assets/sponsor/wechat_qr.png'
</script>

<template>
  <div class="sponsor-page">
    <Tilt3D :max-x="3" :max-y="5">
      <div class="sponsor-card">
        <h1 class="page-title">── {{ t('sponsor.title') }} ──</h1>

        <div class="sponsor-body">
          <p class="intro-text">{{ t('sponsor.intro') }}</p>

          <div class="qr-section">
            <div class="qr-item">
              <div class="qr-frame">
                <img :src="wechatImg" :alt="t('sponsor.wechat')" />
              </div>
              <span class="qr-label">{{ t('sponsor.wechat') }}</span>
            </div>
          </div>

          <div class="usage-section">
            <p class="usage-text">{{ t('sponsor.usage') }}</p>
          </div>

          <div class="warning-section">
            <p class="warning-text">{{ t('sponsor.warning') }}</p>
          </div>

          <div class="footer-note">
            <span class="footer-icon"><SumeruIcon name="tea" :size="14" /></span>
            <span class="footer-text">{{ t('sponsor.footerNote') }}</span>
          </div>
        </div>
      </div>
    </Tilt3D>
  </div>
</template>

<style scoped>
.sponsor-page {
  padding: 24px;
  max-width: 720px;
  margin: 0 auto;
}

.sponsor-card {
  background: rgba(15, 31, 23, 0.55);
  backdrop-filter: blur(12px);
  border: 1px solid var(--glass-border);
  border-radius: 16px;
  padding: 28px 32px;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.3);
}

.page-title {
  font-size: 18px;
  color: var(--wisdom);
  font-family: 'Noto Serif SC', serif;
  text-align: center;
  margin: 0 0 20px;
  letter-spacing: 2px;
  font-weight: 600;
}

.sponsor-body {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.intro-text {
  font-size: 13px;
  color: var(--moon-dim);
  font-family: 'Noto Sans SC', sans-serif;
  line-height: 1.8;
  text-align: center;
  margin: 0;
  opacity: 0.9;
}

.qr-section {
  display: flex;
  justify-content: center;
  padding: 12px 0 8px;
}

.qr-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

.qr-frame {
  width: 180px;
  height: 180px;
  background: #fff;
  border-radius: 10px;
  padding: 8px;
  border: 1px solid var(--glass-border);
  display: flex;
  align-items: center;
  justify-content: center;
}

.qr-frame img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: 4px;
}

.qr-label {
  font-size: 12px;
  color: var(--moon-dim);
  font-family: 'Noto Serif SC', serif;
  letter-spacing: 1px;
}

.usage-section {
  padding: 10px 14px;
  background: rgba(0, 0, 0, 0.18);
  border-radius: 8px;
  border-left: 2px solid var(--dendro);
}

.usage-text {
  font-size: 12px;
  color: var(--moon-dim);
  font-family: 'Noto Sans SC', sans-serif;
  line-height: 1.7;
  margin: 0;
  opacity: 0.8;
}

.warning-section {
  padding: 10px 14px;
  background: rgba(0, 0, 0, 0.18);
  border-radius: 8px;
  border-left: 2px solid rgba(232, 213, 163, 0.4);
}

.warning-text {
  font-size: 12px;
  color: rgba(232, 213, 163, 0.75);
  font-family: 'Noto Sans SC', sans-serif;
  line-height: 1.7;
  margin: 0;
}

.footer-note {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding-top: 8px;
  border-top: 1px solid var(--glass-border);
  color: rgba(232, 213, 163, 0.5);
}

.footer-icon {
  display: flex;
  align-items: center;
}

.footer-text {
  font-size: 11px;
  font-family: 'Noto Serif SC', serif;
  letter-spacing: 1px;
}
</style>
