# Task 17 实施报告：Local AI REST、WebSocket 与旧 Facade

日期：2026-08-11

## 范围

- 新 Local AI 资源 API：devices、catalog、models、downloads、instances，并复用 Task 6 的 storage API。
- WebSocket 事件：`local_ai_device_updated`、`local_ai_download_updated`、`local_ai_instance_updated`。
- 旧 `/api/v1/local-deploy/devices` facade 从权威 DeviceRegistry 翻译响应，服务不可用时保留旧探测降级路径。
- 主应用挂载、Local AI 服务生命周期、下载任务恢复与关闭。
- 全程协调并保留工作区中的未提交 Task 1-18 前置代码；未提交任何 commit。

## TDD 证据

### RED 1：WebSocket 事件合同缺失

命令：

```bash
.venv/bin/python -m pytest tests/test_local_ai_api.py -q
```

结果：收集阶段失败，`ImportError: cannot import name 'local_ai_event' from 'web.ws_hub'`。该失败证明测试先识别到规范事件构造器尚未实现。

### GREEN 1：事件构造器

实现最小 `local_ai_event(resource, record)` 后再次运行。结果：`1 failed, 6 passed`；剩余失败明确指向主应用没有挂载五个新资源路由。

### GREEN 2：主应用路由挂载

挂载 `local_ai_router` 后结果：`7 passed`。

### RED/GREEN 3：下载事件键与删除确认

新增下载 manager 的 `task` 到规范 `download` 键翻译测试，以及删除模型必须携带 `X-Confirm: yes` 测试。RED 为缺少 `local_ai_event_sink` 的导入失败；实现事件适配与删除确认后结果：`9 passed`。

### RED/GREEN 4：服务接入点

新增应用状态接入测试。RED 为缺少 `attach_local_ai_services`；最小实现后转绿。

### RED/GREEN 5：跨资源幂等隔离

新增相同 `request_id` 同时用于 download 与 instance 的行为测试。RED 为实例未启动，证明共享字符串键产生跨资源冲突；改为 `(resource, request_id)` 后结果：`11 passed`。

### RED/GREEN 6：启动恢复

新增 Local AI 初始化必须调用 `downloads.recover()` 的测试。RED 为缺少 `initialize_local_ai_services`；实现并接入 FastAPI lifespan 后结果：`12 passed`。

## P1/P2 复审修复

### P1：复用 Core 权威实例管理器

- RED：新增 `test_local_ai_services_reuse_core_instance_manager`。在复审前实现上独立复现，断言 API 服务必须复用 `core.local_ai_instances`，得到 `AssertionError: 旧实现新建 InstanceManager，未复用 core.local_ai_instances`。
- GREEN：`create_local_ai_services()` 优先读取 `core.local_ai_instances`；仅在 Core 尚未初始化实例管理器时创建，并回写该属性保持兼容。
- 结果：Local AI REST 实例资源与 embedding/reranker 内存集成共享同一实例状态，避免启动、停止和关闭操作落入两套互不一致的 manager。

### P2：限制 WebSocket 事件资源集合

- RED：扩展 `test_websocket_local_ai_events_have_canonical_resource_keys`，要求未知资源 `arbitrary` 抛出 `ValueError`；在复审前实现上独立复现，调用未抛错并得到 `AssertionError: 旧实现接受 arbitrary 并生成非规范 WebSocket 事件`。
- GREEN：`local_ai_event()` 仅接受 `device`、`download`、`instance`，保持三种既有规范事件及 payload 结构不变。
- 结果：阻止拼写错误或非规范资源静默扩散为前端无法消费的 `local_ai_*_updated` 事件，同时不改变合法调用方。

复审定向验证：

```bash
.venv/bin/python -m pytest tests/test_local_ai_api.py::test_local_ai_services_reuse_core_instance_manager -q
.venv/bin/python -m pytest tests/test_local_ai_api.py::test_websocket_local_ai_events_have_canonical_resource_keys -q
```

结果：两项均为 `1 passed`。

## 实现结果

- 所有新 REST 资源统一使用现有 Bearer 鉴权依赖和 `Envelope` 响应。
- 下载创建要求 `model_id`、`destination`、`request_id`，返回 HTTP 202 和 task；重复请求按资源内幂等。
- 实例启动返回 HTTP 202 和 `task_id`，后台启动完成后广播规范 instance 事件。
- 设备重扫广播每个权威设备；下载 manager 事件统一转换为 `download` 资源键。
- 删除模型要求确认头，避免绕过项目既有破坏性操作约束。
- lifespan 初始化 DeviceRegistry、CatalogLoader、ModelRegistry、DownloadManager，并复用 Core 权威 InstanceManager；恢复持久化下载，关闭时取消后台任务并关闭实例。
- 旧设备接口优先翻译 `app.state.local_ai.devices`，不再在权威路径输出固定 VIP 型号或 `3 TOPS INT8` 标签。

## 回归结果

计划指定可用回归：

```bash
.venv/bin/python -m pytest tests/test_local_ai_api.py tests/test_p1_ws_unregister.py -q
```

结果：`23 passed`。

计划列出的 `tests/test_web_auth_enforcement.py` 当前仓库不存在；未伪造或替代该文件。鉴权由 `test_all_local_ai_resources_require_auth` 对五个新集合逐一验证 401。

相关 Local AI 前置回归：

```bash
.venv/bin/python -m pytest tests/test_local_ai_storage.py tests/test_local_ai_downloads.py tests/test_local_ai_instances.py tests/test_local_ai_model_registry.py -q
```

结果：`146 passed`。

静态验证：

```bash
.venv/bin/python -m ruff check web/routers/local_ai.py tests/test_local_ai_api.py
.venv/bin/python -m py_compile web/routers/local_ai.py web/routers/local_deploy.py web/ws_hub.py web/server.py tests/test_local_ai_api.py
git diff --check -- web/routers/local_ai.py web/routers/local_deploy.py web/ws_hub.py web/server.py tests/test_local_ai_api.py task-17-report.md
```

结果：全部通过。

扩展 Ruff 到 `web/routers/local_deploy.py web/ws_hub.py web/server.py` 时发现 21 项既有 import 排序/未使用导入问题；这些文件还承载其他并发任务的大量未提交改动，本次不使用自动修复扩大 Task 17 范围，也不将扩展 Ruff 计为通过。

编辑器诊断因工具将 `/home/orangepi/ai-agent` 判为工作目录外而返回访问拒绝，未计为成功证据。

扩展运行 `tests/test_local_ai_memory_integration.py tests/test_local_ai_instances.py` 得到 `2 failed, 65 passed`。其中 `tests/test_local_ai_instances.py` 的 50 项全部通过；两项失败分别是并发任务改动中的 `EmbedCache.clear` 缺失和 bundled fallback 预期变化，不位于 Task 17 修改范围，且不将该扩展集合计为 Task 17 成功证据。

## 复审第二轮 P1/P2 修复（2026-08-12）

针对二次复审提出的六个阻塞项，按 red-green 逐一修复：

### P1：`request_id` 由必填改为可选并服务端生成

- RED：新增/更新 `test_download_create_requires_destination_and_is_idempotent_by_request_id`，在缺省 `request_id` 时应由服务端生成 UUID 并保持幂等。旧实现因 `request_id` 必填导致 422，测试失败。
- GREEN：`DownloadRequest` 与 `StartInstanceRequest` 的 `request_id` 改为可选，缺省时生成 UUID；幂等键 `(resource, request_id)` 保持不变。

### P1：下载创建前的存储策略服务端校验

- RED：目标路径越界/非法时应返回 422；旧实现未校验，测试失败。
- GREEN：创建下载前调用 `StoragePolicy.validate_destination(destination, model.download_size)`，失败返回 HTTP 422，阻断路径绕过/SSRF 风险。

### P1：实例启动失败的可观测 WebSocket 事件

- RED：新增 `test_instance_start_failure_publishes_retryable_websocket_event`，后台启动异常时应广播 `status=failed` 且 `error.retryable=True` 的规范 instance 事件。旧实现吞异常，测试失败。
- GREEN：`_start_instance` 捕获异常后广播 `local_ai_instance_updated`（`operation=start, status=failed, error.code=instance_start_failed, retryable=True`）。

### P1：下载恢复的损坏状态隔离

- RED：`test_recover_isolates_corrupt_state_file` 要求整体 JSON 损坏时 `recover()` 返回 `[]`、`list()` 为空且**不破坏**原始损坏字节；`test_recover_isolates_corrupt_entry_and_keeps_valid_tasks` 要求单条损坏被跳过而保留其余有效任务并持久化。
- GREEN：`recover()` 在 `JSONDecodeError/OSError/UnicodeError` 或结构非法时返回空列表且不覆盖原文件；逐条 `from_dict` 时对 `KeyError/TypeError/ValueError/OSError` `continue` 跳过，随后仅持久化有效任务。
- 验证：`pytest tests/test_local_ai_downloads.py -k recover -q` → `3 passed`。

### P2：旧 facade active 设备 ID 翻译

- RED：`test_legacy_devices_endpoint_translates_authoritative_devices` 要求 `active` 基于权威设备 ID（如 `cpu:0`）判定，而非旧配置短 ID 直接比较。
- GREEN：`local_deploy_devices` 先将 `local_deploy.device` 配置解析为权威 `current`（精确匹配 `device.id`，回退匹配 `device.kind`），再以翻译后的 `current` 判定 `active`。

### P2：请求指纹冲突检测

- RED：`test_reusing_download_request_id_with_different_input_is_rejected` 要求同 `request_id` 复用但输入不同（`model_id/destination`）时返回 409。
- GREEN：`create_download`/`start_instance` 记录 `request_inputs[(resource, request_id)]`，同 ID 异参返回 409，同 ID 同参返回缓存结果保持幂等。

### 第二轮回归结果

```bash
.venv/bin/python -m pytest tests/test_local_ai_api.py tests/test_local_ai_downloads.py \
  tests/test_local_ai_device_registry.py tests/test_local_ai_docs_contract.py -q
```

结果：`2 failed, 133 passed`。

- Task 17 责任范围全绿：`test_local_ai_api.py` 19 passed、`test_local_ai_downloads.py` 18 passed、`test_local_ai_docs_contract.py` 17 passed。
- 2 项失败均为 `test_local_ai_device_registry.py::test_scan_recomputes_degraded_hardware_from_healthy_backend[False/True]`，涉及 DeviceRegistry `scan` 的降级重算语义（Task 11 范畴）。经核对 `local_ai/devices/` 工作区无任何未提交改动（`git diff --stat` 与 `git status` 均空，`registry.py` 已被追踪且未修改），确认该失败非本次 Task 17 引入，属并行 Task 11 的测试/实现漂移，如实记录不计入 Task 17 成功证据。

## 工作区协调

- Task 17 开始时已有大量未提交 Local AI、provider、frontend、打包及报告文件；未重置、未暂存、未覆盖这些前置成果。
- 实施期间检测到其他并发改动继续修改 Task 18/19 相关测试、组件和构建产物；Task 17 只改动自己的后端/API 测试与本报告。
- `web/routers/local_ai.py`、`tests/test_local_ai_api.py` 是既有未提交草稿上的增量完善，不能通过相对 HEAD 的 diff 单独代表 Task 17 全部内容。
- 未执行 `git add`、`git commit`、`git push`。

## 结论

Task 17 的 REST、WebSocket、旧 facade、主应用挂载和生命周期已按严格 TDD 完成，两轮复审 P1/P2 均已按最小兼容方案关闭（含 request_id 可选生成、存储策略校验、实例失败可观测、恢复损坏隔离、facade active 翻译、请求指纹冲突检测）。Task 17 责任范围内目标测试、下载与文档契约回归全绿；剩余 2 项 DeviceRegistry scan 失败经核对属并行 Task 11 漂移、非本次引入，已如实记录不计入 Task 17 成功证据。指定但不存在的鉴权测试文件亦如实记录，工作区保持未提交状态。
