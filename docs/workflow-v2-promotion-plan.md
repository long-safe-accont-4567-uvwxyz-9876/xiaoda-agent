# workflow_v2 完整转正立项书

日期:2026-08-22 · 状态:**待审阅**(用户已拍板"完整转正、先立项")
关联提交:`2e8f1b56`(最小闭环,降级为本立项的 M0 底座)

---

## 1. 背景与现状核查(2026-08-22 实地核对)

**v1 的真相:没有执行器。** `web/routers/workflows.py` 保存工作流 JSON 时顺带
生成 `skills/wf_{name}.md`,由 `prompt_builder.load_skills()` 注入 system prompt。
也就是说 v1 "执行"= 把工作流拼成一段 prompt,**由 LLM 自由发挥**,不是确定性 DAG。
这决定了: v1 → v2 转正不是"迁移执行引擎",而是**第一次给工作流装上确定性的执行语义**。

**v2 现状(承接 `2e8f1b56`):**
- 已具备:SQLite 持久化(定义/版本/运行/步骤/事件)、DAG 调度器(compute_ready、
  claim-step CAS、终态守卫、重启恢复)、图校验与 content_hash、路由(启动/列表/
  publish)、前端按钮与运行弹窗。
- 空白:`WorkflowExecutor` 仅实现 `legacy_prompt` + 结构节点;`tool/mcp/agent`
  三类真实节点全部返回 `UNSUPPORTED_NODE` 失败落库。`NodeSpec` 的
  timeout/retry/failure_policy/idempotency 字段齐备但执行器一侧没有消费。

**可复用能力层已经存在**(实测核查,非臆测):
| 能力 | 可复用入口 | 耦合度 |
|---|---|---|
| 工具调用 | `tool_engine/tool_executor.py::ToolExecutor.execute(name, args, user_id, safe_mode)` | ✅ 可脱离 AgentCore 独立 new(web/routers/tools.py:116 同行用例); 注册表/限流/沙箱/审计内置 |
| MCP 工具 | 启动后以 `mcp_<server>_<n>` 注册进同一注册表,走 ToolExecutor | ✅ 同一路径,MCPManager 先 start |
| LLM 调用 | `model_router.ModelRouter.route / route_config(config, messages)` | ✅ 完全独立,密钥走 provider_key |
| 子智能体 | `agent_core/sub_agent.py:SubAgent.chat`(需 core/mcp_manager) | ⚠️ 强耦合 AgentCore 上下文,需封装,后续立项 |
| 技能 | 无执行端,仅 `prompt_builder.load_skills()` 注入 | ⚠️ 需要新定义"技能节点执行"语义 |
| 敏感/安全 | `security/security.py`(输入威胁检测/输出隐私)、`secrets_broker`、`tool_executor._generic_sensitive_args` | ✅ 现成 |

**migrate_v1 映射现状核实**(决定执行含义):
- v1 `tool`→TOOL(ref+arguments)、`mcp`→MCP(ref+arguments)、`step`→TRANSFORM,
  `agent`→AGENT、`model`→AGENT(model_policy)、`skill`→AGENT(skill_refs)、
  unknown→LEGACY_PROMPT。
- ⚠️ 设计缺陷:`skill` 节点被映射成 AGENT(子智能体)——但 v1 里 skill 是
  **拼进 prompt**,不是子智能体。M1 需修正为独立 SKILL 语义(见 §4.5),
  否则迁移后技能节点会行为漂移。

## 2. 目标与非目标

**目标(本立项交付):**
1. 统一执行管线:TOOL / MCP / AGENT / MODEL / SKILL / TRANSFORM / LEGACY_PROMPT
   全部有确定性实现,复用现有能力层而不是再造一套;
2. 安全:input/secret 清洗、脱敏日志、敏感工具护栏、审计事件带节点标签;
3. 前端按 revision/发布模型重写交互(草稿保存 → 发布为版本 → 运行当前版本 →
   版本列表/发布/回滚);
4. 双引擎并存的发布/回退/迁移策略与开关灰度;
5. 完整测试面(单节点类型集成 + 安全专项 + 迁移幂等)。

**非目标(明确不做,防范围蔓延):**
- 高级 DAG 结构节点(JOIN/CONDITION/PARALLEL/INPUT/APPROVAL/DELAY)的富交互
  编排——先支持校验与结构通过,执行侧 WORKFLOW/条件边留给 M4+;
- 自动运行恢复之外的"人工暂停/恢复" UI、实时 WebSocket 节点事件推送
   (轮询 after_seq 足够 M1 用);
- v1 编辑器本体重构(v1 仍是定义作者,见 §6)。

## 3. 架构:统一 Tool 调用管线

```
                     ┌──────────────────────────────┐
                     │  WorkflowExecutor (注入槽)    │
                     │  node dispatch → 5 类处理器    │
                     └──┬───────┬───────┬──────┬────┘
        TOOL/MCP       │  MODEL │ AGENT │SKILL/LEGACY
                        ▼        ▼       ▼      ▼
┌────────────────┐ ┌──────────┐ ┌────────────┐
│ ToolExecutor   │ │ ModelRouter│ │ SubAgent    │
│ .execute()     │ │ .route_config│ │ .chat()     │  ← 复用现有层
│ (registry +    │ │           │ │ (M1 先封装)  │
│  sandbox+审计) │  │ fallback  │  │ skip       │
└──────┬─────────┘  └──────────┘  └────────────┘
 通用横切(M1 内全部覆盖)
  限流 → super().__init__ 权限manager → 审批钩子 → 审计(workflow_id/run_id/node_id)
  input_value 清洗(security.check_validation_error)
  输出脱敏(check_output_privacy) + secret 占位符(secrets_broker) ← 见 §5
  错误归一(超时/重试/失败策略 → NodeResult → FailurePolicy)
```

**节点→实现映射(M1 交付):**
| NodeType | M1 实现 | 复用点 |
|---|---|---|
| START/END/TRANSFORM | 直通,record 输出 | 现状已做 |
| TOOL | `ToolExecutor().execute(tool_ref, args)` | 复用注册表+安全+审计 |
| MCP | 同上(`mcp_<s>_<n>`),参数透传 | 复用注册表 |
| MODEL | `ModelRouter().route_config({"ref":...}, messages)` | 独立可复用 |
| SKILL | **独立技能执行器(决策已定)**：SkillLoader 解析技能 manifest+正文 → 组装执行指令(技能内容+节点 input) → `ModelRouter.route_config` 独立推理 | 新增组件,复用 SkillLoader + ModelRouter |
| AGENT | `SubAgent.chat(task, ctx)` 目标封装(v1 委托语义) | 二期(M1.5) |
| LEGACY_PROMPT | 现状保留(LLM 单轮) | 现状 |
| CONDITION/PARALLEL/JOIN/WORKFLOW/.. | 校验通过即失败标记 NO_IMPL(防静默),记录 | 不做执行 |

**执行器统一共性**(M1 必须):
- 每节点:`asyncio.wait_for(timeout_seconds)` → 失败打 error_code/error_message
  落 step;`failure_policy` 生效(FAIL_RUN → run_failed;CONTINUE → 记录后继续);
- `retry_policy` 用指数退避最多 max_attempts;
- 输入统一为 `ctx["run"]`(`run.input` + 上游输出),输出写 `node_outputs`,
  供箭头链下游读取;
- 全部走 `_execute_with_timeout` + `_generic_sensitive_args` 脱敏日志;
- STEP 事件记录原文 + 脱敏后,满足审计。

## 4. 前端:revision 模型的交互重写

现状问题(用户指出的确凿):
- v1 前端是"单文件编辑 → 保存"模型; v2 的 publish 语义是"固化快照 + 提升 current";
- 现有"发布/运行"是打补丁,不是 version 管理。

目标交互模型:
```
[编辑]  草稿写在 v1 JSON(单文件,由 legacy 约定自动写 skill)
            ↓ "发布"
[发布]  =  从当前 v1 JSON 固化 revision_v2(current_revision_id ← 新修订)
            ↓
[运行]  以 current 版本执行(与编辑中未发布的草稿无关)
            ↓
[版本]  列表(时间/内容hash/说明) + [回滚] = PATCH current_revision_id(etag CAS)
```

涉及的 v2 路由(补齐 M2 + 前端):
- `POST /workflows/{wid}/revisions`(显式创建,当前从 v1 快照)
- `PATCH /workflows/{wid}/current`(回滚,带 If-Match etag)← 现无
- `POST /workflows/{wid}/publish` 保留(语义: v1 快照 → 新修订 + 置 current)
- `GET /workflows/{wid}/runs|revisions` 已具备(仅列表补充)
- 运行弹窗:自动轮询 `GET /workflow-runs/{rid}`(after_seq),(M2)事件流。

前端补丁点: WorkflowView 增加"回滚"动作;发布按钮加二次确认;运行按钮
只在有 current_revision 时可点(第一次需要先发布,自动引导),禁用态提示文案。

## 5. 安全设计(执行引擎专属,M1 开始就不允许裸奔)

1. **输入清洗**:节点 input / args 先走 `SecurityFilter.check_validation_error`
   拒绝明显注入(CLI/mot)参数;拒绝写入 tools 的敏感参数名表。
2. **secret 解耦**:节点参数支持 `{{secret:name}}` 占位符,由
   `SecretsBroker.get(...)` 运行时解析(不落库、不落 step output 原文)。
3. **输出隐私**:LLM 节点输出走 `check_output_privacy`(主路径同款),避免
   工作流把密钥回显给用户。
4. **敏感工具审批(决策已定:照主对话走卡片确认)**：需要
   `requires_confirmation`/WRITE 权限的工具,执行器走**审批通道**:
   检测到 → run/step 置 `WAITING_APPROVAL` → 产生审批事件 → WebUI 审批
   卡片(复用主对话审批组件)→ 批准放行、拒绝则步骤失败(`APPROVAL_REJECTED`)。
   M1 先落服务端通道+状态/事件,前端卡片 UI 归 M2;
5. **审计**:步骤事件 payload 一律脱敏后落库(tool args/output 用
   `_generic_sensitive_args`), workflow_id/run_id/node_id 三级标签齐全,
   事实供 `GET runs/events` 展示。

## 6. 双引擎并存与迁移/回滚策略

**作者模型(过渡期)**:v1 CRUD 是定义唯—写入口(不新增文件格式)。
- 每次"发布" = 从当前 v1 JSON 快照为不可变 revision → 提升 current。
- v2 只消费 current revision + 写 runs。(现服务已按此实现)
- **回滚** = 切换 current_revision(服务层已有,前端/API 补)。

**迁移工具**:`scripts/migrate_v1_workflows.py`(CLI):
- 列出 `WORKSPACE_DIR/workflows/*.json` → 逐个 `migrate_v1` → 校验图 → 幂等
  upsert 定义 + insert revision(同 content_hash 跳过)→ 置 current;
- `--dry-run` 输出差异报告;`--rollback <wf_id> <prev_rev>` 回退。

**开关与灰度(决策已定:试点少数工作流)**:不新增环境变量(约束),用 **DB
config 键**:`workflow_v2.enabled`(全局,默认 `false`)+
`workflow_v2.pilot_wf_ids`(JSON 数组)。生效规则:全局开 **或** run 所属
wf_id 在白名单内 → 该流可用;否则前端隐藏"启动"按钮(占位文案)、对应路由
503、driver 不为非试点流调度。试点期把目标工作流逐个加入白名单,每轮跑
§7 全量测试 + 抽查 v1/v2 语义一致性。v1→v2 收敛评估通过后再开全局开关。

## 7. 里程碑与验收

**M0(已完成等于 2e8f1b56)**:最小闭环(迁移/排队/调度/legacy)。
**M1(统一执行管线)**:§3 全表 + §5 全项(含 B 决策的 SKILL 独立执行器与
   - 验收: 每个节点类型一个集成测试;TOOL/MCP 用 fake ToolExecutor;
     MODEL/SKILL 用 stub ModelRouter; 安全专项(secret 不落日志/db;审批拒绝
     路径可测)。
   - 状态: ✅ 2026-08-22 落地 — executor.py 统一执行器接入 build_runtime
     (tool_executor/router/security/secret/skill_vesolver 全注入);
     NodeType 增加 MODEL/SKILL;AGENT 保留 AGENT_NOT_IMPLEMENTED(等 M1.5)。
     tests/workflow_v2/test_executor.py 21 项 + test_runtime_smoke.py
     端到端(TOOL→MODEL→END 全链路),全套 56 通过。
**M1.5(AGENT 子智能体节点)**:§3 表中 AGENT 行的"二期(M1.5)"落地——
   决策: 迁移产物按 config 键三分派,主路径走真实子智能体。
   - 状态: ✅ 2026-08-22 落地 — `_run_agent`:`agent_ref` → 子智能体
     (subagent_loader 走 core.dispatcher.get_agent,chat(task,context)
     委托,超时/异常/隐私过滤对齐 LLM 节点);无 agent_ref 时
     `skill_refs`→SKILL 回退、`model_policy`→MODEL 回退(M1 迁移兼容);
     找不到/不可用/无 loader 一律显式失败(AGENT_NOT_FOUND /
     AGENT_LOADER_UNAVAILABLE / AGENT_TIMEOUT),永不静默。
     build_runtime 注入 `_subagent_loader_from_core`;
     tests/workflow_v2/test_m5_agent.py 13 项(fake SubAgent + 三分派 +
     async loader + 输出过滤),旧 test_agent_node_not_implemented 改为
     断言 loader 缺失仍显式失败。
**M2(前端 revision 模型)**:§4;验收: 浏览器走查 发布→运行→回滚→再次运行;
  typecheck + vue 构建通过。
   - 状态: ✅ 2026-08-22 落地 — `POST /revisions` 显式快照(不升 current)、
     `PATCH /current` 回滚(If-Match etag CAS,404/409 区分)、
     `list_revisions` 富化 current/etag;service 拆出 snapshot_revision_from_v1。
     WebUI: 发布 popconfirm 二次确认、版本弹窗(编号/时间戳/哈希/当前 Tag/
     回滚 popconfirm)、运行弹窗非终态 2.5s 轮询自停、启动 tooltip。
     测试: 服务层 6 + 路由层 3(发布→回滚→409 全链路),全套 65 通过;
**M3(迁移/回滚/灰度)**:§6;验收: CLI dry-run 对真实工作区零失败; 双
 引擎一致性抽查脚本。
  - 状态: ✅ 2026-08-22 落地 — `wf_config` KV 表(db v28)+ 灰度开关
    `workflow_v2.enabled`(默认关)/`pilot_wf_ids` 白名单(DB config,不新增
    env var);启用规则 = 全局开或白名单内;路由 `POST /runs` 未开放 503
    (WORKFLOW_V2_DISABLED)+`GET /{wf_id}/v2-status` 前端可用性查询;
    driver 不调度未开放工作流的 QUEUED 运行(白名单加入后自动续跑);
    启动按钮未开放时禁用+占位文案;迁移 CLI `scripts/migrate_v1_workflows.py`
    (status/dry-run/迁移幂等(dedup by content_hash,不覆盖人工回滚)/
    rollback/set_current/pilot 白名单操作);
    验收: 真实工作区 `--dry-run` rc=0 零警告; 18 个新用例(M3 灰度 16 +
    版本 28 契约补齐); 全套 4965 通过。
**M4(加固/可观测)**:高级 DAG 节点 REVIEW、PRINCIPLES 文档 + 负载节流、
 workflow 级指标(事件已有,加 debug 计数)。
  - 状态: ✅ 2026-08-22 落地 — REVIEW 审批节点（executor 置 WAITING +
    `wf_review` 表(schema v29) + 决策端点批准放行/拒绝停流,单事务 CAS 防重复
    决策); APPROVAL 老类型同语义兼容;负载节流 `workflow_v2.max_concurrent_runs`
    (DB config,默认 4,0=不限)(driver 只挡新 QUEUED run 启动);
    workflow 级指标 `GET /workflow-metrics`(运行/步骤/事件/审批 debug 计数);
    REVIEW 文档 docs/workflow-v2-review.md + 原则文档 docs/workflow-v2-principles.md;
    测试: test_m4_review.py 12 项;全套 workflow_v2 93 + 版本契约 57 通过。
**M5(前端 REVIEW 审批卡片收口)**:M2 §5.4 决策 #2 承诺的前端审批通道;
M4 REVIEW 节点服务端已就绪,前端补齐。
   - 状态: ✅ 2026-08-22 落地 — api 层 `listWorkflowReviews` /
     `decideWorkflowReview`(approve/reject + 可选备注);WorkflowView 运行
     弹窗内 `waiting_input` 运行项内嵌 REVIEW 审批卡片(待决 review 列表、
     备注输入、批准/拒绝按钮、决策中 loading、决策后刷新运行列表并停轮询);
     卡片随 2.5s 轮询增量加载,无新增轮询周期。
     typecheck + vite 构建通过。

## 8. 决策表(2026-08-22 已全部拍板)

| # | 决策项 | 结论 | 影响 |
|---|---|---|---|
| 1 | skill 节点语义 | **B 独立技能执行器**(工程量+) | M1 新增 SKILL 执行器组件 |
| 2 | 敏感工具默认策略 | **B 照主对话走卡片确认**(需前端配合) | §5.4 审批通道;M1 服务端、M2 前端卡片 |
| 3 | 灰度范围 | **试点少数工作流** | §6 pilot_wf_ids 白名单 |
| 4 | v1 历史工作流迁移 | **按需自动迁移,保持现状** | 维持现状,CLI 全量导入仅作可选 |

## 9. 风险

- 执行器接入会产生双引擎语义差(v1=prompt 注入 vs v2=确定执行)——用
  §4 的前端交互与 §6 开关做缓冲;
- SubAgent 复用耦合大 → 风险最高,放 M1 末做,失败则 M 只交付四种能力
  (tool/mcp/model/skill),agent 节点保持 NO_IMPLEMENT;
- 调度器现有假定对长任务(LLM 60s+)的 lease_ttl=60s 正好边际——M1 需
  验证 tick 与 executor 并发,出问题则调 lease 或延长超时。