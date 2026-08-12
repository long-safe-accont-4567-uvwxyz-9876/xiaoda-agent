# Task 15 审查：Atomic Provider Onboarding and Route Validation

**审查日期：** 2026-08-11  
**审查范围：** `.superpowers/sdd/task-15-brief.md`、`task-15-report.md`、`.superpowers/sdd/task-15-review-local-ai.md`、Task 15 提交 `dc8ecb7` 中的 provider 生命周期实现及相关调用链  
**Spec 结论：** FAIL  
**质量结论：** FAIL

## 结论摘要

Task 15 已提供 provider draft 测试、CRUD、能力报告、模型发现与路由校验 API，既有 42 项指定测试通过，单请求下的探活失败也确实不会修改配置、凭证和 runtime。但是“原子 provider 生命周期”的核心验收条件仍未成立：并发创建同一 provider 时，失败请求的补偿回滚会删除另一请求已经成功提交的全部状态；旧凭证更新 API 仍绕过 `ProviderService`，可把未探活凭证和 client 直接投入运行。

安全方面存在可利用的 SSRF 边界缺口。所有协议都无条件放行 localhost 类主机，而不只限于明确的 Ollama 场景；公网域名虽然在保存前解析校验并写入 pin cache，但实际 HTTP/OpenAI client 从未使用 pinned IP，攻击者控制 DNS 时仍可在校验后重绑定到私网或云元数据地址。自定义 mapping 还允许把字面凭证放入普通 headers，随后明文持久化并由查询 API返回。

因此，本次实现不能以“完成”状态验收。建议先修复两个 Critical，再关闭凭证旁路、回滚可靠性和内置 provider 路由可用性缺口，并增加真实并发与 SSRF 重绑定测试。

## Critical

### 1. 并发创建的失败回滚会删除另一请求已成功提交的 provider

**位置：** `llm_gateway/provider_service.py:87-103,199-215`

`create()` 在探活前检查 catalog 是否存在，随后在 `_stage()` 中发生 `await`，但整个“检查 → 探活 → 提交”过程没有按 provider 加锁或版本校验。两个同 ID 请求可同时通过不存在检查并并发探活：第一个请求提交成功后，第二个请求先覆盖凭证与配置，随后在 `catalog.register()` 因重复注册失败；第二个请求的 `_commit_create()` 异常分支会无条件删除凭证、配置、catalog 项和 runtime client，连第一个请求已经成功返回的状态也一起删除。

这不是单纯的 last-write-wins，而是一个失败事务撤销另一个成功事务，直接破坏 brief 要求的原子性。当前测试只有顺序失败注入，没有 `asyncio.gather()`、barrier 或同 ID 并发创建覆盖。

**建议：** 为每个 provider 的 create/update/delete 建立共享异步锁，并在锁内重新检查前置条件；补偿操作必须只撤销本事务实际写入且仍由本事务拥有的版本。新增双 create、create/update、update/delete 的确定性并发测试。

### 2. SSRF 校验可被 localhost 放行与 DNS rebinding 绕过

**位置：** `llm_gateway/provider_service.py:261-276,344-383`、`security/ssrf_guard.py:228-274,288-314`、`llm_gateway/transports/custom_mapping.py:39-45,125-179`

认证用户可控制 provider 的完整 `base_url` 主机和协议，并触发 `/providers/test`、create/update、capabilities 与 models API 发起服务端请求。当前 `_definition()` 对 `localhost`、`127.0.0.1`、`::1`、`0.0.0.0`、`host.docker.internal` 等一律跳过 `validate_url()`，且没有约束 protocol 必须是 Ollama、端口必须是 11434 或目标必须满足 Ollama 握手，因此可探测和访问 Web 服务所在主机或容器宿主机的任意 HTTP 服务。

对于公网域名，`validate_url()` 虽把解析结果写入 `_PIN_CACHE`，但 ProviderService 构造的 `httpx.AsyncClient`、OpenAI `AsyncOpenAI` 和 Anthropic client 均继续使用原 hostname，未调用 `get_pinned_ip()`，也未把连接绑定到已校验 IP。攻击者控制 DNS 时可在初次校验返回公网 IP，再在 health check 或后续模型请求时解析到私网/链路本地地址，完整绕过声明的 DNS pinning。

**建议：** 删除通用 localhost 豁免；如产品必须支持本地 Ollama，应使用显式、本机管理员控制的本地 provider 类型与严格端口/握手约束。所有远程 transport 必须通过真正绑定已验证 IP 的统一安全 HTTP transport 发起请求，并在每次重定向时重新校验目标。增加 localhost 非 Ollama、IPv4/IPv6、DNS 重绑定和重定向到元数据地址的端到端测试。

## Warning

### 3. 旧凭证更新 API 仍是非原子旁路

**位置：** `web/routers/models.py:184-208`

`POST /models/providers/{pid}/key` 仍调用 `_save_key_and_register()`：先直接覆盖凭证文件，再直接替换 runtime client，不执行 capability/health check，不更新 `ProviderService` 的 capability report，也没有失败补偿。无效 key 会立即替换工作中的凭证和 client，而 API 仍返回 200；写文件成功、构建 client 失败时还会留下磁盘/runtime 不一致。

实施报告称旧 provider 入口已迁移以避免非原子旁路，但该写凭证入口仍可改变 provider 生命周期关键状态，和 brief 的“credential transaction”要求冲突。

**建议：** 将凭证轮换纳入 `ProviderService.update()` 的同一 staging/commit/rollback 路径，或删除该旁路并让调用方统一使用 provider update API；增加无效 key 保持旧凭证、旧 runtime 和旧报告不变的 API 测试。

### 4. 补偿回滚本身失败时会留下部分提交状态并掩盖原始异常

**位置：** `llm_gateway/provider_service.py:124-153,199-245`、`web/config_service.py:336-368`、`llm_gateway/provider_service.py:35-55`

create/update/delete 的异常分支依次执行多项补偿，但补偿操作没有独立保护或聚合错误。任一 `credentials.write/delete`、`config.set/delete` 或 catalog 恢复再次失败，后续补偿便不再执行，并以补偿异常覆盖原始提交异常。尤其 `ConfigService.delete()` 在 `_save()` 失败时不像 `set()` 那样恢复内存值；凭证写入也直接 `Path.write_text()`，没有临时文件替换。因此报告中“提交阶段异常时恢复全部旧状态”的结论只对理想化的单点、一次性失败 mock 成立。

**建议：** 将配置与凭证快照/恢复做成明确事务对象，凭证使用同目录临时文件加原子替换；每项补偿独立执行并在最后报告复合错误。测试每一个提交步骤和每一个补偿步骤都失败的矩阵，并同时断言内存与磁盘状态。

### 5. 自定义 header 可把凭证绕过凭证仓库存入普通配置并由 API 返回

**位置：** `llm_gateway/provider_service.py:304-310,329-337,369-383`、`web/routers/providers.py:28-47`

Custom Mapping 允许 headers 中出现任意无花括号字符串。调用方可提交 `{"Authorization": "Bearer real-secret"}` 或其他字面 token；该值进入 `models.providers` 普通配置、写入 `webui_overrides.json`，并由 `GET /providers` 的 `headers` 字段原样返回。当前只保证独立 `credentials.api_key` 不回显，无法保证 provider 凭证不进入响应和普通配置，和报告的安全边界声明不一致。

**建议：** 持久化与响应合同只允许受控占位符，不接受疑似凭证的字面 header 值；需要多个秘密时使用命名 credential references，并统一进入凭证仓库。增加 literal Authorization/X-API-Key 被拒绝或脱敏的测试。

### 6. 内置 provider 路由未校验运行时与凭证可用性

**位置：** `llm_gateway/provider_service.py:177-191`、`web/routers/models.py:251-268`

`validate_route()` 只对非内置 provider 检查 enabled 和 `_custom_clients`。对 catalog 中的内置 provider，只要 ID 存在便通过，即使对应 runtime client 为 `None`、凭证缺失或健康报告明确不可用。用户可把 chat 路由切换到不可用的内置 provider 并收到 200，直到实际模型调用才失败。这没有满足 Task 15 对 provider 存在、启用、运行时可用和模型有效性的统一路由验证要求。

**建议：** 让 catalog definition 提供统一 runtime availability 判定，不以 builtin 绕过；路由更新前校验凭证/client 和最近 capability report，并为缺凭证、client 为 None、health report unavailable 的内置 provider 增加 409 测试。

### 7. chat 路由更新与 `models.chat_model` 二次持久化不是一个原子操作

**位置：** `web/routers/models.py:312-337`、`model_router.py:483-517`

`registry.update_route()` 已先修改并持久化 `models.routes.chat`，随后 handler 再单独 `cfg.set("models.chat_model", ...)`。第二次写盘失败时，请求返回 500，但路由内存和 `models.routes.chat` 已成功改变，`models.chat_model` 仍是旧值；重启恢复链又同时读取两处配置，可能产生不一致行为。

**建议：** 使用一次配置批量事务提交 route 与 chat_model，并在任一持久化失败时同时恢复 runtime route；增加第二次写盘失败的回归测试。

## Info

### 1. API 错误合同和双入口行为仍不完全一致

新 `/providers` 与旧 `/models/providers` 对内置 provider、可选凭证、错误文本及状态码采用不同规则；旧列表还会隐藏无 key 的合法可选认证 provider。短期保留兼容入口可以接受，但应明确 canonical API，并让旧入口仅做无额外业务逻辑的适配，避免后续再次形成状态旁路。

### 2. 报告中的最终测试数字无法由 brief 指定命令直接复现

本次按 brief 指定的三个测试文件复跑得到 `42 passed in 8.75s`；报告中的 `103 passed` 使用了扩展测试集合。扩展验证本身有价值，但报告应同时明确列出 brief 指定集合的独立结果，避免把更大集合数字误解为 Task 15 专属用例数。

## Spec 核对

| 要求 | 结果 | 审查结论 |
|---|---|---|
| `ProviderService.test(draft) -> CapabilityReport` | PASS | 接口存在，transport 在 finally 中关闭 |
| create/update/delete 带 rollback | FAIL | 顺序单点失败可补偿，但并发 create 会撤销成功事务，补偿失败也会残留部分状态 |
| staged client construction 与 capability testing | PASS | create/update 在提交前完成 health check 与候选 client 构造 |
| credential transaction | FAIL | 旧 key API 仍直写凭证/runtime；凭证文件写入非原子 |
| config transaction 与 runtime swap | PARTIAL | 单请求 happy path 成立；跨资源、并发及二次回滚不成立 |
| provider draft test、CRUD、capability、discovery API | PASS | 新 API 均存在且受 `get_current_user` 保护，delete 保留确认头 |
| route validation | PARTIAL | 自定义 provider 的 missing/disabled/runtime/model 有校验；内置 provider 可用性绕过 |
| setup/startup/sub-agent 迁移 | PARTIAL | 已接入 catalog/service，但 runtime 启动恢复仍依赖旧注册链，重复映射尚未完全移除 |
| SSRF 安全边界 | FAIL | localhost 通用豁免与未落实的 DNS pinning 可绕过 |
| 凭证不进入配置/API | FAIL | 独立 api_key 不回显，但 literal custom headers 可明文持久化并返回 |
| 指定测试 | PASS | 42 项指定测试通过，但缺少并发、补偿失败、DNS rebinding 与旧 key 旁路测试 |
| Task 15 commit | 已执行于混合提交 | Task 15 与 Local AI 等 35 个文件共同进入 `dc8ecb7`，不影响代码结论，但不符合 brief 的独立 lifecycle commit 形态 |

## 验证证据

- `.venv/bin/python -m pytest tests/test_provider_onboarding.py tests/test_provider_key_reading.py tests/test_model_switching_refactor.py -q`：`42 passed in 8.75s`。
- `.venv/bin/python -m ruff check llm_gateway/provider_service.py web/routers/providers.py tests/test_provider_onboarding.py`：`All checks passed!`。
- `git diff e5f6d73..HEAD --check -- <Task 15 files>`：退出码 0。
- `provider_service.py` 编辑器诊断：无诊断。
- CodeRabbit CLI 0.7.2 已安装并认证；远程审查只返回 connecting 状态，没有发现项输出，因此不将其计作 PASS 证据。
- 代码库语义索引本次两次获取均超时；结论基于完整文件、调用链、提交差异和本地测试，未将索引失败解释为无问题。

## 最终判定

- **Spec：FAIL。** API 面与顺序 happy path 基本齐全，但原子回滚、credential transaction、统一 route availability 与 SSRF 边界均未达到 brief 的核心验收标准。
- **质量：FAIL。** 存在 2 个 Critical、5 个 Warning；其中并发回滚可删除成功事务，SSRF 可访问本机服务或通过 DNS rebinding 进入受保护网络，合并前必须修复。
