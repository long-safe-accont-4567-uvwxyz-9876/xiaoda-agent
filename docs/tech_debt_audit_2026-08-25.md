# 技术债对账扫描（2026-08-25）

> 性质：**只读对账**。扫描时另一会话正在工作区活跃编辑（详见一），本轮零代码修改。
> 基线：HEAD `728dfd1d`（独立 worktree 快照实测）；critical 子集 **328 passed / 40.6s 全绿**。
> 上游文档：tech_debt_audit_2026-08-22.md（对账修订版）、giant_files_split_plan_2026-08-22.md、
> broad_except_inventory_2026-08-22.md。

## 一、在途会话观测（勿动清单）

工作区 581 文件未提交，实质是**全仓 lint 清理 + 开关统一**专项，方向正确：

- ruff：HEAD **1446 错**（I001×703 / F401×275 / W292×228 / E402×115 / F841×55 / 其余零头）
  → 在途会话已修至 ~150（剩 E402 + F841 两类无安全自动修复项），编辑期间错误数
  149→150→161 波动属中间态。
- `config_constants.py`：新增 `env_flag` / `env_optout_flag` 统一助手（opt-in allow-list /
  opt-out deny-list 双语义），`RERANKER_ENABLED` 已切换——全仓 `*_ENABLED` 解析漂移的收口。
- `web/server.py`：补回 `.env → 凭证文件同步`（自注：`915023d6` 重构时误删，实测症状
  siliconflow 凭证占位符 401）。
- `web/routers/model_discovery.py`：discover_models 缓存逻辑重构（spawn_refresh），
  `tests/test_model_discovery_cache.py` 配套在途。
- 新守卫测试 `tests/test_pipeline_call_signatures.py`（未跟踪）：AST 比对 retrieval 包
  kwarg 名漂移——防 mock 单测掩盖 TypeError，是好防线，应随批提交。

**风险提示（交由在途会话收尾时验证）**：F401 275→0 的自动删除依赖 noqa 标注正确的
re-export 契约（如 agent_dispatcher 的兼容面，契约在 test_dispatcher_split.py）；
push 前必须全集绿 + critical 328 绿。

## 二、巨型文件拆分计划对账（08-22 计划 → 08-25 现状）

| 项 | 计划时 | 现状 | 判定 |
|---|---|---|---|
| P1 web/routers/setup.py | 1630 | 1322 | 🔶 探针库已拆（`774608b7`），按向导阶段再切仍可行 |
| ~~P2 prompt_builder.py~~ | 1640 | — | ✅ 包化四子模块+门面（`ed22cc88`），契约测试守护 |
| P3 memory/_retrieval_engine.py | 2153 | — | ✅ retrieval 包六模块（`eba85d81`），pipeline.py 1069 |
| P4 qq_bot_adapter | 1819 | **2173** | 🔶 模板方法已沉基类，但文件反涨 354 行——止血棘轮已就位 |
| P4 wechat_bot_adapter | 1494 | 1582 | 🔶 同上 |
| ~~P5 ws_hub~~ | 1228 | — | ✅ 拆出 ws_terminal 534 行（`050bebd5`），hub 本体降至 1118 |
| P5 vector_store / text_utils | 1677/1198 | 1618/1151 | 维持触碰随治理 |

**08-25 晚追加拆分**：web/ws_hub.py(1591) → hub(1118)+ws_terminal(534)，
终端状态单一事实源内聚子模块，manager 反向依赖延迟导入零环；
兼容面(hub._X 引用面 tests 64 处)re-export 保持。

**测试侧肥大评估结论（§二 local_ai 四文件）**：**不合并**。域划分清晰
(device_registry=provider探测 / provider_onboarding=接入凭证 /
memory_integration=嵌入向量集成 / instances=运行时生命周期)，
跨文件重复仅假模型目录 9 处且集中单文件；合并会造出 3000+ 行真巨型文件。
轻量收尾：device_registry 内 9 处两行式提为 `_seed_fake_model` helper（`f7eb92bc`）。

**新入榜（计划外，≥1000 行）**：web/server.py 1397、agent_context.py 1345、
db/legacy_migrations.py 1289（假阳性，append-only 注册表）、core/bootstrap.py 1171、
utils/text_utils 1151、memory/_memory_encoder.py 1134、db/db_memory_reconciliation.py 1102、
llm_gateway/router_execution.py 1097、tool_engine/mcp_client.py 1090、ilink_client.py 1039、
agent_core/sub_agent_manager.py 1032、sub_agent.py 1004。

**测试侧巨型文件**：test_local_ai_device_registry 2261 / test_provider_onboarding 1715 /
test_local_ai_memory_integration 1401 / test_local_ai_instances 1235——四份 local_ai 系
合计 6.6K 行，fixture 与用例重复度值得一次合并评估（读侧共享、写侧分文件）。

前端：巨型 SFC 批次已收官（`01a6311e`：InsightView 1413→242 等）；余 600+ 行 SFC 9 个
（ChatView 962 / RetrievalView 915 / SettingsView 896 / ChatTerminal 774 / ModelsView 768 /
SetupWizardView 721 / WorkflowView 696 / PromptInput 673 / ToolsView 633），按 boy-scout。

## 三、except 棘轮

非测试源码 `except Exception` 计数 **1143 < 基线 1158**（scripts/broad_except_baseline.txt
已随检索治理上调过一次），防线健康。批次 B（memory）/ C（bootstrap/adapters）的结构化
降级字段仍未开——维持"触碰时治理"即可，不单开专项。

## 四、workflow_v2 定性修正（对 08-22 审计第四节.2 的更新）

前端**已有 v2 消费者**：`web/frontend/src/api/index.ts:251-267` 的
`/workflow-runs/{get,cancel,reviews,decide}`；工作流 CRUD 仍走 v1 `/workflows`；
双路由并挂（web/server.py:1255）；迁移脚本 `scripts/migrate_v1_workflows.py` 就绪。
**判定：不再是"零消费者待决策"，而是渐进双轨迁移态。** 建议补一份收敛 checklist：
v1 写路径冻结时点 → 存量数据迁移 → v1 路由下线，避免双轨永久化。

## 五、本轮新发现

1. **CLAUDE.md:23 数字漂移**："500+ 自动化用例" vs 实际 **4911**（`9965352e` 全集基线）。
2. **DEPRECATED_MODULES.md（07-19）过时**：`memory/fluid_memory.py` 已删仍在册；
   建议随下次清坟刷新或直接标注档案化。
3. **core/degradation.py（97 行）生产零引用**：仅 tests/test_phase1_5_modules.py 供养
   （"测试供养死代码"模式）。注意与活代码 core/degradation_strategy.py（5 处生产引用）
   是两个文件，勿混淆。→ **本轮已删除**（连同 4 个供养用例；DegradationStrategy
   在 test_degradation_strategy.py 有 25 用例独立覆盖，删除后 69 passed，见 §八）。
4. **web/routers/setup.py:342 `client_ip` 死赋值**：test-key 限流窗口是全局的、未按 IP。
   鉴权后影响小，但要么用（per-IP 窗口）要么删。
5. **E402×115 定性**：绝大多数为有意的 lazy-import（config import 副作用链、循环依赖
   规避），top 为 local_ai.py×13、setup.py×5。建议 pyproject `per-file-ignores` 承认
   现状，而不是机械上移 import（上移会触发副作用链/循环导入）。
6. **auth.py 9 处 `except as exc` 未用 exc**：logger.exception 已带栈，纯观感债
   （在途会话 WIP 已覆盖 auth.py）。

## 六、自 08-22 审计以来已消解（摘）

- 三可视化库并存 → 前端仅剩 echarts（package.json 实证）✅
- 巨型文件 P3（retrieval 包化）/ P1 首刀 / P4 基类沉淀 ✅（`eba85d81`/`774608b7`）
- 死代码清坟 ~2050 行：task_orchestrator 僵尸链、双 transport 旧栈、KLEE 残迹、v30 DROP ✅（`d5bd3cd1`）
- 前端巨型 SFC 五连拆 + types.ts 去 any ✅（`01a6311e`）
- 假门禁修复（i18n 棘轮 g 标志 / lock 对齐 / critical 单一事实源）✅（`d56e1952`）

## 七、优先级 v4

- **P0**（在途会话收尾门禁）：全集 5716 收集基线绿 + critical 328 绿 + F401 删除未破
  re-export 契约 + broad_except 棘轮复核（新增 env_flag 统一可能小幅波动计数）
- **P1**：~~prompt_builder.py 拆分提级~~ ✅ `ed22cc88`；~~ws_hub 拆分评估~~ ✅ `050bebd5`（全集 5717 全绿）
- ~~**P1**：qq_bot_adapter 止血规约~~ ✅ AST 棘轮 `beaf6fd4` 入 critical
- ~~**P2**：core/degradation.py + 供养测试删除；DEPRECATED_MODULES.md 刷新；CLAUDE.md
  用例数修正~~ ✅ 本轮已偿（见 §八）
- ~~**P2**：workflow v1/v2 双轨收敛 checklist~~ ✅ 已产出
  docs/workflow-v2-convergence-checklist.md（含易漏项：v1 保存生成的
  `skills/wf_*.md` 在 v2 接管后仍是 prompt 污染源，实存 wf_Agent 邮箱配置.md）
- **P3**：ws_hub.py 拆分评估（1228→1591，全仓涨幅最大）；四份 local_ai 测试合并评估
- 维持既有决策：web/dist 入库保留、E402 按有意 lazy-import 对待（配 per-file-ignores）

## 八、多会话归属台账（本轮）

- **本轮（对账+定点偿还）**：
  - 只读对账：本文档（/tmp 临时 worktree 已清理，HEAD 基线 critical 328 绿）；
  - 定点偿还：删除 core/degradation.py + tests/test_phase1_5_modules.py 的
    TestDegradationManager 4 例（69 passed 复验、全仓零引用复验、
    core/__init__.py 无残留导出复验）；DEPRECATED_MODULES.md 两节 REMOVED 化；
    CLAUDE.md 用例数 500+→5716（实测收集）；新增 workflow-v2-convergence-checklist.md。
  - 以上仅提交本轮自有文件（见 git log），未触碰在途会话的 581 文件 WIP；
    tests/test_phase1_5_modules.py 因含在途 lint 批次的 import 排序，
    随本轮一并提交（无语义影响）。
- **在途会话（工作区，未提交）**：ruff 全仓清理、env_flag 统一、model_discovery
  缓存重构、server.py 凭证同步修复、test_pipeline_call_signatures.py 守卫。
  工作区其余改动归其所有，收尾门禁见 §七 P0。
- **另见**：/home/orangepi/wt-fix 工作树（fix/sqlite-httpx-hygiene 分支）为第三条
  在途线，本轮未触碰。
