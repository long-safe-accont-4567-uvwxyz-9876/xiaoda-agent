# Principal 与 ConversationSession 身份隔离实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 Principal 与 ConversationSession 核心接缝，并修复权限参数丢失和记忆检索默认 Scope 回退两个 P0 问题。

**Architecture:** 使用 `PrincipalResolver` 将渠道声明解析为 fail-closed 的稳定主体；使用不可变 `ConversationSession` 同时携带 principal、context、session 与 memory scope，逐步替代散落字符串。首批只接入 `AgentCore.process()` 请求入口，不重写历史存储；MemoryManager 请求级检索必须消费显式或已绑定 Scope，缺失时抛错。

**Tech Stack:** Python 3.11、dataclasses、ContextVar、asyncio、pytest、pytest-asyncio

## Global Constraints

- 保留现有 `AgentCore.process()` 参数，避免破坏 Web、QQ、微信和 CLI 调用方。
- 身份解析 fail-closed：没有稳定账号 ID 或未在 owner 集合中的主体不得获得 owner 权限。
- `ConversationSession` 是请求级不可变值对象，不持有共享 history，不引入新的全局锁。
- 记忆检索仅接受显式 Scope 或当前请求绑定的 Scope，不允许静默回退 `default/xiaoda`。
- 工具权限检查必须收到原始 `arguments`，不得记录敏感参数。
- 不新增第三方依赖，不修改数据库 schema，不提交 Git。

---

### Task 1: Principal 身份接缝

**Files:**
- Create: `agent_core/principal.py`
- Modify: `agent_core/__init__.py`
- Test: `tests/test_principal.py`

**Interfaces:**
- Consumes: `SecurityFilter.is_owner(subject_id: str) -> bool`
- Produces: `Principal`, `ChannelIdentity`, `PrincipalResolver.resolve(channel_identity) -> Principal`

- [ ] **Step 1: Write the failing tests**

覆盖已配置 owner、未配置访客、缺少稳定 ID、QQ 前缀兼容，以及 Principal 的 `principal_id/is_owner/address_term`。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_principal.py -q`

Expected: FAIL，模块 `agent_core.principal` 尚不存在。

- [ ] **Step 3: Write minimal implementation**

使用 frozen dataclass；`PrincipalResolver` 只依据稳定账号 ID 和 `SecurityFilter` 判定 owner，渠道类型不产生授权。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_principal.py -q`

Expected: PASS。

### Task 2: ConversationSession 请求级框架

**Files:**
- Create: `agent_core/conversation_session.py`
- Modify: `agent_core/_shared.py`
- Modify: `agent_core/__init__.py`
- Test: `tests/test_conversation_session.py`

**Interfaces:**
- Consumes: `Principal`, `memory.scope.Scope`
- Produces: `ConversationSession.create(...)`, `ConversationSession.memory_scope(request_id)`

- [ ] **Step 1: Write the failing tests**

验证会话保留原始 principal、独立 context ID、标准化默认 session ID，并生成同主体同 Agent 的 Scope。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_conversation_session.py -q`

Expected: FAIL，模块尚不存在。

- [ ] **Step 3: Write minimal implementation**

建立 frozen `ConversationSession`，字段为 `principal/context_id/session_id/agent_id/source/channel_subject_id`；`RequestContext` 新增强类型 `principal` 与 `conversation_session` 兼容字段。

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_conversation_session.py -q`

Expected: PASS。

### Task 3: AgentCore 接入新身份与会话

**Files:**
- Modify: `agent_core/core.py`
- Test: `tests/test_agent_core_principal_session.py`
- Test: `tests/test_identity.py`
- Test: `tests/test_shared_context.py`

**Interfaces:**
- Consumes: `PrincipalResolver.resolve()`、`ConversationSession.create()`
- Produces: 每次 `process()` 中绑定一致的 RequestContext 与 memory Scope

- [ ] **Step 1: Write the failing integration test**

通过最小 fake core 调用 `process()`，验证未授权的 Web/私聊主体不再因渠道成为 owner，并验证 `ctx.principal`、`ctx.conversation_session` 与绑定 Scope 一致。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_agent_core_principal_session.py -q`

Expected: FAIL，旧入口仍按渠道默认 owner。

- [ ] **Step 3: Write minimal implementation**

在 AgentCore 初始化时创建 resolver；`process()` 先创建 ChannelIdentity 与 Principal，再计算 context ID 并创建 ConversationSession；保留 `UserIdentity` 兼容投影。

- [ ] **Step 4: Run identity regressions**

Run: `pytest tests/test_agent_core_principal_session.py tests/test_identity.py tests/test_shared_context.py -q`

Expected: PASS；如旧测试断言“所有非群聊都是 owner”，应更新为 fail-closed 新契约。

### Task 4: 权限参数透传

**Files:**
- Modify: `tool_engine/tool_executor.py`
- Test: `tests/test_tool_permission_argument_passthrough.py`
- Test: `tests/test_permission_mode_five_states.py`

**Interfaces:**
- Consumes: `PermissionManager.check_tool_permission(tool_name, tool_input)`
- Produces: 原始工具参数参与危险命令判断

- [ ] **Step 1: Write the failing test**

注入 fake PermissionManager，调用 ToolExecutor 公共执行接口，断言收到与调用方一致的参数字典，并验证 AUTO 模式拒绝危险 shell 命令。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_tool_permission_argument_passthrough.py -q`

Expected: FAIL，fake 只收到 tool name。

- [ ] **Step 3: Write minimal implementation**

把调用改为 `check_tool_permission(tool_name, arguments)`，不增加日志参数。

- [ ] **Step 4: Run permission regressions**

Run: `pytest tests/test_tool_permission_argument_passthrough.py tests/test_permission_mode_five_states.py -q`

Expected: PASS。

### Task 5: 记忆检索 Scope fail-closed

**Files:**
- Modify: `memory/memory_manager.py`
- Modify: `agent_core/message_processor.py`
- Test: `tests/test_memory_retrieval_scope_contract.py`
- Test: `tests/test_scope_isolation.py`

**Interfaces:**
- Consumes: `memory.scope.current_scope()`
- Produces: `MemoryManager.retrieve_memories(..., scope=None)` 必须使用已绑定 Scope，未绑定时抛 `RuntimeError`

- [ ] **Step 1: Write the failing tests**

验证绑定 Scope 时隐式检索使用该对象；无绑定且无显式 Scope 时抛错；显式 Scope 始终优先；缓存键包含 `user_id` 和 `agent_id`。

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_memory_retrieval_scope_contract.py -q`

Expected: FAIL，旧实现创建 `Scope()`。

- [ ] **Step 3: Write minimal implementation**

在检索入口用 `current_scope()` 替代 `Scope()` 默认构造；主消息链显式取得当前 Scope 并传入；缓存键加入 Scope namespace。

- [ ] **Step 4: Run Scope regressions**

Run: `pytest tests/test_memory_retrieval_scope_contract.py tests/test_scope_isolation.py tests/test_memory_tool_request_scope.py -q`

Expected: PASS。

### Task 6: 综合验证

**Files:**
- Verify only

**Interfaces:**
- Consumes: Tasks 1-5 的公开接缝
- Produces: 首批身份隔离框架可回归的验证证据

- [ ] **Step 1: Run focused suite**

Run: `pytest tests/test_principal.py tests/test_conversation_session.py tests/test_agent_core_principal_session.py tests/test_tool_permission_argument_passthrough.py tests/test_memory_retrieval_scope_contract.py tests/test_identity.py tests/test_shared_context.py tests/test_permission_mode_five_states.py tests/test_scope_isolation.py tests/test_memory_tool_request_scope.py -q`

Expected: PASS。

- [ ] **Step 2: Run smoke and diagnostics**

Run: `pytest tests/test_smoke.py tests/test_agent_routing.py tests/test_tool_guardrails.py -q`

Expected: PASS；随后检查所有改动文件的语言诊断无新增错误。

- [ ] **Step 3: Review diff**

确认没有修改用户已有的无关改动，没有默认 owner 或默认 Scope 回退，没有新增共享可变状态，也没有泄露工具参数。
