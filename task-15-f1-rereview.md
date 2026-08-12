# Task 15 F1 复审：统一 Provider 生命周期入口

**审查日期：** 2026-08-12  
**审查类型：** 当前工作树独立复审  
**审查基线：** `task-15-final-review.md` F1、`.superpowers/sdd/task-15-brief.md` Step 3、`task-15-report.md` 终审 F1 收口  
**审查范围：** `web/routers/setup.py`、`web/server.py`、`web/routers/model_discovery.py`、`llm_gateway/provider_service.py`、`tests/test_provider_onboarding.py` 及关联 provider 回归  
**复审结论：** **规范 PASS / 质量 PASS**

## 结论摘要

F1 的三类直接旁路已经完成结构性迁移：setup provider 保存改为调用应用级 `ProviderService.create/update/bind_builtin`，server 启动恢复不再直接调用 `register_into_router`，model discovery 切换模型前改为调用 `ProviderService.validate_route`。三处目标文件已无 `register_into_router`、`_ensure_custom_provider` 或 setup provider 凭证文件直写，startup 也能由 `ProviderService` 构造阶段依据持久化配置和凭证重建 custom runtime client。

本轮已关闭 setup 外层事务的既有 provider 回滚缺口。`ProviderService.snapshot()` 在变更前捕获 definition、config record、credential、capability report 与 custom/builtin runtime client；`restore_snapshot()` 不调用 `_stage()`，直接逐资源恢复提交前状态。因此旧凭证已失效或旧端点暂不可用时，`.env` 写失败仍能恢复完整快照。

setup 补偿改为逆序调用 `restore_snapshot()`，并逐项捕获异常；所有 provider 都尝试恢复后，以 `ExceptionGroup` 聚合失败。新增真实 `ProviderService` 测试覆盖 custom 旧凭证不可用、builtin 旧 client 恢复，以及单项补偿失败仍继续处理其余快照。

严格 TDD 证据完整：真实 custom 回归首次运行稳定失败于 `ProviderService._stage()`，错误为 `old unavailable`，并观察到 config 仍为新 label；builtin 与逐项补偿测试同样先失败。最小实现后 3 项定向测试、88 项 onboarding 回归和 199 项五文件 provider 聚焦回归全部通过。F1 的“入口统一”与“setup 中途失败全回滚”均已闭合。

## 已整改问题

### F1-R1（Important）— setup 环境写失败时无探活恢复 provider 快照

**修复证据：**

- `llm_gateway/provider_service.py`：新增不可变 `ProviderSnapshot`，快照覆盖 definition、config、credential、report、runtime 类型、client 是否存在及 client 对象。
- `ProviderService.snapshot()`：在 setup 变更前读取完整状态；builtin client 通过 runtime router 的 `get_builtin_client()` 获取。
- `ProviderService.restore_snapshot()`：不调用 `test()`/`_stage()`，直接恢复 config、credential、catalog、custom/builtin runtime 与 report；单资源失败时仍继续其余恢复并聚合异常。
- `web/routers/setup.py`：create/update/bind 前捕获快照，`.env` 写失败或 provider 部分提交失败时逆序恢复；单 provider 恢复失败不会阻断更早快照。
- `model_router.py`：新增对称的 `get_builtin_client()`，使 builtin 快照能保存并恢复原 client 对象。

**行为结果：**

- `.env` 写失败后，旧凭证是否仍可探活不再影响补偿。
- custom provider 的旧 config、credential、catalog definition、runtime client 与 report 保持对象/值级一致。
- builtin provider 的旧 credential、专用 runtime client 与 report 恢复。
- 多 provider 补偿逐项尽力执行，失败统一聚合，不跳过其余 provider。

**RED 证据：**

使用真实 `ProviderService`、内存 config/credential/runtime：新凭证探活通过，旧凭证固定返回不可用，随后强制 `.env` 写失败。

```text
ProviderConnectionError: old unavailable
assert config.get("models.providers.siliconflow") == old_record
E AssertionError: 新 label 仍为 SiliconFlow 硅基流动
```

**新增回归：**

- `test_setup_env_write_failure_restores_real_provider_snapshot_when_old_credential_is_unavailable`
- `test_setup_env_write_failure_restores_builtin_client_without_old_credential_health_check`
- `test_setup_provider_rollback_continues_after_single_snapshot_restore_failure`

## 已通过项

| F1 验收项 | 结果 | 复审批注 |
|---|---|---|
| setup 保存统一调用应用级 ProviderService | PASS | `save_keys()` 已异步调用 `_auto_register_providers()`；custom 走 create/update，builtin 走 bind_builtin，探活失败发生在 `.env` 持久化前 |
| setup 探活失败不持久化新环境 | PASS | 对应行为测试通过，代码顺序也满足先 stage 后写 `.env` |
| setup 中途失败全回滚 | PASS | 真实 custom/builtin 快照均无探活恢复；多 provider 补偿逐项执行并聚合失败 |
| startup 不直接注册 runtime | PASS | `_register_all_providers` 已删除，`ProviderService._restore_custom_definitions()` 统一重建 persisted custom client |
| model discovery 不即时构造 client | PASS | `_ensure_custom_provider` 已删除，切换前只消费 `provider_service.validate_route()` |
| 三处旁路静态约束 | PASS | 目标文件无 `register_into_router`、`_ensure_custom_provider`、`provider_{pid}.key` 命中 |
| provider 聚焦回归 | PASS | 当前工作树 5 文件合计 `199 passed in 27.52s` |
| 编译与差异空白检查 | PASS | compileall 和目标范围 `git diff --check` 均退出码 0 |

## 质量评价

**正向评价：**

- 三个明确调用面已从直接 client 构造和凭证直写迁到 catalog/service，F1 原先指出的架构分叉显著收敛。
- model discovery 的职责边界更清晰：只验证并切换 route，不再隐式修改 provider 生命周期。
- startup 通过 `ProviderService` 构造恢复 persisted definition/runtime，协议与 client 构造映射不再复制在 `web/server.py`。
- 新增测试覆盖 setup create/builtin bind、探活失败不写环境、model discovery 服务消费和禁止三处直注册，方向正确。

**质量结论：**

- 快照恢复与正常变更入口分离，补偿语义不再依赖外部 provider 健康状态。
- custom 与 builtin runtime 都保存原 client 对象，避免用旧凭证重建出行为不同的新 client。
- 单资源与单 provider 补偿都采用尽力恢复并聚合异常，主失败信息仍可追踪。
- 新增生产服务级测试替代 fake service 对关键原子合同的证明；限定 Ruff、编译和差异空白检查通过。

## 报告一致性

`task-15-report.md:192-204` 对三处入口迁移、静态禁直注册和 provider 回归通过的描述基本可核实，但以下内容需更正：

- “环境写失败回滚新旧 provider”仅由 fake service 测试证明，真实 `ProviderService` 的既有 provider 回滚存在上述失败路径，不能列为完整 GREEN。
- 报告记录“关联 provider 回归 135 passed”，当前工作树同一五文件范围实测为 `196 passed`；测试数变化本身不是缺陷，但应注明命令、工作树状态和日期，避免证据不可复现。
- 报告称“8 项入口行为测试”，当前按 F1 关键词独立选择为 10 项通过；应以具体测试名或固定命令代替易漂移计数。
- startup 的“service 已恢复 runtime router”由 ProviderService 重启测试与轻量对象复用测试组合证明，建议在报告中区分两类证据，避免把后者描述成完整 lifespan 恢复测试。

## 验证证据

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest \
  tests/test_provider_onboarding.py tests/test_provider_key_reading.py \
  tests/test_model_switching_refactor.py tests/test_provider_catalog.py \
  tests/test_provider_transports.py -q \
  -p pytest_asyncio.plugin -p pytest_timeout
```

最终复验结果：`199 passed in 12.81s`，退出码 0；完整输出保存在 `/tmp/task15_f1_focus_final.log`。

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest \
  tests/test_provider_onboarding.py::test_setup_env_write_failure_restores_real_provider_snapshot_when_old_credential_is_unavailable \
  tests/test_provider_onboarding.py::test_setup_env_write_failure_restores_builtin_client_without_old_credential_health_check \
  tests/test_provider_onboarding.py::test_setup_provider_rollback_continues_after_single_snapshot_restore_failure \
  -q -p pytest_asyncio.plugin -p pytest_timeout
```

结果：`3 passed in 1.23s`，退出码 0。完整 onboarding 文件复测为 `88 passed in 6.35s`。

```bash
.venv/bin/python -m ruff check llm_gateway/provider_service.py tests/test_provider_onboarding.py
.venv/bin/python -m compileall -q llm_gateway/provider_service.py \
  model_router.py web/routers/setup.py \
  tests/test_provider_onboarding.py
git diff --check -- llm_gateway/provider_service.py model_router.py \
  web/routers/setup.py tests/test_provider_onboarding.py task-15-f1-rereview.md
```

结果：限定 Ruff `All checks passed!`；compileall 与差异空白检查退出码均为 0。

```bash
git grep -n -E 'register_into_router|_ensure_custom_provider|provider_\{pid\}\.key' \
  -- web/routers/setup.py web/server.py web/routers/model_discovery.py
```

结果：零命中。

补充说明：工作树存在本任务开始前的其他未提交改动；本轮只修改 `llm_gateway/provider_service.py`、`model_router.py`、`web/routers/setup.py`、`tests/test_provider_onboarding.py` 与本报告，未提交任何 commit。

## 最终判定

- **规范：PASS。** setup、startup、model discovery 的统一入口保持成立，且 `.env` 写失败时可在旧凭证不可用条件下恢复完整 provider 快照。
- **质量：PASS。** 快照恢复不重新探活，custom/builtin runtime 与 report 均覆盖；补偿逐项执行并聚合失败，真实服务回归验证关键合同。
- **发布建议：可关闭 F1。** 199 项 provider 聚焦回归、限定 Ruff、compileall 与 `git diff --check` 全部通过；本轮未提交代码。
