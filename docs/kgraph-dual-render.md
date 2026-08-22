# 知识图谱双渲染评估（echarts 2D 面板 / UniverseGraph 3D 全屏）

> 2026-08-22 技术债批 4 评估：**保留双模式**，二者互补而非维护分叉；
> 本轮落地整改 = 3D 全屏改按需加载（defineAsyncComponent）。

## 1. 现状

InsightView.vue 的知识图谱 tab 下存在两个渲染实现：

| 模式 | 技术 | 入口 | 交互 |
|---|---|---|---|
| 2D 面板 | echarts `type:'graph'` force | tab 内默认视图 | 实体/关系增删改、聚焦、按深度扩展 |
| 3D 全屏 | 3d-force-graph + three | 「全屏」按钮打开 n-modal | 3D 漫游、点击节点按 depth=1 扩展 |

## 2. 评估结论：不合并

- **两模式互补，非同一组件的两份维护实现**：2D 侧重管理（CRUD），3D 侧重空间
  化探索；删除任一都会丢失能力。
- **数据层已统一**：两者都调用同一 `getKnowledgeGraph()` API，扩展逻辑各自独立
  （每次一跳），无数据源漂移。
- **状态已贯通**：打开 3D 时 `:entity="graphEntity" :depth="graphDepth"` 直接继承
  面板当前聚焦实体与深度，无需另设共享 store。
- **3D 性能前置评估**：见 `docs/rust-hybrid-poc.md`——当前规模远未触发 WebGL/WASM
  下沉门槛（31-300 节点，60fps 预算余量 3-18 倍），性能路线已归档。

## 3. 本轮落地：3D 独立 chunk（懒加载）

改动前 `UniverseGraph` 静态 import，`3d-force-graph`/`three` 及全部依赖常驻主包，
即使用户从不打开 3D 也在首屏加载。改为
`defineAsyncComponent(() => import('.../UniverseGraph.vue'))` 后：

- 主包移除 WebGL 渲染栈，Insight tab 首屏体积下降；
- 首次打开 3D 才有短暂按需加载，功能与行为完全一致。

验证：`npm run typecheck` 通过；生产构建产物中 UniverseGraph 单独成 chunk。

## 4. 遗留待决策（非当前必要）

若未来想彻底消灭双引擎：把 2D/3D 统一为同一数据源的两种渲染器，或让 3D 成为
2D 的懒加载插件式扩展；两种都需产品取舍，当前不动作。