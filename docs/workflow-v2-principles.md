# workflow_v2 引擎原则（PRINCIPLES，M4）

> 这份文档是 workflow_v2 引擎的**设计与运维准则**——改动引擎前先对照。
> 日期 2026-08-22（转正前全量回看时沉淀，随 M4 加固固化）。

## P1. 永不静默失败
未知/未实现节点必须落**显式错误码**（`UNSUPPORTED_NODE` / `AGENT_NOT_IMPLEMENTED`
/ `REVIEW_REJECTED` …）+ 事件，禁止悄悄跳过。
违反示例：`if t == X: pass`；合规示例：`return NodeResult(status=FAILED, error_code=...)`。

## P2. 事件先于一切展示

- 每次状态迁移 = `wf_run_event` 一行（step_started / step_succeeded / review_required /
  run_failed …），事件流是**唯一权威审计**；
- step 的错误原始 + 脱敏后都进事件（payload 不允许出现明文 secret）；
- `workflow-metrics`（M4）只是内存计数，展示用，**不参与状态判定**——事件表才是真理。

## P3. 一切写入走 CAS

- run/step 并发写统一 `lock_version` CAS（claim / commit / cancel / review 决策
  同一闸门）；CAS 失败 = 数据被别人改过 → 放弃本轮，绝不覆盖；
- 幂等键（idempotency_key）唯一索引兜底并发建 run；
- `If-Match` etag 语义用于定义层（PATCH / 回滚 current）。

## P4. 双引擎并存期：v1 作者、v2 消费

- v1 CRUD 是定义**唯一写入口**；v2 只消费 current revision + 写 runs；
- "发布" = 从 v1 JSON 固化不可变 revision → 提升 current；回滚只移指针，版本内容不可变；
- v2 路由一律走 `app.state.workflow_v2`（降级 503），**不绕过服务层直写 repository**。

## P5. 灰度/节流不新增环境变量

- 一切运行开关/上限走 DB config `wf_config` 键
  （`workflow_v2.enabled`、`pilot_wf_ids`、`max_concurrent_runs`）；
- 未开放的流：路由 503、driver 不调度（队列保活，白名单加入自动续跑）；
- 节流只挡**新启动**，绝不打断已在运行的 run（限制 = 并发上限，不是速率）。

## P6. 执行器无隐藏全局态

- 能力层（tool/router/security/secret）全部**依赖注入**；测试用 faker，生产装真：
  "无 approver 时默认拒绝"也是注入的默认值，不是代码分支；
- `user_id="workflow"` 贯穿审计与审批，工作流内外一视同仁。

## P7. 节点超时 / 重试 / 审批语义收敛

- 每节点 `asyncio.wait_for(timeout_seconds)`；超时按 `retry_policy` 指数退避（只重试超时）；
- `failure_policy.FAIL_RUN` → run 终态失败；`CONTINUE` → 记录重试后继续；
- REVIEW/APPROVAL 节点 = 人工闸门：审批**不是**超时/重试——它唯一正确出口是
  人的决定（approve/reject），或 run 被取消。

## P8. 观测以"多少"优先

- workflow 级指标（M4）回答"正在跑什么、共跑过多少"；prometheus `/metrics`
  是进程粒度，不重复造轮子；
- debug 计数（事件数、审批待决数）纯内存，随进程生命周期，不做持久化承诺。

## 违背示例清单（历史踩坑，防止回潮）

- `RevisionProvider` 传成未 await 的协程函数 → 每 tick 崩（P2/P6 相关）；
- 缺状态的 run 直接 status 回退 → 永不终态（P3 的终态守卫）；
- Windows/posix 路径硬编码 → 排序外异常（P5 的"不硬编码"变体，见
  `docs/workflow-v2-review.md`）。