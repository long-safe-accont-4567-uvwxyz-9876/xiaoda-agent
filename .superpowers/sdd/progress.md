# WebUI experience optimization progress
Baseline: 77 tests passed; working tree snapshot /tmp/webui-experience-baseline.patch

## Plan: 2026-08-09 Local AI Platform（续）

- [x] Task 8–11：按既有台账完成并复审通过
- [x] Task 12：VectorStore 与 Memory 集成（生产实例装配、缓存 generation 隔离、停止实例无静默回退；28 项指定回归 + 53 项实例回归；复审通过）
- [x] Task 13：Provider Catalog 单一权威（二审通过）
- [x] Task 14：完整协议 Transports（二审通过，98 项相关回归）
- [x] Task 15：Provider 原子接入与路由校验（统一 setup/startup/model discovery 生命周期入口；无探活快照回滚；199 项聚焦回归；F1 复审规范与质量通过）
- [x] Task 16：ModelRouter Local Transport 迁移（task-16-review-local-ai.md，复审 2 项 Important 已闭环修复）
- [x] Task 17：Local AI REST 与 WebSocket API（task-17-review-local-ai.md，Spec Approved / Quality 有条件 Approved）
- [x] Task 18：Local AI Pinia Store 与 API Client（task-18-review-local-ai.md，Spec/Quality PASS，localAi.ts 5 处 TS2345 已修复）
- [x] Task 19：五标签本地部署 UI（task-19-review-local-ai.md，Spec/Quality PASS）
- [x] Task 20：统一 Provider 接入 UI（task-20-report.md / task-20-review-local-ai-v2.md；后端功能/原子安全双 PASS，前端 Spec/Quality 双 PASS；207 passed + 17 passed，vue-tsc/build/diff-check exit 0；核心 Provider Ruff/py_compile 通过，setup.py/server.py 全文件 Ruff 39 项存量 import 风格债务如实记录；未提交）
- [x] Task 21：跨平台 Runtime 打包（task-21-review-local-ai.md，Spec/Quality PASS）
- [x] Task 22：运维与用户文档（行为合同 RED：1 failed/8 passed；GREEN：16 passed；完整中文指南、README 入口与 ModelScope 环境说明已补齐；未提交）
- [x] Task 23：全项目验证、兼容性清理与端到端验收（聚焦 713 passed；完整 Python 3864 passed/6 skipped/6 deselected；vue-tsc/build/脚本/YAML/diff 检查通过；实际 Web UI 可启动预览；外部设备/凭证流程按环境边界记录；未提交）

2026-08-12/13（全量 23 Task 审核闭环）：dispatch 4 组独立审核（task-1-6-final-review.md / task-7-12-final-review.md / task-13-17-final-review.md / task-18-23-final-review.md，HEAD=488f571）。结论：Task 1-17 全 PASS（0 Critical/Important）；Task 18/20/21/22 PASS；发现 **1 Critical（C1：860bdb3 功能节点界面重写引入 6 个前端契约失败、Task 23 通过证据过时）+ 3 Important（I1 ModelMarketTab 默认目录复用+每次下载前校验丢失；I2 4 组件绕过 store 直连 api；I3 文档↔UI 脱节 + SystemModelNodesTab 契约覆盖缺口）+ W1（failed 口径不统一）+ Task 6 I1（restricted-mode 越界阻断未实现）**。已全部闭环（修复报告 task-18-23-fix-report.md / task-6-roots-fix-report.md）：
- 前端：ModelMarketTab 恢复 choose（generation 竞态守卫 + 默认目录 validateStorage 复用）；8 个 local-ai 组件收敛到 store（新增 hubCategories/searchHub/inspectRemote/downloadHubRepository/benchmarkModel/refreshDevices/fetchModelNodes/setModelNodeBackend action）；契约测试更新为 6 标签/8 组件/failed 过滤；docs 六标签 + 文档↔LocalDeployView 一致性断言；三处 failed 口径统一；附带修复 860bdb3 遗留 14 个 vue-tsc 类型错误。
- 后端：config_service 新增 allowed_storage_roots + LOCAL_AI_ALLOWED_STORAGE_ROOTS 解析；storage.py validate_destination 增加 realpath 受限模式越界阻断；.env.example/docs 补说明；新增 test_local_ai_storage_roots.py（14 测试，TDD RED→GREEN）。
- 终态复核（final-verification.md）：前端契约 29 passed、docs 契约 19 passed、storage_roots 14 passed、storage 36 passed、downloads 18 passed、vue-tsc exit 0、ruff/diff-check 通过；改动文件边界 13 个未越界。**全量 23 Task 整体 Spec/Quality 通过**。

2026-08-11（第二轮复审闭环）：对已实施未复审的 Task 16/17/18/19/20/21 全部 dispatch 独立 reviewer 并产出双判定报告（.superpowers/sdd/task-{16,17,18,19,20,21}-review-local-ai.md）。发现并修复：Task 16 `_classify_error` NameError 回归 + `route()` 非流式缺 local-ort 分支（TDD RED→GREEN，11 passed/2 deselected[外部测试]）；Task 20 ModelsView.vue `key_masked` TS2339（移除无数据源展示行）；Task 18 localAi.ts 5 处 TS2345（ws.ts `on/off` 泛型化，vue-tsc 错误消除 + 25 契约测试 passed）。遗留跟踪：Task 17 2 个 Warning（local_deploy.py 回退路径硬编码 "VIP9000"、认证测试未覆盖 storage 端点）、Task 18 W1（实例启动失败事件静默丢弃）、Task 20 W1/W2（legacy 封装残留、单 header 编辑）——均记录不修（范围纪律）。TopBar.vue:52 / SettingsView.vue:295 类型错误属其他并发任务区域，不处理。

2026-08-11: Local AI Platform (docs/superpowers/plans/2026-08-09-local-ai-platform.md) Task 13 Provider Catalog / 14 Protocol Transports / 15 Atomic Provider Onboarding implemented; Task 15 second review (Spec PASS / Quality conditional FAIL) raised N1-N4; N1 builtin availability fixed via _provider_ready + _is_client_configured, N2 dead is_local_host exemption removed, N3 dead _save_key_and_register removed, N4 server key write made atomic (setup/model_discovery ProviderService migration recorded as boundary). Final: 143 focused + 21 provider-compat tests green, ruff/compileall/git-diff-check clean. Awaiting focused acceptance.
Task 1: complete (working tree, review clean; 25 tests passed)
Task 2: complete (working tree, review clean; 49 tests passed)

2026-08-12: Tasks 9-12 联合兼容性与端到端验收（E2E 完成）: 11 个测试文件联合 340 passed（ort_runtimes 17 / ort_genai 37 / instances 53 / memory_integration 52 / parent_child 25 / context_governance 4 / local_embed_mode 3 / model_registry 39 / router_local_transport 6 / api 19 / contracts 85）；ruff E9,F63,F7,F82 全过；compileall 通过。各任务均已复审（Task11 双门禁、Task12 四轮复审后规格 PASS/质量 APPROVED）。未提交。

## 2026-08-24 技术债整改（审查驱动，工作树未提交）
基线：HEAD=28fa9269 起始时点附近；全程不提交不推送；共享工作树有其他会话并行改动。
- [x] 批1 P0 隐私隔离（跨用户动态上下文 + 群检索 scope）：
  - 子任务A 用户上下文：UserContextToken(user_id,epoch)/commit_user_context 锁内提交；restore A→B/ABA 防护；后台 notebook/portrait/memory await 后写入带 token；称谓锁内切换；bootstrap 目标 key 激活后延迟加载+single-flight；owner-only portrait/notebook gate；子代理单/并行群访客 log-only；QQ timeout failure 带请求 token；Web 直达子代理收敛 core.dispatch_web_sub_agent() 锁内入口。reviewer 三轮后 APPROVED（tests/test_user_context_races.py 等）。
  - 子任务B 群检索 scope：Scope 不可变+qq_group:{真实group_openid} 边界；FTS/时间/entity/child SQL LIMIT 前下推；vector/child/spreading 改 keyset 分页续窗+预算 partial 指标；KG v1 有界扩候选；KG v2 scoped fail-closed（legacy default 仅 admin）；query cache namespace 含边界；sub_agent submit_memory 访客拒绝/owner 绑定 current_scope()/写后失效缓存；群 activation_key 与 Scope.session_id 同源；restore_from_db/get_conversations_readonly 接收完整 Scope。reviewer 四项阻断确认关闭+唯一过时契约测试已修。
  - 合并验收：17 文件 222 passed（scope/context/QQ/KG/subagent/dispatcher 全绿）。
- [x] 批2a 媒体 token 收敛（主会话+代理协作）：query ?token= 全链路移除（media_auth/media.py/chat export/agents 贴纸）；贴纸端点改 cookie→Bearer 凭据链；x_media_token 双路径下发（/media + /api/v1/agents）；test_media_auth 等 29+43 passed。
- [x] 批2b systemd/Dockerfile：install-linux.sh unit 加 User=/Group=/HOME/NoNewPrivileges/ProtectSystem/ReadWritePaths，root 需 --force；Dockerfile HEALTHCHECK 只读化；79 项测试通过。
- [x] 批4 CI/发布链（代理完成）：pytest-cov 入 lock、管道假绿移除、deselect 用例修复；docker needs:[build,test] promotion 拆分；.run 锚定 ^__ARCHIVE__$ + gzip magic smoke；updater fail-closed + linux-arm64 映射；wheel py-modules 27 根模块 + config exclude（干净 venv 实证 --help）；ConPTY 安装校验；Actions SHA 固定；security_gate.py 门禁化。100 项测试通过。遗留：镜像 digest 离线未 pin（TODO 注释）、lock 非哈希闭包。
- [x] 批5a WS 幂等+心跳自取消（主会话）：track put-if-absent + DUPLICATE_IN_FLIGHT/COMPLETED 回执 + 60s TTL 重放；unregister 永不 cancel current_task；新增 test_ws_msg_id_idempotency.py，WS 套件 43 passed。
- [x] 批5a 补遗（主会话）：test_ws_heartbeat 两用例改走 register() 真实路径（含心跳任务正常 return 非 cancelled 断言）；unregister 任务回收加 5s 超时上限。WS 五套件 43 passed。
- [x] 批3 迁移原子性收尾（主会话）：SAVEPOINT 包裹 + v16/v17 内部 commit 清零后，v17 兼容测试改直应用 v17+v20（最小 v16 夹具不含 FTS 等后续迁移假设对象，全链覆盖由新鲜库套件承担）；迁移套件 13 passed。
- [x] 批4b 适配器/生命周期批收尾（主会话，六项全 GREEN 74 passed）：第 5 项插件装配顺序——_setup_plugin_manager 提取并在 core.init() 前注册、_start_services 复用+回绑 _memory/_kg 不二次 discover；测试补丁修至 agent_core.AgentCore 源模块（局部导入）、_FakeCore 记录点移入 init()。
- [x] 批4c 数据库小任务B收尾（主会话，子代理仅落 RED；204+55 passed）：B1 execute_and_commit 受控单语句写入口（与 write_transaction 共享连接级锁）+ KnowledgeDBV2 auto_commit 写点锁内 BEGIN IMMEDIATE 失败即抛 + merge_relation 移除隐式事务降级；B2 删除顺序重构——_purge_memory_references 先清 memory_versions/context_audit_log/entity_memory_links/memory_child_chunks/FTS 再删主行，commit 后删向量（失败不抛可对账重试），delete_memory/delete_memory_with_vector/hard_delete_raw_for_user_request 三路统一，memory_tool forget 自动受益；B3 register_migration 幂等钩子 + doctor REQUIRED_RECONCILIATION_TABLES 公开常量（v32 迁移同构验证绿）；B4 HASH_ALGO_VERSION="v2-snapshot-500" 快照口径哈希 + migrate_legacy_hashes 一次性存量修复（旧口径签名识别、疑似篡改不动、prev 链重接）。遗留：MemoryDB 其余 auto_commit 写点未全部迁移到 execute_and_commit（契约测试覆盖面外）；delete_memories_batch 仍向量先行（无测试覆盖，未动）；长文本 >500 字符窗口外的更新不产生新版本行（快照口径的正确语义，测试已注明）。
- [x] 批4c 补遗·高频写点迁移完成（主会话）：WriteTxGuard/owned_write_section 注入式守卫——DatabaseManager.init 给 manager.memory attach_tx_guard（外层 write_transaction 持锁期间让渡提交避免死锁，独立 auto_commit 写锁内执行+提交）；EpisodicMixin 全部 10 个写方法迁移（insert 含 FTS 并段单次提交、classification/emotion/distill/summary/fallback_raw/access_count×2/archive×2）；delete_memories_batch 改主库先行→commit→向量后置可重试。新增回归：insert 与长事务竞态零泄漏、批量删除 commit 后删向量+失败重试。相关记忆套件 77+139+44 passed，ruff/diff-check 干净。
