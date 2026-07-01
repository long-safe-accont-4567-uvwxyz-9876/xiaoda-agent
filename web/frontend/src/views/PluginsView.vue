<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { NButton, NTag, NPopconfirm, NSpin, NInput, NEmpty, NTabs, NTabPane, useMessage } from 'naive-ui'
import { get, post } from '../api'
import { t, tf } from '../i18n'
import Tilt3D from '../components/fx/Tilt3D.vue'

const message = useMessage()
const plugins = ref<any[]>([])
const discovering = ref(false)
const testingPlugin = ref<Record<string, boolean>>({})
const pluginTestResult = ref<Record<string, any>>({})

const stateTagType = (state: string): 'default' | 'info' | 'success' | 'warning' | 'error' => {
  const map: Record<string, 'default' | 'info' | 'success' | 'warning' | 'error'> = {
    found: 'default', loaded: 'info', enabled: 'success',
    disabled: 'warning', unloaded: 'default', error: 'error',
  }
  return map[state] || 'default'
}

async function load() {
  try {
    plugins.value = await get<any[]>('/plugins')
  } catch (e: any) {
    message.error(tf('pluginsView.loadFailed', e.message))
  }
}

async function discoverPlugins() {
  discovering.value = true
  try {
    const res = await post<any>('/plugins/discover', {})
    message.success(tf('pluginsView.discovered', res.discovered?.length || 0))
    await load()
  } catch (e: any) {
    message.error(tf('pluginsView.scanFailed', e.message))
  } finally {
    discovering.value = false
  }
}

async function doAction(pluginId: string, action: string) {
  try {
    const res = await post<any>(`/plugins/${pluginId}/${action}`, {})
    if (res.status === 'ok') {
      message.success(tf('pluginsView.actionSuccess', action))
    } else {
      message.error(tf('pluginsView.actionFailed', action))
    }
    await load()
  } catch (e: any) {
    message.error(tf('pluginsView.actionFailedErr', action, e.message))
  }
}

async function testPlugin(pluginId: string) {
  testingPlugin.value[pluginId] = true
  pluginTestResult.value[pluginId] = null
  try {
    // 测试插件是否能正常加载（通过 load action 验证）
    const res = await post<any>(`/plugins/${pluginId}/load`, {})
    if (res.status === 'ok') {
      pluginTestResult.value[pluginId] = { ok: true, message: t('pluginsView.pluginNormal') }
      message.success(tf('pluginsView.pluginTestPassed', pluginId))
    } else {
      pluginTestResult.value[pluginId] = { ok: false, message: res.detail || t('pluginsView.loadFailed') }
      message.error(tf('pluginsView.pluginTestFailed', pluginId))
    }
    await load()
  } catch (e: any) {
    pluginTestResult.value[pluginId] = { ok: false, message: e.message }
    message.error(tf('pluginsView.pluginTestFailedErr', e.message))
  } finally {
    testingPlugin.value[pluginId] = false
  }
}

// ── 插件市场 ──────────────────────────────────────────────
const marketItems = ref<any[]>([])
const marketLoading = ref(false)
const marketSearch = ref('')
const installingMarket = ref<Record<string, boolean>>({})
const uninstallingMarket = ref<Record<string, boolean>>({})
const testingMarketPlugin = ref<Record<string, boolean>>({})
const marketPluginTestResult = ref<Record<string, any>>({})

const filteredMarket = computed(() => {
  if (!marketSearch.value.trim()) return marketItems.value
  const q = marketSearch.value.toLowerCase()
  return marketItems.value.filter((i: any) =>
    i.name.toLowerCase().includes(q) ||
    i.description.toLowerCase().includes(q) ||
    (i.tags || []).some((t: string) => t.toLowerCase().includes(q))
  )
})

async function loadMarket(force = false) {
  marketLoading.value = true
  try {
    const data = await get<any>(`/market/plugins${force ? '?force=true' : ''}`)
    marketItems.value = data.items || []
  } catch { /* 静默失败 */ } finally {
    marketLoading.value = false
  }
}

async function installFromMarket(item: any) {
  installingMarket.value[item.id] = true
  try {
    await post('/market/plugins/install', {
      item_id: item.id,
      download_url: item.download_url,
      version: item.version,
      sha256: item.sha256,
    })
    message.success(tf('pluginsView.installSuccess', item.name))
    await loadMarket()
    await load()
  } catch (e: any) {
    message.error(tf('pluginsView.installFailed', e.message))
  } finally {
    installingMarket.value[item.id] = false
  }
}

async function uninstallFromMarket(item: any) {
  uninstallingMarket.value[item.id] = true
  try {
    await post('/market/plugins/uninstall', { item_id: item.id })
    message.success(tf('pluginsView.uninstalled', item.name))
    await loadMarket()
    await load()
  } catch (e: any) {
    message.error(tf('pluginsView.uninstallFailed', e.message))
  } finally {
    uninstallingMarket.value[item.id] = false
  }
}

async function testMarketPlugin(item: any) {
  testingMarketPlugin.value[item.id] = true
  marketPluginTestResult.value[item.id] = null
  try {
    // 通过重新加载插件来验证
    const res = await post<any>(`/plugins/${item.id}/load`, {})
    if (res.status === 'ok') {
      marketPluginTestResult.value[item.id] = { ok: true, message: t('pluginsView.pluginNormal') }
      message.success(tf('pluginsView.pluginTestPassed', item.name))
    } else {
      marketPluginTestResult.value[item.id] = { ok: false, message: res.detail || t('pluginsView.loadFailed') }
      message.error(tf('pluginsView.pluginTestFailed', item.name))
    }
    await loadMarket()
    await load()
  } catch (e: any) {
    marketPluginTestResult.value[item.id] = { ok: false, message: e.message }
    message.error(tf('pluginsView.testFailed', e.message))
  } finally {
    testingMarketPlugin.value[item.id] = false
  }
}

// ── Tab 切换时按需加载 ────────────────────────────────────
const activeTab = ref('installed')

function onTabChange(name: string | number) {
  if (name === 'market' && marketItems.value.length === 0) loadMarket()
}

onMounted(() => { load(); loadMarket() })
</script>

<template>
  <div class="plugins-view">
    <div class="view-header">
      <h2>🧩 {{ t('pluginsView.title') }}</h2>
      <n-button type="primary" :loading="discovering" @click="discoverPlugins">🔍 {{ t('pluginsView.scan') }}</n-button>
    </div>

    <n-tabs v-model:value="activeTab" type="line" @update:value="onTabChange">
      <!-- ── 已安装 ──────────────────────────────────────── -->
      <n-tab-pane name="installed" :tab="t('installed')">
        <p class="plugins-hint">
          {{ t('pluginsView.scanHint') }}
        </p>

        <div class="plugin-grid">
          <Tilt3D v-for="p in plugins" :key="p.id">
          <div class="plugin-card glass-panel glass-panel-hover">
            <div class="plugin-head">
              <span class="plugin-name">{{ p.name }}</span>
              <n-tag size="small" :type="stateTagType(p.state)" :bordered="false">{{ p.state }}</n-tag>
              <span class="plugin-ver">v{{ p.version }}</span>
            </div>
            <div class="plugin-desc">{{ p.description }}</div>
            <div v-if="p.error_message" class="plugin-error">{{ p.error_message }}</div>
            <div class="plugin-ops">
              <n-button v-if="p.state === 'found'" size="tiny" type="primary" secondary
                        @click="doAction(p.id, 'load')">{{ t('pluginsView.load') }}</n-button>
              <n-button v-if="p.state === 'loaded' || p.state === 'disabled'" size="tiny" type="primary"
                        @click="doAction(p.id, 'enable')">{{ t('pluginsView.enable') }}</n-button>
              <n-button v-if="p.state === 'enabled'" size="tiny" type="warning"
                        @click="doAction(p.id, 'disable')">{{ t('pluginsView.disable') }}</n-button>
              <n-button v-if="p.state === 'enabled'" size="tiny"
                        @click="doAction(p.id, 'reload')">{{ t('pluginsView.reload') }}</n-button>
              <n-button size="tiny" :type="pluginTestResult[p.id]?.ok === false ? 'error' : 'success'"
                        :loading="testingPlugin[p.id]" @click="testPlugin(p.id)">
                {{ pluginTestResult[p.id]?.ok === false ? t('pluginsView.retry') : t('pluginsView.test') }}
              </n-button>
              <n-popconfirm v-if="['loaded','disabled','error'].includes(p.state)"
                            @positive-click="doAction(p.id, 'unload')">
                <template #trigger>
                  <n-button size="tiny" type="error" quaternary>{{ t('uninstall') }}</n-button>
                </template>
                {{ t('pluginsView.confirmUninstall') }}「{{ p.name }}」？
              </n-popconfirm>
            </div>
            <div v-if="pluginTestResult[p.id]" class="plugin-test-result"
                 :class="pluginTestResult[p.id].ok ? 'test-ok' : 'test-fail'">
              {{ pluginTestResult[p.id].ok ? '✓ ' + t('pluginsView.testPass') : '✕ ' + pluginTestResult[p.id].message }}
            </div>
          </div>
          </Tilt3D>

          <div v-if="!plugins.length" class="empty-state glass-panel">
            <p>{{ t('pluginsView.noPlugins') }}</p>
          </div>
        </div>
      </n-tab-pane>

      <!-- ── 插件市场 ──────────────────────────────────────── -->
      <n-tab-pane name="market" :tab="t('pluginsView.market')">
        <div class="market-toolbar">
          <n-input v-model:value="marketSearch" :placeholder="t('pluginsView.searchPlaceholder')" clearable
                   size="small" style="width: 200px" />
          <n-button size="small" :loading="marketLoading" @click="loadMarket(true)">{{ t('refresh') }}</n-button>
        </div>
        <p class="market-hint">{{ t('pluginsView.marketDesc') }}</p>

        <n-spin :show="marketLoading">
          <div class="market-grid">
            <Tilt3D v-for="item in filteredMarket" :key="item.id">
            <div class="market-card glass-panel glass-panel-hover">
              <div class="card-head">
                <span class="card-icon">{{ item.icon || '🧩' }}</span>
                <div class="card-title-group">
                  <span class="card-name">{{ item.name }}</span>
                  <div class="card-meta">
                    <span class="card-version">v{{ item.version }}</span>
                    <span v-if="item.author" class="card-author">{{ item.author }}</span>
                  </div>
                </div>
              </div>
              <div class="card-desc">{{ item.description }}</div>
              <div v-if="item.tags?.length" class="card-tags">
                <n-tag v-for="tag in item.tags" :key="tag" size="tiny" :bordered="false" round>{{ tag }}</n-tag>
              </div>
              <div class="card-footer">
                <n-tag v-if="item.installed" size="tiny" type="success" :bordered="false">
                  {{ t('pluginsView.installedVersion') }} v{{ item.installed_version }}
                </n-tag>
                <span v-else></span>
                <div class="card-actions">
                  <n-button v-if="item.installed" size="tiny"
                            :loading="testingMarketPlugin[item.id]"
                            :type="marketPluginTestResult[item.id]?.ok ? 'success' : 'default'"
                            @click="testMarketPlugin(item)">
                    {{ marketPluginTestResult[item.id]?.ok ? t('pluginsView.testPass') : t('pluginsView.test') }}
                  </n-button>
                  <n-popconfirm v-if="item.installed"
                                @positive-click="uninstallFromMarket(item)">
                    <template #trigger>
                      <n-button size="tiny" type="error" quaternary
                                :loading="uninstallingMarket[item.id]">{{ t('uninstall') }}</n-button>
                    </template>
                    {{ t('pluginsView.confirmUninstall') }}「{{ item.name }}」？
                  </n-popconfirm>
                  <n-button size="tiny" type="primary"
                            :loading="installingMarket[item.id]"
                            @click="installFromMarket(item)">
                    {{ item.installed ? t('update') : t('install') }}
                  </n-button>
                </div>
              </div>
            </div>
            </Tilt3D>
            <n-empty v-if="!marketLoading && filteredMarket.length === 0"
                     :description="t('pluginsView.marketEmpty')" class="empty-state" />
          </div>
        </n-spin>
      </n-tab-pane>

    </n-tabs>
  </div>
</template>

<style scoped>
.view-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.view-header h2 { font-family: 'Noto Serif SC', serif; }
.plugins-hint { font-size: 12.5px; color: var(--moon-dim); margin-bottom: 14px; }

.plugin-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 14px;
}

.plugin-card { padding: 14px 16px; }
.plugin-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.plugin-name { font-weight: 600; font-size: 15px; }
.plugin-ver { font-size: 12px; color: var(--moon-dim); }

.plugin-desc {
  font-size: 13px; color: var(--moon-dim);
  margin-bottom: 8px;
}

.plugin-error {
  font-size: 12px; color: var(--alert);
  background: rgba(217, 106, 95, 0.08);
  border-radius: 6px; padding: 4px 8px; margin-bottom: 8px;
}

.plugin-ops { display: flex; gap: 6px; flex-wrap: wrap; }
.plugin-test-result { font-size: 11.5px; margin-top: 6px; padding: 4px 8px; border-radius: 4px; }
.plugin-test-result.test-ok { color: var(--dendro); background: rgba(76,175,80,0.08); }
.plugin-test-result.test-fail { color: #e74c3c; background: rgba(231,76,60,0.08); }

.empty-state { padding: 40px; text-align: center; color: var(--moon-dim); grid-column: 1 / -1; }

/* ── 市场通用 ─────────────────────────────────────────── */
.market-toolbar {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 8px; padding-top: 4px;
}
.market-hint { font-size: 12.5px; color: var(--moon-dim); margin-bottom: 12px; }

.market-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}
.market-card { padding: 12px 14px; }
.card-head { display: flex; align-items: flex-start; gap: 8px; margin-bottom: 6px; }
.card-icon { font-size: 24px; flex-shrink: 0; line-height: 1; }
.card-title-group { flex: 1; min-width: 0; }
.card-name { font-weight: 600; font-size: 14px; display: block; }
.card-meta { display: flex; align-items: center; gap: 6px; margin-top: 1px; }
.card-version { font-size: 11px; color: var(--moon-dim); }
.card-author { font-size: 11px; color: var(--moon-dim); }
.card-desc {
  font-size: 12.5px; color: var(--moon-dim); margin-bottom: 6px;
  display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden;
}
.card-tags { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 6px; }
.card-footer { display: flex; align-items: center; justify-content: space-between; }
.card-actions { display: flex; gap: 6px; }
</style>
