<script setup lang="ts">
/**
 * 知识图谱 Tab：力导图画布 + 检索工具栏 + 实体/关系列表 + 3D 全屏入口。
 *
 * 职责边界（2026-08-23 大文件拆分专项）：
 * - 数据编排（搜索/展开合并/删除）留在视图层，本组件只负责渲染与交互上抛；
 *   视图在数据变更后调用 expose 的 render()（与原 renderKnowledge 显式调用点一一对应）
 * - 挂载即 emit('load')：n-tab-pane displayDirective 默认 'if'，每次切入本页重新挂载，
 *   原 activeTab watcher 的 nextTick+100ms 时序原样保留
 * - 3D 宇宙视图按需加载（WebGL 依赖不进主包），showUniverse 状态归本组件
 */
import { defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { NButton, NInput, NInputNumber, NModal, NPopconfirm, NTag, useMessage } from 'naive-ui'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import SumeruIcon from '../fx/SumeruIcon.vue'
import Tilt3D from '../fx/Tilt3D.vue'
import { t } from '../../i18n'
import type { EntityItem, KgEdge2D, KgNode2D, RelationItem } from './types'

echarts.use([GraphChart, TooltipComponent, CanvasRenderer])

const UniverseGraph = defineAsyncComponent(
  () => import('../knowledge/UniverseGraph.vue'),
)

const props = defineProps<{
  entities: EntityItem[]
  relations: RelationItem[]
  nodes: KgNode2D[]
  edges: KgEdge2D[]
  /** 已展开邻域的节点集合（描边标记） */
  expandedIds: Set<string>
  expandingNode: string
  entity: string
  depth: number
}>()

const emit = defineEmits<{
  (e: 'load'): void
  (e: 'update:entity', v: string): void
  (e: 'update:depth', v: number): void
  (e: 'expand', name: string): void
  (e: 'add-entity'): void
  (e: 'add-relation'): void
  (e: 'edit-entity', item: EntityItem): void
  (e: 'edit-relation', item: RelationItem): void
  (e: 'delete-entity', name: string): void
  (e: 'delete-relation', id: string): void
}>()

const message = useMessage()
const graphEl = ref<HTMLElement | null>(null)
const showUniverse = ref(false)
let knowledgeChart: echarts.ECharts | null = null

// ── 挂载即拉取（原 activeTab watcher 的 nextTick+100ms 时序）──
onMounted(() => {
  window.addEventListener('resize', handleResize)
  nextTick(() => {
    setTimeout(() => emit('load'), 100)
  })
})

// ── 渲染（原 InsightView.renderKnowledge 原样迁移）──
// 全量重绘：echarts force 图 setOption 增量挂边会破坏已固定节点坐标，
// dispose + init + setOption 并保留拖拽固定语义。
async function render() {
  try {
    await nextTick()
    if (!graphEl.value) return
    if (knowledgeChart) { knowledgeChart.dispose() }
    knowledgeChart = echarts.init(graphEl.value)

    // 按节点对分组，计算每条边的独立曲率，避免重叠
    const pairKey = (a: string, b: string) => [a, b].sort().join('||')
    const pairCount: Record<string, number> = {}
    const pairIdx: Record<string, number> = {}
    for (const e of props.edges) {
      const k = pairKey(e.from, e.to)
      pairCount[k] = (pairCount[k] || 0) + 1
    }

    const links = props.edges.map((e) => {
      const k = pairKey(e.from, e.to)
      const total = pairCount[k]
      const idx = pairIdx[k] || 0
      pairIdx[k] = idx + 1
      // 单条边用小曲率，多条边均匀展开
      let curveness: number
      if (total === 1) {
        curveness = 0.1
      } else {
        // 均匀分布在 -0.4 ~ 0.4 之间
        curveness = -0.4 + (idx / (total - 1)) * 0.8
      }
      return {
        source: e.from,
        target: e.to,
        relation: e.relation,
        lineStyle: { curveness },
      }
    })

    // 力导向布局，拖拽后固定
    // 渲染性能分级：>150 节点关边标签（edgeLabel 是 force 图最大开销），
    // >400 节点再关节点标签，避免大图一次 setOption 卡死主线程
    const nodeTotal = props.nodes.length
    const showEdgeLabel = nodeTotal <= 150
    const showNodeLabel = nodeTotal <= 400
    const symbolSize = nodeTotal > 300 ? 14 : 26

    const nodeData = props.nodes.map(n => ({
      name: n.name,
      value: n.kind ?? n.value,
      kind: n.kind,
      symbolSize,
      label: { show: showNodeLabel },
      // 已展开节点用描边标记，提示用户该邻域已加载
      itemStyle: props.expandedIds.has(n.name)
        ? { color: '#7fd650', borderColor: '#fbbf24', borderWidth: 2 }
        : { color: '#7fd650' },
    }))

    knowledgeChart.setOption({
      tooltip: {
        triggerOn: 'click',
        formatter: (p: any) => {
          if (p.dataType === 'node') {
            return `<b>${p.data.name}</b><br/>${t('insightView.typeName')} ${p.data.value || ''}`
          }
          if (p.dataType === 'edge') {
            return `${p.data.source} → <b>${p.data.relation}</b> → ${p.data.target}`
          }
          return ''
        },
      },
      series: [{
        type: 'graph', layout: 'force', roam: true, draggable: true,
        force: { repulsion: 260, edgeLength: 120, gravity: 0.08, friction: 0.32 },
        label: { show: showNodeLabel, color: '#f2f7ee', fontSize: 11 },
        edgeLabel: {
          show: showEdgeLabel, fontSize: 9, color: '#e8d5a3',
          formatter: (p: any) => p.data.relation || '',
        },
        // 大图关闭逐帧动画重排：layoutAnimation=false 一次算完，避免长时间掉帧
        layoutAnimation: nodeTotal <= 200,
        itemStyle: { color: '#7fd650' },
        lineStyle: { color: 'rgba(232, 213, 163, 0.5)' },
        emphasis: { disabled: true },
        select: {
          focus: 'adjacency',
          lineStyle: { width: 3, color: '#fbbf24' },
          label: { fontSize: 14 },
          itemStyle: { shadowBlur: 10, shadowColor: '#fbbf24' },
        },
        data: nodeData,
        links,
      }],
    })

    // 单击节点：增量展开该节点邻域（按需加载核心交互；去重守卫在视图层 expandNode）
    knowledgeChart.on('click', (params: any) => {
      if (params.dataType === 'node' && params.data?.name) {
        emit('expand', params.data.name)
      }
    })

    // 拖拽松手后固定节点，不弹回
    knowledgeChart.on('mouseup', (params: any) => {
      if (params.dataType === 'node' && params.data) {
        const opt = knowledgeChart?.getOption() as any
        if (opt?.series?.[0]?.data) {
          const sData = opt.series[0].data.map((d: any) => {
            if (d.name === params.data.name) {
              return { ...d, fixed: true, x: params.data.x, y: params.data.y }
            }
            return d
          })
          knowledgeChart?.setOption({ series: [{ data: sData }] }, false)
        }
      }
    })
  } catch (e: any) { message.error(e.message) }
}

defineExpose({ render })

function onDepthInput(v: number | null) {
  // 输入框清空时归一为当前上限（深度恒有值，下游无需判空）
  emit('update:depth', v ?? 6)
}

let resizeTimer: ReturnType<typeof setTimeout> | null = null
function handleResize() {
  if (resizeTimer) clearTimeout(resizeTimer)
  resizeTimer = setTimeout(() => {
    knowledgeChart?.resize()
  }, 200)
}

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  if (resizeTimer) clearTimeout(resizeTimer)
  knowledgeChart?.dispose(); knowledgeChart = null
})
</script>

<template>
  <div>
    <div class="glass-panel chart-box">
      <div class="kg-toolbar">
        <n-input :value="entity" :placeholder="t('insightView.entityFocusPh')" size="small"
                 style="max-width: 200px" @update:value="emit('update:entity', $event)"
                 @keydown.enter="emit('load')" />
        <div class="kg-depth-input" :title="t('insightView.depthHint')">
          <span class="kg-depth-label">{{ t('insightView.depthLabel') }}</span>
          <n-input-number
            :value="depth"
            size="small"
            :min="1"
            :max="12"
            :show-button="false"
            placeholder="6"
            style="width: 64px"
            @update:value="onDepthInput"
          />
        </div>
        <span v-if="expandingNode" class="kg-expanding">{{ t('insightView.expanding') }}「{{ expandingNode }}」…</span>
        <n-button size="tiny" type="primary" @click="emit('add-entity')"><SumeruIcon name="plus" :size="12" variant="duo" tone="add" interactive /> {{ t('insightView.addEntity') }}</n-button>
        <n-button size="tiny" type="primary" @click="emit('add-relation')"><SumeruIcon name="plus" :size="12" variant="duo" tone="add" interactive /> {{ t('insightView.addRelation') }}</n-button>
        <n-button size="tiny" type="primary" @click="showUniverse = true"><SumeruIcon name="sparkle" :size="12" variant="duo" tone="magic" interactive /> {{ t('insightView.fullscreen') }}</n-button>
      </div>
      <div ref="graphEl" class="chart tall"></div>
    </div>
    <div class="kg-lists">
      <div class="kg-section">
        <h4>{{ t('insightView.entitiesLabel') }} ({{ entities.length }})</h4>
        <div class="item-list">
          <Tilt3D v-for="e in entities" :key="e.name"><div class="list-row glass-panel">
            <n-tag size="tiny" :bordered="false" v-if="e.kind">{{ e.kind }}</n-tag>
            <span class="note-content">{{ e.name }}</span>
            <n-button size="tiny" quaternary @click="emit('edit-entity', e)">{{ t('insightView.edit') }}</n-button>
            <n-popconfirm @positive-click="emit('delete-entity', e.name)">
              <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
              {{ t('insightView.deleteEntityConfirm') }}
            </n-popconfirm>
          </div></Tilt3D>
          <div v-if="!entities.length" class="empty-state"><p>{{ t('insightView.noEntities') }}</p></div>
        </div>
      </div>
      <div class="kg-section">
        <h4>{{ t('insightView.relationsLabel') }} ({{ relations.length }})</h4>
        <div class="item-list">
          <Tilt3D v-for="r in relations" :key="r.id"><div class="list-row glass-panel">
            <span class="kg-rel-from">{{ r.from_entity }}</span>
            <n-tag size="tiny" type="info" :bordered="false">{{ r.relation_type }}</n-tag>
            <span class="kg-rel-to">{{ r.to_entity }}</span>
            <n-button size="tiny" quaternary @click="emit('edit-relation', r)">{{ t('insightView.edit') }}</n-button>
            <n-popconfirm @positive-click="emit('delete-relation', String(r.id))">
              <template #trigger><n-button size="tiny" type="error" quaternary>{{ t('insightView.delete') }}</n-button></template>
              {{ t('insightView.deleteRelationConfirm') }}
            </n-popconfirm>
          </div></Tilt3D>
          <div v-if="!relations.length" class="empty-state"><p>{{ t('insightView.noRelations') }}</p></div>
        </div>
      </div>
    </div>

    <!-- 纳西妲宇宙 3D 全屏图谱 -->
    <n-modal
      v-model:show="showUniverse"
      :trap-focus="false"
      :close-on-esc="true"
      :mask-closable="true"
      :show-mask="false"
      display-directive="show"
      style="width:100vw;height:100vh;max-width:none;max-height:none"
    >
      <UniverseGraph :entity="entity" :depth="depth" @close="showUniverse = false" />
    </n-modal>
  </div>
</template>

<style scoped>
.chart-box { padding: 14px 16px; }
.chart-box h4 { font-size: 13px; color: var(--dendro); margin-bottom: 8px; }
.chart.tall { height: 380px; }

.kg-toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }

.kg-depth-input {
  display: flex;
  align-items: center;
  gap: 6px;
}

.kg-depth-label {
  font-size: 12px;
  color: var(--moon-dim);
  white-space: nowrap;
}

.kg-expanding {
  font-size: 11px;
  color: var(--wisdom);
  animation: kg-pulse 1.2s ease-in-out infinite;
}

@keyframes kg-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.45; }
}

.item-list { display: flex; flex-direction: column; gap: 6px; }
.list-row { display: flex; align-items: center; gap: 10px; padding: 8px 14px; font-size: 13px; }
.note-content { flex: 1; }

.empty-state { padding: 30px; text-align: center; color: var(--moon-dim); }

.kg-lists { display: flex; gap: 14px; margin-top: 14px; flex-wrap: wrap; }
.kg-section { flex: 1; min-width: 300px; }
.kg-section h4 { font-size: 13px; color: var(--dendro); margin-bottom: 8px; }
.kg-rel-from, .kg-rel-to { font-size: 12px; color: var(--moon); }
.kg-rel-from::after { content: ' →'; color: var(--wisdom); margin: 0 4px; }
</style>
