# workflow_v2 高级 DAG 节点走查（M4）

> 范围：`NodeType` 中除 START/END/TOOL/MCP/MODEL/SKILL/LEGACY_PROMPT 外的
> 高级/结构性节点在 v2 执行引擎里的状态、风险与决策。日期 2026-08-22。

## 结论矩阵

| 节点类型 | 执行语义 | 状态 | 说明 |
|---|---|---|---|
| `TRANSFORM` | 直通（输出 note） | ✅ 已实现 | v1 "step" 节点 = 操作说明，不调能力层 |
| `APPROVAL` / `REVIEW` | **人工审批闸门** | ✅ M4 已实现 | 步骤置 WAITING→审批单落 `wf_review`→决策端点批准续跑/拒绝停流 |
| `DELAY` | 等待 | ⚠️ 未实现（显式失败） | 可用 `transform` + 脚本主图/外部定时补；保险是有 `UNSUPPORTED_NODE` 防静默 |
| `AGENT` | 子智能体 | ✅ M1.5 已实现（经 core.dispatcher.get_agent 委托） | `agent_ref` 主路径；无 agent_ref 时 `skill_refs`→SKILL、`model_policy`→MODEL 回退（M1 迁移兼容） |
| `CONDITION` | 条件分支 | ⚠️ 未实现（UNSUPPORTED_NODE） | 执行侧留 M4+；图校验已允许出现（不拦排布） |
| `PARALLEL` / `JOIN` | 并行/汇合 | ⚠️ 未实现 | 同上，属"校验通过即失败"的防静默护栏 |
| `WORKFLOW` | 嵌套子流 | ⚠️ 未实现 | 同 CONDITION，留给后续；本轮 REVIEW 无投入 |
| `INPUT` | 输入节点 | ⚠️ 未实现 | v1 无对应语义，映射为 TRANSFORM 即可 |

**防静默原则**：未实现节点统一走 `UNSUPPORTED_NODE` 不可变失败码落库 + 事件，
绝不悄悄跳过——这是本引擎的底线约束（见
`docs/workflow-v2-principles.md`）。AGENT 自 M1.5 起已实现，失败一律有显式码
（见下文设计纪要）。

## REVIEW 节点（本轮新增）设计纪要

- **语义**：运行到该节点即暂停等待人工决策；决策后自动续跑（DAG 语义 + 事件流）。
- **执行路径**：executor 只声明 `waiting_input`（不触碰任何能力层）→ scheduler
  建审批单 → 决策端点 CAS。重复 tick 不会重跑该步骤（compute_ready 认"已启动"）。
- **审批单** `wf_review`：review_id / run_id / node_id / attempt / title / note /
  status(pending|approved|rejected) / decided_by / decision_note / 时间线。
- **决策**：`approve` → 步骤 SUCCEEDED + run 恢复 RUNNING 自动推进；
  `reject` → 步骤 FAILED + run FAILED（code `REVIEW_REJECTED`）——审批否决即停流。
- **并发**：决策与所有写入同闸（单事务 + lock_version CAS），重复决策/终态 →
  409，不重复计分。
- **前端**：审批 UI 未做——服务端通道与端点已全，前端归后续工作。
  前端已暴露端点：`GET /api/v1/workflow-runs/{rid}/reviews`、
  `POST /api/v1/workflow-runs/{rid}/reviews/{rev}/decide`。
- **前端已收口（M5, 2026-08-22）**：WorkflowView 运行弹窗内 `waiting_input`
  运行项内嵌审批卡片（待决审批列表 + 备注输入 + 批准/拒绝 + loading + 决策后
  刷新）；审批决策即停流、运行列表即时反映。

## AGENT 节点（M1.5）设计纪要

- **三分派**：迁移产物按 config 键判定——`agent_ref` → 子智能体主路径；
  无 `agent_ref` 时 `skill_refs`（列表）→ SKILL 执行器、`model_policy`（dict）
  → MODEL 执行器（v1 老产物兼容，语义与 M1 相同）。
- **委托链路**：`build_runtime` 注入 `_subagent_loader_from_core`（核心
  dispatcher.get_agent 适配）；`chat(task, context=...)` 与 LLM 节点同款超时
  封装（asyncio.wait_for + 重试退避）。
- **失败语义**：找不到/不可用/无 loader/超时/空输出一律显式失败码
  （`AGENT_NOT_FOUND` / `AGENT_LOADER_UNAVAILABLE` / `AGENT_TIMEOUT` /
  `AGENT_EMPTY_OUTPUT`），绝不静默。
- **输出**：`{agent, text, privacy_filtered}`——隐私过滤与字幕掩码对齐 LLM 节点。

## 后续待收敛

1. `DELAY` / `PARALLEL` / `JOIN` 若有真实诉求再补执行语义（当前无诉求，不造）；
2. AGENT 已落地（M1.5）；其余未实现节点落地前一律显式失败，绝不全写静默 skip；
3. 可视化编辑里若出现未支持节点，应提示"该节点不参与执行"，由编排者改写。