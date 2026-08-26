<script setup lang="ts">
/**
 * 收件处理设置卡：总开关/owner 邮箱/处理模式/白名单/回信通道/每日上限/免打扰。
 * config 对象经 props 注入（同一引用，字段直接双向绑定——与拆分前共享 ref 语义一致，
 * 深度变更由视图层 useMailSettings 的 deep watch 捕获并自动保存）。
 */
import { computed } from 'vue'
import { NInput, NRadioButton, NRadioGroup, NSelect, NSlider, NSpin, NSwitch } from 'naive-ui'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import type { MailConfig } from './types'

const props = defineProps<{
  config: MailConfig
  loading: boolean
  saving: boolean
}>()

// 免打扰小时选项（0-23，起止相同=不启用）
const dndHourOptions = Array.from({ length: 24 }, (_, i) => ({
  label: `${String(i).padStart(2, '0')}:00`,
  value: i,
}))

const modeDesc = computed(() => {
  switch (props.config.mode) {
    case 'off': return t('mailView.modeOffDesc')
    case 'allowlist': return t('mailView.modeAllowlistDesc')
    case 'all': return t('mailView.modeAllDesc')
    default: return ''
  }
})

const channelDesc = computed(() => {
  return props.config.reply_channel === 'mail_and_qq'
    ? t('mailView.channelMailQQDesc')
    : t('mailView.channelMailDesc')
})
</script>

<template>
  <Tilt3D :max-x="4" :max-y="6"><section class="glass-panel section animate-slide-up">
    <h3>{{ t('mailView.configCard') }}</h3>
    <n-spin :show="loading">
      <div class="cfg-body">
        <div class="setting-row">
          <div class="row-label">
            <span class="s-label">{{ t('mailView.masterSwitch') }}</span>
            <span class="row-desc">{{ t('mailView.masterSwitchDesc') }}</span>
          </div>
          <n-switch v-model:value="config.enabled" />
        </div>

        <div class="setting-row">
          <div class="row-label">
            <span class="s-label">{{ t('mailView.ownerEmail') }}</span>
            <span class="perm-desc">{{ t('mailView.ownerEmailDesc') }}</span>
          </div>
          <n-input
            v-model:value="config.owner_email"
            class="owner-email-input"
            :placeholder="t('mailView.ownerEmailPh')"
          />
        </div>

        <div class="setting-row">
          <div class="row-label">
            <span class="s-label">{{ t('mailView.processMode') }}</span>
          </div>
          <n-radio-group v-model:value="config.mode">
            <n-radio-button value="off">{{ t('mailView.modeOff') }}</n-radio-button>
            <n-radio-button value="allowlist">{{ t('mailView.modeAllowlist') }}</n-radio-button>
            <n-radio-button value="all">{{ t('mailView.modeAll') }}</n-radio-button>
          </n-radio-group>
        </div>
        <p class="perm-desc">{{ modeDesc }}</p>

        <transition name="fade-slide">
          <div v-if="config.mode === 'allowlist'" class="setting-row column-row">
            <span class="s-label">{{ t('mailView.allowedSenders') }}</span>
            <n-select
              v-model:value="config.allowed_senders"
              tag
              filterable
              multiple
              :placeholder="t('mailView.allowedSendersPh')"
              :max-tag-count="8"
              class="full-width"
            />
          </div>
        </transition>

        <div class="setting-row">
          <div class="row-label">
            <span class="s-label">{{ t('mailView.replyChannel') }}</span>
          </div>
          <n-radio-group v-model:value="config.reply_channel">
            <n-radio-button value="mail">{{ t('mailView.channelMail') }}</n-radio-button>
            <n-radio-button value="mail_and_qq">{{ t('mailView.channelMailQQ') }}</n-radio-button>
          </n-radio-group>
        </div>
        <p class="perm-desc">{{ channelDesc }}</p>

        <div class="setting-row brightness-row">
          <div class="brightness-label">
            <span class="s-label">{{ t('mailView.dailyLimit') }}</span>
            <span class="brightness-value">{{ config.max_per_day }}</span>
          </div>
          <n-slider
            v-model:value="config.max_per_day"
            :min="5"
            :max="100"
            :step="1"
            :marks="{ 5: '5', 50: '50', 100: '100' }"
            class="full-width"
          />
        </div>
        <p class="brightness-hint">{{ t('mailView.dailyLimitHint') }}</p>

        <div class="setting-row">
          <div class="row-label">
            <span class="s-label">{{ t('mailView.dndPeriod') }}</span>
            <span class="row-desc">{{ t('mailView.dndPeriodDesc') }}</span>
          </div>
          <div class="dnd-pickers">
            <n-select v-model:value="config.dnd_start" :options="dndHourOptions"
                      size="small" style="width: 110px" />
            <span class="dnd-sep">~</span>
            <n-select v-model:value="config.dnd_end" :options="dndHourOptions"
                      size="small" style="width: 110px" />
          </div>
        </div>
        <p class="perm-desc">{{ t('mailView.dndHint') }}</p>

        <div class="save-row" v-if="saving">
          <span class="saving-hint">{{ t('mailView.saving') }}</span>
        </div>
      </div>
    </n-spin>
  </section></Tilt3D>
</template>

<style scoped>
.section { padding: 16px 18px; margin-bottom: 14px; }
.section h3 { font-size: 14px; color: var(--dendro); margin-bottom: 14px; }

.cfg-body { display: flex; flex-direction: column; gap: 4px; }

.setting-row {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 0; gap: 16px; flex-wrap: wrap;
}
.column-row { flex-direction: column; align-items: stretch; gap: 8px; }
.row-label { display: flex; flex-direction: column; gap: 2px; }
.row-desc { font-size: 11.5px; color: var(--moon-dim); opacity: 0.75; }
.s-label { font-size: 13.5px; }
.full-width { width: 100%; }

.perm-desc { font-size: 12.5px; color: var(--wisdom); margin: 4px 0 8px; }

/* 滑块行 */
.brightness-row { flex-direction: column; align-items: stretch; gap: 8px; }
.brightness-label { display: flex; justify-content: space-between; align-items: center; }
.brightness-value {
  font-size: 13px; color: var(--dendro);
  font-family: 'JetBrains Mono', monospace;
}
.brightness-hint { font-size: 11.5px; color: var(--moon-dim); margin: 4px 0 0; opacity: 0.7; }

.save-row { display: flex; justify-content: flex-end; padding-top: 8px; }

/* DND 时段选择器 */
.dnd-pickers { display: flex; align-items: center; gap: 8px; }
.dnd-sep { color: var(--moon-dim); font-size: 13px; }
.saving-hint {
  font-size: 12px; color: var(--moon-dim);
  font-family: 'JetBrains Mono', monospace;
  opacity: 0.8;
}

/* 过渡动画 */
.fade-slide-enter-active, .fade-slide-leave-active {
  transition: opacity 0.28s var(--ease-smooth), transform 0.28s var(--ease-smooth), max-height 0.28s var(--ease-smooth);
}
.fade-slide-enter-from, .fade-slide-leave-to {
  opacity: 0; transform: translateY(-6px); max-height: 0;
}
.fade-slide-enter-to, .fade-slide-leave-from {
  opacity: 1; max-height: 400px;
}
</style>
