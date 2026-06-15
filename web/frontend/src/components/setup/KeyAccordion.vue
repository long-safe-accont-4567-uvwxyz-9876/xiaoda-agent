<script setup lang="ts">
import { ref } from 'vue'

export interface KeyItem {
  key: string
  label: string
  desc: string
  url: string
  url_desc: string
  required: boolean
  configured: boolean
  masked_value: string
}

export type TestStatus = 'untested' | 'testing' | 'passed' | 'failed'

const props = defineProps<{
  items: KeyItem[]
  testStatuses: Record<string, TestStatus>
  testMessages: Record<string, string>
}>()

const emit = defineEmits<{
  update: [key: string, value: string]
  test: [key: string]
}>()

const expandedKeys = ref<Set<string>>(new Set())

function toggle(key: string) {
  if (expandedKeys.value.has(key)) {
    expandedKeys.value.delete(key)
  } else {
    expandedKeys.value.add(key)
  }
}

function isExpanded(key: string): boolean {
  return expandedKeys.value.has(key)
}

const inputValues = ref<Record<string, string>>({})

function onInput(key: string, value: string) {
  inputValues.value[key] = value
  emit('update', key, value)
}

function onTest(key: string) {
  emit('test', key)
}
</script>

<template>
  <div class="key-accordion">
    <div
      v-for="item in items"
      :key="item.key"
      class="accordion-item glass-panel"
      :class="{ 'is-expanded': isExpanded(item.key) }"
    >
      <!-- 标题行 -->
      <div class="accordion-header" @click="toggle(item.key)">
        <span class="tag" :class="item.required ? 'tag-required' : 'tag-optional'">
          {{ item.required ? '必填' : '选填' }}
        </span>
        <span class="key-name">{{ item.key }}</span>
        <span class="key-label">{{ item.label }}</span>
        <!-- 测试状态图标 -->
        <span
          class="test-status-icon"
          :class="{
            'status-untested': testStatuses[item.key] === 'untested' || !testStatuses[item.key],
            'status-testing': testStatuses[item.key] === 'testing',
            'status-passed': testStatuses[item.key] === 'passed',
            'status-failed': testStatuses[item.key] === 'failed',
          }"
          :title="testMessages[item.key] || ''"
        >
          <template v-if="!testStatuses[item.key] || testStatuses[item.key] === 'untested'">
            <span class="circle-gray"></span>
          </template>
          <template v-else-if="testStatuses[item.key] === 'testing'">
            <span class="spinner"></span>
          </template>
          <template v-else-if="testStatuses[item.key] === 'passed'">
            ✓
          </template>
          <template v-else-if="testStatuses[item.key] === 'failed'">
            ✗
          </template>
        </span>
        <span class="arrow" :class="{ 'arrow-open': isExpanded(item.key) }">❯</span>
      </div>

      <!-- 展开内容 -->
      <Transition name="accordion">
        <div v-if="isExpanded(item.key)" class="accordion-body">
          <p class="item-desc">{{ item.desc }}</p>
          <div class="item-url">
            <span class="url-label">获取地址：</span>
            <a :href="item.url" target="_blank" rel="noopener" class="url-link">{{ item.url }}</a>
          </div>
          <p class="item-steps">{{ item.url_desc }}</p>
          <div class="input-row">
            <input
              class="dendro-input"
              :placeholder="'请输入 ' + item.key"
              :value="inputValues[item.key] ?? ''"
              @input="onInput(item.key, ($event.target as HTMLInputElement).value)"
            />
          </div>
          <div class="action-row">
            <button
              class="dendro-btn test-btn"
              :disabled="!inputValues[item.key] || testStatuses[item.key] === 'testing'"
              @click.stop="onTest(item.key)"
            >
              {{ testStatuses[item.key] === 'testing' ? '测试中…' : '测试' }}
            </button>
          </div>
          <p v-if="item.configured && item.masked_value" class="current-value">
            当前值：{{ item.masked_value }}
          </p>
          <p v-if="testStatuses[item.key] === 'failed' && testMessages[item.key]" class="test-error-text">
            {{ testMessages[item.key] }}
          </p>
          <p v-if="testStatuses[item.key] === 'passed'" class="test-success-text">
            测试通过
          </p>
        </div>
      </Transition>
    </div>
  </div>
</template>

<style scoped>
.key-accordion {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.accordion-item {
  overflow: hidden;
  transition: border-color 0.3s;
}

.accordion-item.is-expanded {
  border-color: rgba(127, 214, 80, 0.35);
  box-shadow: var(--shadow-glow);
}

/* 标题行 */
.accordion-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
  transition: background 0.2s;
}

.accordion-header:hover {
  background: rgba(127, 214, 80, 0.06);
}

.tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 600;
  flex-shrink: 0;
}

.tag-required {
  color: var(--wisdom);
  background: rgba(232, 213, 163, 0.15);
}

.tag-optional {
  color: cyan;
  background: rgba(0, 255, 255, 0.1);
}

.key-name {
  color: var(--dendro);
  font-family: 'Courier New', monospace;
  font-size: 13px;
  font-weight: 600;
  flex-shrink: 0;
}

.key-label {
  color: var(--moon);
  font-size: 14px;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 测试状态图标 */
.test-status-icon {
  font-size: 14px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
}

.test-status-icon.status-untested {
  color: #888;
}

.test-status-icon.status-testing {
  color: var(--dendro);
}

.test-status-icon.status-passed {
  color: #4ade80;
  text-shadow: 0 0 6px rgba(74, 222, 128, 0.5);
}

.test-status-icon.status-failed {
  color: #f87171;
  text-shadow: 0 0 6px rgba(248, 113, 113, 0.5);
}

.circle-gray {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #666;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid rgba(127, 214, 80, 0.3);
  border-top-color: var(--dendro);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

.arrow {
  color: var(--dendro);
  font-size: 12px;
  transition: transform 0.3s var(--ease-smooth);
  flex-shrink: 0;
}

.arrow-open {
  transform: rotate(90deg);
}

/* 展开内容 */
.accordion-body {
  padding: 0 16px 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.item-desc {
  color: var(--wisdom);
  font-size: 13px;
  margin: 0;
  line-height: 1.6;
}

.item-url {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}

.url-label {
  color: var(--moon-dim);
}

.url-link {
  color: var(--dendro);
  text-decoration: underline;
  text-underline-offset: 2px;
  word-break: break-all;
}

.url-link:hover {
  color: #a0e87a;
}

.item-steps {
  color: var(--moon-dim);
  font-size: 12px;
  margin: 0;
  line-height: 1.5;
}

.input-row {
  margin-top: 4px;
}

.input-row .dendro-input {
  width: 100%;
  box-sizing: border-box;
}

.action-row {
  display: flex;
  gap: 8px;
  margin-top: 2px;
}

.test-btn {
  padding: 6px 16px;
  font-size: 13px;
  height: 32px;
  white-space: nowrap;
}

.test-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
}

.current-value {
  color: var(--moon-dim);
  font-size: 12px;
  margin: 0;
  font-family: 'Courier New', monospace;
}

.test-error-text {
  color: #f87171;
  font-size: 12px;
  margin: 0;
  line-height: 1.4;
}

.test-success-text {
  color: #4ade80;
  font-size: 12px;
  margin: 0;
}

/* 手风琴过渡动画 */
.accordion-enter-active,
.accordion-leave-active {
  transition: max-height 0.3s var(--ease-smooth),
              opacity 0.3s var(--ease-smooth),
              transform 0.3s var(--ease-smooth);
  overflow: hidden;
}

.accordion-enter-from,
.accordion-leave-to {
  max-height: 0;
  opacity: 0;
  transform: translateY(-8px);
}

.accordion-enter-to,
.accordion-leave-from {
  max-height: 300px;
  opacity: 1;
  transform: translateY(0);
}
</style>
