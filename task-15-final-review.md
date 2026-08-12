# Task 15 终审：Atomic Provider Onboarding and Route Validation

**审查日期：** 2026-08-11  
**审查类型：** 当前工作树独立终审  
**审查基线：** `docs/superpowers/plans/2026-08-09-local-ai-platform.md:706-758`、`.superpowers/sdd/task-15-brief.md`、`task-15-report.md`、`task-15-review.md`  
**参考材料：** `task-15-review-v2.md`、`task-15-review-final.md`  
**终审结论：** **规范 FAIL / 质量 FAIL（条件性）**

## 结论摘要

Task 15 的核心新增实现质量总体较高。首轮审查的并发回滚、DNS rebinding、旧 key API、补偿失败、字面 secret header、内置 provider 可用性和 chat 路由双写问题均已真实整改；聚焦回归 143 项、额外 provider 兼容回归 21 项、Ruff、compileall 与 `git diff --check` 全部通过。

但当前实现仍未完成计划 Task 15 Step 3 明确要求的迁移收口：setup 保存、启动恢复和 model discovery 仍直接写凭证/配置或直接调用 `register_into_router`，绕过 `ProviderService` 的 staging、health check、按 provider 锁、原子提交、补偿回滚、catalog 定义和 capability report。该事实同时与实施报告“Setup provider 凭证测试接入 catalog/service”“启动生命周期初始化 service”容易形成的完成性表述不一致。

这不是纯风格遗留。`POST /setup/keys` 默认可在不启用 `test_required` 时进入 `_auto_register_providers()`；该函数先非原子覆盖凭证文件，再分别写配置、替换 runtime，任一步失败都可能留下跨资源不一致。Task 15 的核心规范是 provider 生命周期统一原子化，因此不能把计划明确要求的三条旁路作为“可接受边界”后仍判 Spec PASS。

除该迁移缺口外，未发现新的 Critical 安全问题。建议完成 Step 3 迁移并补齐旁路回归测试后再验收；其余当前整改可保留。

## 阻塞问题

### F1（Important）— Step 3 迁移未完成，provider 生命周期仍存在三类旁路

**规范依据：** `.superpowers/sdd/task-15-brief.md:39-41` 和主计划 `docs/superpowers/plans/2026-08-09-local-ai-platform.md:744-746` 明确要求：迁移 setup、startup restoration、model discovery 和 sub-agent resolution 到 `ProviderCatalog`/`ProviderService`，调用点迁移后再移除重复映射。

**当前证据：**

- `web/routers/setup.py:649-675,967-1032`：`save_keys()` 调用同步 `_auto_register_providers()`；后者使用 `Path.write_text()` 写 provider 凭证、`cfg.set()` 写 provider 配置、`register_into_router()` 替换 runtime，没有 `ProviderService` staging/health check/锁/补偿。
- `web/server.py:20-40,127-155`：启动恢复仍由 `_register_all_providers()` 读取配置和凭证后直接 `register_into_router()`，未通过 catalog/service 恢复入口；`ProviderService` 到 `web/server.py:791` 才初始化。
- `web/routers/model_discovery.py:483-559`：切换聊天模型前调用 `_ensure_custom_provider()`，从配置或环境变量取凭证后直接 `register_into_router()`，绕过统一 route/provider 生命周期服务。
- `web/routers/setup.py:552-572` 仅把部分 setup 凭证“测试”接入临时 `ProviderService.test()`；这不等于把保存和运行时注册迁移到 service。
- sub-agent provider 解析已迁移到 catalog metadata/凭证别名，属于已完成部分，但不能抵消其余三个明确调用面的缺口。

**影响：**

- setup 默认 `test_required=False` 时，未探活凭证可直接覆盖凭证文件并投入 runtime；探活失败不触发 `ProviderService` 的旧状态保留语义。
- 凭证、配置和 runtime 分三步修改，后续步骤异常时没有跨资源补偿；`Path.write_text()` 还绕过已经建立的 `atomic_write(..., mode=0o600)` 凭证模式。
- startup/model discovery 继续维护第二套 provider 协议映射和 client 构造入口，catalog/service 不再是 provider 定义与运行时交换的唯一权威入口。
- 现有 Task 15 测试主要覆盖 canonical API 与 legacy models API，没有测试 setup/startup/model discovery 不得绕过 service，因而全绿不能证明 Step 3 完成。

**整改要求：**

1. 将 setup 保存流程改为异步调用应用级 `ProviderService.create/update`；若产品允许“保存但暂不启用”，应在服务合同中显式建模，而不是恢复直写旁路。
2. 将启动恢复改为由 catalog/service 统一恢复 definition、credential 和 runtime client；避免 `_register_all_providers` 继续复制协议映射。
3. 将 model discovery 的即时注册改为查询 service/catalog 状态并使用统一恢复或更新入口；route 切换只消费已验证 provider。
4. 增加 setup 探活失败不改磁盘/runtime、setup 中途失败全回滚、startup 使用持久化 mapping、model discovery 不直注册的回归测试。

## 已通过项

| 验收项 | 结果 | 终审批注 |
|---|---|---|
| `ProviderService.test(draft) -> CapabilityReport` | PASS | transport 在 `finally` 中关闭；协议适配与 capability report 合同完整 |
| create/update/delete 原子性 | PASS | provider 级规范化 `asyncio.Lock`、锁内前置检查、提交快照与全量补偿均已实现 |
| 并发事务隔离 | PASS | 双 create、create/update、update/delete 使用 barrier/gather 的确定性测试覆盖 |
| 补偿可靠性 | PASS | 补偿动作逐项执行，失败聚合到主异常上下文；配置删除恢复内存，凭证仓库使用原子写 |
| canonical provider API | PASS | draft test、列表、CRUD、capabilities、models 均存在并受认证保护；删除保留确认头 |
| legacy models CRUD/key | PASS | create/update/delete/key 已委托 `ProviderService`，无效 key 保持旧凭证与 runtime |
| route validation | PASS | missing/disabled/runtime/model 均校验；builtin 委托 `ModelRouter._is_client_configured`，N1 回归已解除 |
| chat route 持久化 | PASS | route 与 `models.chat_model` 使用 `set_many` 单次持久化，失败回滚 runtime 表 |
| SSRF 边界 | PASS | 非 Ollama 无 localhost 通用豁免；Ollama 严格限制回环 11434；请求期重新解析并绑定 IP，禁用自动重定向 |
| 凭证响应边界 | PASS | API 不返回 api_key；custom mapping header 仅允许受控占位符，字面 secret 被拒绝 |
| setup/startup/model discovery/sub-agent 迁移 | FAIL | sub-agent 与部分 test 路径已迁移，但 setup 保存、startup restoration、model discovery 仍是直接写/直接注册 |
| 规范测试与静态检查 | PASS | 本次独立复跑全部通过，详见验证证据 |

## 代码质量

**正向评价：**

- `ProviderService` 把 definition 构造、凭证选择、探活、候选 client 构造、提交和补偿分层，事务边界清晰。
- SSRF 修复不是仅做保存前 URL 校验，而是将请求期 DNS 解析、危险地址校验和连接 IP 绑定下沉到统一 transport，覆盖运行时真实调用链。
- N1 使用 builtin 专属 `_is_client_configured` 语义，避免用 `_custom_clients` 错判 mimo/agnes；非 builtin 行为保持不变。
- 测试覆盖包含真实并发编排、提交/补偿失败矩阵、DNS rebinding、重定向、旧 API 和 HTTP 状态码，关键风险有可复现保护。

**质量扣分：**

- 计划要求的生命周期唯一入口尚未成立，生产代码同时保留 service 路径与三类直接注册路径，后续修改容易再次产生状态分裂。
- `task-15-report.md:175-178` 已承认 setup/model discovery 未完整迁移，却将其判定为超范围；这与 Task 15 brief Step 3 的文字要求直接冲突，报告的完成状态偏乐观。
- 既有 `task-15-review-final.md` 将 N4 的部分修复认定为整体 PASS，实际只修复了 `web/server.py` 的一个原子写调用点，没有关闭 setup 的非原子凭证写和直接 runtime 注册。

因此质量结论为 **FAIL（条件性）**：当前 canonical 服务本身可评为良好，但任务交付仍存在一个 Important 级架构/一致性缺口。完成 F1 后，如回归保持全绿，可转为 Quality PASS。

## 验证证据

```bash
.venv/bin/python -m pytest tests/test_provider_onboarding.py tests/test_provider_key_reading.py tests/test_model_switching_refactor.py tests/test_provider_catalog.py tests/test_provider_transports.py -q
```

结果：`143 passed`，退出码 0。

```bash
.venv/bin/python -m pytest tests/test_custom_anthropic_provider.py tests/test_frontend_provider_contracts.py tests/test_degraded_mode_any_provider.py tests/test_agnes_provider_routing_bug.py -q
```

结果：`21 passed in 4.36s`，退出码 0。

```bash
.venv/bin/python -m ruff check security/ssrf_guard.py llm_gateway/ web/routers/providers.py web/routers/models.py web/custom_providers.py tests/test_provider_transports.py tests/test_provider_onboarding.py
.venv/bin/python -m compileall -q security/ llm_gateway/ web/
git diff --check
```

结果：Ruff `All checks passed!`；compileall 与差异空白检查退出码均为 0。

旁路静态核验：

```bash
git grep -n -E '_auto_register_providers|_ensure_custom_provider|register_into_router' -- web/server.py web/routers/setup.py web/routers/model_discovery.py
git grep -n -E 'write_text|cfg\.set|register_into_router' -- web/routers/setup.py
```

结果：稳定命中 `web/routers/setup.py:675,967-1032`、`web/server.py:24,40,127-155`、`web/routers/model_discovery.py:497,517-559`；setup provider 凭证直写命中 `web/routers/setup.py:999`。

## 最终判定

- **规范：FAIL。** 原子 CRUD、统一路由校验与安全边界已达标，但计划 Task 15 Step 3 明确要求的 setup、startup restoration、model discovery 迁移未完成；这是显式验收项，不应降格为无条件接受的边界。
- **质量：FAIL（条件性）。** 无新增 Critical，canonical `ProviderService` 及其整改质量良好；但未收口的生产旁路仍可造成凭证/配置/runtime 不一致，并削弱服务作为唯一生命周期入口的不变量。
- **发布建议：暂不按 Task 15 完成验收。** 完成 F1 四项整改与相应测试后可快速复审；现有通过项无需返工。
