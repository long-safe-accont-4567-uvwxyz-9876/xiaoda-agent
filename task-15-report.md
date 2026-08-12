# Task 15 实施报告

## 状态

完成。严格按 RED→GREEN→REFACTOR 实施，未提交，未添加代码注释。

## 实施范围

- 新增 `llm_gateway/provider_service.py`，复用 Task 13 `ProviderCatalog` 与 Task 14 transports，提供 draft 探活、能力报告、模型发现和原子 CRUD。
- 新增 `web/routers/providers.py`，提供 provider draft 测试、列表、创建、更新、删除、能力和模型发现 API。
- 新增 `tests/test_provider_onboarding.py`，覆盖原子提交与回滚、API 状态码、路由校验、安全边界、自定义 mapping 合同和旧 API 迁移。
- 扩展 `ProviderCatalog.unregister()`，支持非内置 provider 的事务删除和回滚。
- 将旧 `/models/providers` 创建、更新和删除入口迁移到 `ProviderService`，避免保留非原子旁路。
- 将模型路由保存接入 provider 存在、启用、运行时可用和已发现模型校验；仅更新 provider 时使用当前有效模型校验。
- 将 Setup provider 凭证测试接入 catalog/service，将启动生命周期初始化 service 并注册 provider API。
- 将子代理 provider 解析迁移到 catalog metadata 与凭证别名，自定义 provider 继续使用受保护凭证文件。
- 保留 Setup 文件和路由，只做增量迁移。

## 原子性

- 创建和更新先构造 transport、执行 health check、生成候选 runtime client，再写凭证、配置、catalog 和运行时。
- 探活失败时不触碰磁盘配置、凭证、catalog 或 runtime。
- 提交阶段异常时恢复旧凭证、旧配置、旧 catalog 定义和旧 runtime client。
- 删除前拒绝仍被路由引用的 provider；删除阶段异常时恢复已移除状态。
- 内置 provider 不允许覆盖或删除。

## TDD 证据

### 既有 RED

Task 15 草稿最初已包含原子更新和禁用 provider 路由测试；当前接手时 4 项测试已通过，因此不将其冒充为本轮 RED 证据。

### RED 1：旧 API 非原子旁路

先新增旧 `/models/providers/{id}` 更新失败保持配置、凭证和 runtime 不变的测试：

```bash
.venv/bin/python -m pytest tests/test_provider_onboarding.py -q
```

结果：`1 failed, 5 passed`。失败为旧入口返回 `200`，证明它绕过 `ProviderService` 并直接写配置。

### GREEN 1

将旧 provider CRUD 委托给 `ProviderService` 后，同一测试文件结果为 `6 passed`。

### RED 2：路由有效模型与 SSRF

新增仅修改 provider 时校验当前模型，以及新 provider API 拒绝云元数据地址的测试。

结果：`2 failed, 6 passed`。路由错误使用空模型得到 `409`；新 API 未执行 SSRF 校验并进入 transport factory。

### GREEN 2

- 路由校验改用请求模型或当前路由模型组成的有效模型。
- draft 定义统一执行 http(s)、本地服务豁免和 SSRF 校验，API 将输入错误映射为 `400`。
- 修复后 Task 15 测试为 `11 passed`；随后并入 custom mapping 与 API 错误合同测试，最终为 `14 passed`。

## 最终验证

```bash
.venv/bin/python -m pytest tests/test_provider_catalog.py tests/test_provider_transports.py tests/test_custom_anthropic_provider.py tests/test_provider_onboarding.py tests/test_provider_key_reading.py tests/test_model_switching_refactor.py -q
.venv/bin/python -m ruff check llm_gateway/provider_service.py web/routers/providers.py tests/test_provider_onboarding.py
.venv/bin/python -m compileall -q llm_gateway/provider_service.py web/routers/providers.py web/routers/models.py web/routers/setup.py web/model_route_validator.py web/server.py web/agent_registry.py tests/test_provider_onboarding.py
git diff --check -- llm_gateway/provider_service.py web/routers/providers.py web/routers/models.py web/routers/setup.py web/model_route_validator.py web/server.py web/agent_registry.py llm_gateway/provider_catalog.py tests/test_provider_onboarding.py task-15-report.md
```

- Task 13 catalog、Task 14 transports、Task 15 onboarding、凭证读取和模型切换回归：`103 passed in 8.92s`。
- Ruff：`All checks passed!`。
- Python 编译检查：退出码 0。
- 差异空白检查：退出码 0。
- 新增核心文件与测试的编辑器诊断：无诊断。

## 安全边界

- provider base URL 只接受 HTTP(S)；云元数据、私网、链路本地和危险 DNS 解析结果被拒绝。
- localhost 本地服务保持显式放行，支持 Ollama 等本地 provider。
- API 不返回凭证；自定义 mapping 仅返回声明式路径、header 模板和 auth 合同。
- provider 删除仍要求 `X-Confirm: yes`。

## 范围纪律

- 未执行 commit。
- 未添加代码注释。
- 未删除或替换 Setup 相关文件、路由或组件。
- 工作树已有 Task 17、本地 AI、记忆和前端等不相关改动，本任务未回退或覆盖这些改动。
- `model_router.py` 的统一 transport 迁移属于 Task 16，本任务未修改。

## 修复周期（审查整改方案 B 执行证据）

以下为二轮审查（Critical×2 + Warning×5）整改期间逐项追加的 RED/GREEN 证据。

### RED 1 — 安全传输层（Critical 2 整改）

新增 8 个安全 transport 测试于 `tests/test_provider_transports.py`：

```bash
.venv/bin/python -m pytest tests/test_provider_transports.py -q -k "secure or injects_secure or uses_secure"
```

SecureAsyncTransport 尚未实现时结果：`ERROR tests/test_provider_transports.py - ImportError: cannot import name 'SecureAsyncTransport'`（RED，导入即失败）；随后实现 `security/ssrf_guard.py` 的 `SecureAsyncTransport` 与 `build_secure_async_client` 后，测试断言曾因 httpx 0.28 公开 `transport` 属性移除而失败（RED，AttributeError），修正断言为私有 `_transport` 后：

```bash
.venv/bin/python -m pytest tests/test_provider_transports.py -q -k "secure or injects_secure or uses_secure"
```

GREEN 结果：`8 passed, 41 deselected`。

### GREEN 1 — 默认 client 与运行时 client 统一安全传输层

- `llm_gateway/transports/anthropic.py`、`custom_mapping.py` 默认 client 改为 `build_secure_async_client(base_url)`，`aclose()` 契约不变。
- `provider_service._build_transport` OpenAI 分支注入 `http_client=build_secure_async_client(...)`；`_build_runtime_client` 经 `web.custom_providers.build_client` 同样注入。
- `web/custom_providers.py`：`PinningAsyncTransport = SecureAsyncTransport` 别名，`build_client` 注入 secure client。
- 适配 `test_http_transport_closes_only_owned_client` 参数化 patch 目标（anthropic/custom_mapping 改为模块内 `build_secure_async_client` 绑定名，ollama 保留 `httpx.AsyncClient`）；`test_openai_runtime_pinning_transport_rewrites_to_pinned_ip` patch 目标改为 `security.ssrf_guard.resolve_and_pin`（SecureAsyncTransport 内部引用模块全局）。

### GREEN 2 — 并发锁、快照补偿、严格本地策略（Critical 1 + Warning 4）

`tests/test_provider_onboarding.py` 已覆盖：并发 create 失败不能回滚成功事务、create/update 与 update/delete 串行化、commit 步骤与补偿步骤失败矩阵（磁盘+内存）、补偿失败聚合主异常链、Ollama 严格本地放行（仅 `http://localhost:11434` 等）与 HTTPS/非规范主机拒绝。新增 2 个 HTTP 层路由校验测试：

```bash
.venv/bin/python -m pytest tests/test_provider_onboarding.py -q
```

GREEN 结果：`42 passed in 3.59s`（原 40 + 新增 missing→404、builtin unavailable→409）。

### GREEN 3 — 旧 key 入口与 secret header（Warning 3、5）

`set_provider_key` 改经 `ProviderService.update` 原子执行（旧 key 旁路修复）；`test_legacy_key_update_preserves_state_when_health_check_fails` 覆盖探活失败不落盘。`test_custom_mapping_rejects_literal_secret_headers` 覆盖 header 中字面量 secret 拒绝（400 不回显）。

### 最终回归

```bash
.venv/bin/python -m pytest tests/test_provider_onboarding.py tests/test_provider_key_reading.py tests/test_model_switching_refactor.py tests/test_provider_catalog.py tests/test_provider_transports.py -q
```

GREEN 结果：`142 passed in 10.44s`。

```bash
.venv/bin/python -m ruff check security/ssrf_guard.py llm_gateway/ llm_gateway/transports/ web/routers/providers.py web/custom_providers.py tests/test_provider_transports.py tests/test_provider_onboarding.py
.venv/bin/python -m compileall -q security/ llm_gateway/ web/
git diff --check
```

- Ruff：`All checks passed`（修复期间清理 `ssrf_guard.py` E402/I001 与 `web/custom_providers.py` F401 死 import）。
- 编译检查：退出码 0。
- 差异空白检查：退出码 0。
- 本修复周期未执行 commit，未添加代码注释。

## 二轮审查整改（task-15-review-v2.md 反馈处理）

二轮审查判定 Spec PASS / Quality 条件性 FAIL，提出 N1–N4。处理如下：

### N1（Important，必修）— 内置 provider 可用性判定回归

`validate_route` 用 `_custom_clients` 成员关系判定内置 provider（mimo/agnes）可用性，但内置 provider 由 `ModelRouter` 专用 client（`_client`/`_agnes_client`）服务、从不进 `_custom_clients`，真实应用保存路由回内置默认 provider 恒 409。

RED：新增 `test_builtin_route_accepts_configured_runtime_client`（runtime 暴露 `_is_client_configured` 且 mimo 已配置时 `validate_route("mimo", "mimo-v2.5")` 应为 None），修复前返回 `'unavailable'`（1 failed）。

GREEN：`ProviderService.validate_route` 提取 `_provider_ready(definition)`——builtin 优先调用 `runtime_router._is_client_configured`（存在时），否则回退 `_custom_clients` 成员判定。回归：

```bash
.venv/bin/python -m pytest tests/test_provider_onboarding.py -q
```

结果：`43 passed in 3.62s`。

### N2（Minor）— legacy POST /models/providers 死豁免

`web/routers/models.py` 创建 provider 路径移除 `is_local_host` 通用豁免，与 `ProviderService._definition` 无通用 localhost 豁免保持一致（legacy 端点仅支持 openai/anthropic，本地 base_url 由服务层拒绝）。

### N3（Minor）— `_save_key_and_register` 死代码

`set_provider_key` 迁移到 `ProviderService.update` 后 `_save_key_and_register` 无调用方（全库仅定义处），删除并清理 `models.py` 随之失效的 `os`/`contextlib`/`_get_cred_dir`/`_key_file` import；`ruff --fix` 重排 import 块。

### N4（Minor）— setup/启动凭证写一致性

- `web/server.py` `_ensure_provider_key_file` 改 `utils.atomic_write` 原子写（原 `write_text` 非原子）。
- `setup._auto_register_providers` / `model_discovery._ensure_custom_provider` 完整迁移到 `ProviderService`（brief Step 3 收尾）涉及 setup 同步→异步改造且会改变「探活失败即拒存」的 setup 语义，判定超出本轮验收范围，作为已知边界记录，未强制迁移（reviewer 亦判定无实际漏洞）。

### 整改后全量验证

```bash
.venv/bin/python -m pytest tests/test_provider_onboarding.py tests/test_provider_key_reading.py tests/test_model_switching_refactor.py tests/test_provider_catalog.py tests/test_provider_transports.py -q
.venv/bin/python -m pytest tests/test_custom_anthropic_provider.py tests/test_frontend_provider_contracts.py tests/test_degraded_mode_any_provider.py tests/test_agnes_provider_routing_bug.py -q
.venv/bin/python -m ruff check security/ssrf_guard.py llm_gateway/ llm_gateway/transports/ web/routers/providers.py web/routers/models.py web/custom_providers.py tests/test_provider_transports.py tests/test_provider_onboarding.py
.venv/bin/python -m compileall -q security/ llm_gateway/ web/
git diff --check
```

结果：聚焦回归 `143 passed`；额外 provider 兼容 `21 passed`；Ruff/compileall/`git diff --check` 全绿。`web/server.py` 存量 import 排序（I001）为既有问题、非本任务引入，未在本次范围处理。

## 终审 F1 收口：统一生命周期入口

- `setup.save_keys` 在写入环境文件前通过应用级 `ProviderService.create/update/bind_builtin` 完成探活与原子 provider 激活；环境文件写入失败时按快照逆序回滚。
- startup restoration 不再直接调用 `register_into_router`，而是复用 `ProviderService` 已恢复的 runtime router。
- model discovery 切换模型前只调用 `ProviderService.validate_route`，不再从配置或环境变量即时构造 runtime client。
- 三个迁移调用面均由静态契约测试约束，不允许重新引入 `register_into_router` 或 setup 凭证文件直写。

### RED/GREEN 证据

- RED：新增入口合同测试后，setup 仍为同步直写、startup/model discovery 仍直接注册，测试因缺少统一服务调用与原子回滚失败。
- GREEN：8 项入口行为测试全部通过，覆盖 setup create/builtin bind、探活失败不持久化、环境写失败回滚新旧 provider、model discovery 路由验证及 startup runtime 复用。
- 关联 provider 回归：`135 passed in 14.41s`。
- Python 编译与差异空白检查通过；迁移文件未再命中 `register_into_router` 或 `_ensure_custom_provider`。
