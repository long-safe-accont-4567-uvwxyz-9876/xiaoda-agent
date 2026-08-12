# Task 15 最终复审：Atomic Provider Onboarding and Route Validation（N1–N4 整改核验）

**审查日期：** 2026-08-11
**审查类型：** 只读最终复审（第三轮）
**审查范围：** `llm_gateway/provider_service.py`、`model_router.py`、`web/routers/models.py`、`web/server.py`、`web/model_route_validator.py`、`utils/atomic_write.py`、`tests/test_provider_onboarding.py` 及相关测试文件
**二轮结论：** Spec PASS / Quality 条件性 FAIL（N1 Important + N2–N4 Minor）
**本轮结论：** **Spec PASS / Quality PASS**

## 结论摘要

二轮审查提出的 N1–N4 全部**真实整改**，且未引入新问题：

- **N1（Important）**：`ProviderService.validate_route` 新增 `_provider_ready(definition)`（`llm_gateway/provider_service.py:203-208`），builtin provider 优先委托 `runtime_router._is_client_configured`（存在时），否则回退 `_custom_clients` 成员判定。`git diff` 证实该方法是本次新增（旧代码为 `if provider_id not in self._runtime_clients(): return "unavailable"`）。委托语义与 `ModelRouter._is_client_configured`（`model_router.py:1206-1219`）逐分支一致：mimo→`self._client is not None`（`model_router.py:560` 仅在配置 `MIMO_API_KEY` 时创建）；agnes→`self._agnes_client is not None or "agnes" in _custom_clients`（覆盖 WebUI 添加 agnes 注册到 `_custom_clients` 的根因场景）。新增测试 `test_builtin_route_accepts_configured_runtime_client`（`tests/test_provider_onboarding.py:884-889`）真正覆盖修复路径（详见「特别核验」）。
- **N2（Minor）**：`web/routers/models.py:96-101` `create_provider` 直接 `validate_url(base_url)`，`is_local_host` 死豁免已删除；全库 grep 确认 models.py 无残留。
- **N3（Minor）**：`_save_key_and_register` 已删除，全库 grep 生产代码零引用（仅审查文档提及）；Ruff 全绿佐证失效 import 清理彻底。
- **N4（Minor）**：`web/server.py:103-119` `_ensure_provider_key_file` 改用 `utils.atomic_write` 原子写，签名匹配（见「特别核验」）；setup/model_discovery 完整迁移作为已记录的验收边界保留，二轮 reviewer 亦判定无实际漏洞，可接受。

验证命令全绿：聚焦回归 `143 passed in 10.97s`、额外兼容 `21 passed in 4.43s`、Ruff `All checks passed!`、compileall exit 0、`git diff --check` exit 0。

---

## 逐项整改核验

### N1（Important）— 内置 provider 路由恒 409 回归 —— ✅ 已修复

**判定：** PASS

**证据（代码，git diff 确认新增）：**
- `llm_gateway/provider_service.py:196-197`：`validate_route` 的 unavailable 判定由 `if provider_id not in self._runtime_clients()` 改为 `if not self._provider_ready(definition)`。
- `llm_gateway/provider_service.py:203-208` `_provider_ready`：
  ```python
  def _provider_ready(self, definition: ProviderDefinition) -> bool:
      if definition.builtin:
          checker = getattr(self.runtime_router, "_is_client_configured", None)
          if checker is not None:
              return bool(checker(definition.id))
      return definition.id in self._runtime_clients()
  ```
  非 builtin 回退路径与原逻辑完全等价（行为无回归）；builtin 无 `_is_client_configured` 时（如测试 fake runtime）静默回退成员判定（防御性设计，可接受）。
- 委托目标语义核验：`model_router.py:1206-1219` `_is_client_configured` 为普通实例方法，`getattr` 取到 bound method 后 `checker(definition.id)` 调用正确。mimo 分支 `self._client is not None`——`model_router.py:557-560` `self._client = AsyncOpenAI(...) if _mimo_key else None`，仅配置 `MIMO_API_KEY` 时创建，语义准确（无 key 的内置 provider 仍判不可用，不会放行空客户端）。agnes 分支同时检查 `_agnes_client` 与 `_custom_clients["agnes"]`，与 `model_router.py:1531-1539`（WebUI 注册 agnes 走 `_custom_clients`）的运行时模型一致。
- 内置集合一致性：`model_router.py:666` `_BUILTIN_PROVIDERS = {"mimo", "agnes"}`；catalog 中内置定义 `builtin=True`（测试 `builtin_catalog()` 于 `tests/test_provider_onboarding.py:101-110`）。

**行为修复验证（逻辑推演）：** 真实应用 `PUT /models/routes/chat` 携带 `provider="mimo"`（系统默认）：配置了 `MIMO_API_KEY` 时 `self._client` 非 None → `validate_route` 返回 None → 放行（修复前恒 409）；未配置时 → "unavailable" → 409（语义正确）。内置默认 provider 的路由保存回归解除。

**证据（测试）：**
- `tests/test_provider_onboarding.py:884-889` `test_builtin_route_accepts_configured_runtime_client`：显式设置 `runtime._client = object()` 与 `runtime._is_client_configured = lambda p: p == "mimo"` 后，断言 `validate_route("mimo", "mimo-v2.5") is None`。
- `tests/test_provider_onboarding.py:878-881` `test_builtin_route_rejects_missing_runtime_client`：fake runtime（无 `_is_client_configured`、无 mimo client）→ 回退 `_custom_clients` → "unavailable"，测试语义仍真实（未配置的内置 provider 不可路由），未被破坏。
- `tests/test_provider_onboarding.py:909-923` `test_builtin_route_unavailable_returns_409`：HTTP 层在 fake runtime 未配置 mimo 的前提下 409 仍成立，与真实「未配置即不可用」语义一致，无需修改。

**RED→GREEN 证据：** `git diff` 显示旧代码正是 `if provider_id not in self._runtime_clients(): return "unavailable"`；新测试在旧代码下 mimo 不在空 `_custom_clients` 必然返回 "unavailable" → 断言 `is None` 失败（RED）；`_provider_ready` 新增后经 `_is_client_configured` 委托放行（GREEN）。实施报告记录 RED 阶段 1 failed、GREEN 后 test_provider_onboarding 43 passed（当前实测 44 项）。逻辑链完整闭合。

---

### N2（Minor）— legacy `POST /models/providers` `is_local_host` 死豁免 —— ✅ 已修复

**判定：** PASS

**证据（代码）：** `web/routers/models.py:96-101` `create_provider` 仅保留 http(s) 前缀检查 + `validate_url(base_url)` 单一校验点，不再有 `is_local_host` 豁免分支。`git grep` 全库：`is_local_host` 在 `web/routers/models.py` 中零引用（仅 `security/ssrf_guard.py:409` 定义、`web/routers/setup.py:531-532` setup 向导 `_test_ollama` 使用——后者属 N4 已记录的验收边界）。服务层 `_definition`（`provider_service.py:318-331`）仍是真正门卫，无安全缺口。

**证据（测试）：** `tests/test_provider_onboarding.py:633-637` `test_non_ollama_rejects_non_loopback_local_hosts`（host.docker.internal/0.0.0.0 拒绝）持续通过，确认服务层校验未被路由层绕过。

---

### N3（Minor）— `_save_key_and_register` 死代码 —— ✅ 已修复

**判定：** PASS

**证据（代码）：** `git grep` 全库 `_save_key_and_register`：生产代码零命中（仅 task-15-review.md / task-15-report.md / task-15-review-v2.md 三份文档提及历史）。`web/routers/models.py:175-194` `set_provider_key` 直接 `await provider_service.update(pid, record, {"api_key": api_key})`。随之失效的 `os`/`contextlib`/`_get_cred_dir`/`_key_file` import 已清理：当前 models.py 顶部 import 为 `json`/`time`/`typing.Any`/`APIRouter` 等（L1-18），Ruff F401 全绿佐证无死 import。

**证据（测试）：** `tests/test_provider_onboarding.py:1075+` `test_legacy_key_update_preserves_state_when_health_check_fails`（422 且旧凭证/旧 runtime client 保持）持续通过。

---

### N4（Minor）— setup/启动凭证写一致性 —— ✅ 部分修复（边界可接受）

**判定：** PASS

**证据（代码）：** `web/server.py:103-119` `_ensure_provider_key_file` 改为 `atomic_write(fp, _encode_key(api_key) + "\n", encoding="utf-8", mode=0o600)`（L116-117），替换原 `Path.write_text` 非原子写。`from contextlib import asynccontextmanager, suppress`（L7）中 `suppress` 仍被 L118 `with suppress(OSError): os.chmod(fp, 0o600)` 使用，无死引用。
- **atomic_write 签名匹配核验：** `utils/atomic_write.py:47-48` `def atomic_write(target_path: str | Path, content: str | bytes, mode: int | None = None, encoding: str = "utf-8") -> None`。调用点位置参数（path, content）+ 关键字参数（encoding, mode）全部匹配；`mode=0o600` 显式传入权限，与 `ProviderCredentialStore.write`（`provider_service.py:60`）的原子写模式一致。`encoding="utf-8"` 下 str content 走 `content.encode(encoding)` 分支，行为正确。
- **验收边界记录：** `setup._auto_register_providers`（`web/routers/setup.py`）与 `model_discovery._ensure_custom_provider` 完整迁移到 `ProviderService` 涉及 setup 同步→异步改造、且会改变「探活失败即拒存」的既有 setup 语义，控制器判定超出本轮验收范围并已在 `task-15-report.md` 显式记录；二轮 reviewer 亦判定这些路径 base_url 来自已校验配置或受信固定端点、无实际漏洞。作为已知边界接受，不计缺陷。

**证据（测试）：** N4 原子写不改变语义（写盘内容与权限一致），无专项测试必要；既有 `test_provider_key_reading.py`（6 项）全绿验证凭证文件读回路径未受影响。

---

## 特别核验项汇总

| 核验项 | 结果 | 证据 |
|---|---|---|
| `_provider_ready` builtin 委托与 `ModelRouter._is_client_configured` 语义一致 | ✅ 一致 | provider_service.py:203-208 ↔ model_router.py:1206-1219；mimo→`_client`（560 行仅在有 key 时创建）、agnes→`_agnes_client or _custom_clients`（覆盖 WebUI 注册场景） |
| `test_builtin_route_accepts_configured_runtime_client` 覆盖修复（RED→GREEN） | ✅ 真实 | git diff 证实旧代码 `provider_id not in _runtime_clients()` 在新测试下必然 RED；新测试显式注入 `_is_client_configured` 委托路径，GREEN 后 143 passed |
| N2/N3 清理无死引用 | ✅ 无残留 | `is_local_host`/`_save_key_and_register` 全库 grep 生产代码零残留；Ruff F401 全绿 |
| N4 atomic_write 签名匹配 | ✅ 匹配 | utils/atomic_write.py:47-48 `(target_path, content, mode=None, encoding="utf-8")`；调用点 `atomic_write(fp, content, encoding=..., mode=0o600)` 参数兼容 |
| `validate_route` 调用点不受影响 | ✅ | models.py:248（PUT route）、model_route_validator.py:46 均受益于统一语义；无其他调用点 |

## 观察项（不作为问题）

- `_provider_ready` 以 `getattr(..., "_is_client_configured", None)` 防御性获取：若未来 `ModelRouter` 重构移除该方法，builtin 将静默回退 `_custom_clients` 判定（回到 N1 修复前行为）。属防御性设计，当前无不安全面，仅提示未来重构时保持该方法。
- `test_builtin_route_accepts_configured_runtime_client` 的 lambda 硬编码 `provider == "mimo"`，`runtime._client = object()` 不参与 lambda 求值——测试模拟的是委托语义而非绑定真实属性，仍真实覆盖 `_provider_ready` 的 builtin 分支与委托调用，可接受。
- `web/server.py` 存量 import 排序（I001）为既有问题，非本任务引入，不计缺陷（任务说明确认）。

## 验证证据

```bash
# 1. 聚焦回归（5 个文件）
.venv/bin/python -m pytest tests/test_provider_onboarding.py tests/test_provider_key_reading.py \
  tests/test_model_switching_refactor.py tests/test_provider_catalog.py tests/test_provider_transports.py -q
# ===== 143 passed in 10.97s =====  （exit 0；较二轮 142 多 1：新增 test_builtin_route_accepts_configured_runtime_client）

# 2. 额外 provider 兼容（补充证据）
.venv/bin/python -m pytest tests/test_custom_anthropic_provider.py tests/test_frontend_provider_contracts.py \
  tests/test_degraded_mode_any_provider.py tests/test_agnes_provider_routing_bug.py -q
# ===== 21 passed in 4.43s =====  （exit 0）

# 3. Ruff（含 web/routers/models.py）
.venv/bin/python -m ruff check security/ssrf_guard.py llm_gateway/ llm_gateway/transports/ \
  web/routers/providers.py web/routers/models.py web/custom_providers.py \
  tests/test_provider_transports.py tests/test_provider_onboarding.py
# All checks passed!  （exit 0）

# 4. compileall
.venv/bin/python -m compileall -q security/ llm_gateway/ web/   # exit 0

# 5. git diff --check（全局）
git diff --check   # exit 0

# 6. git diff 确认 _provider_ready 为本次新增（RED→GREEN 佐证）
git diff -U5 -- llm_gateway/provider_service.py
# -        if provider_id not in self._runtime_clients():
# +        if not self._provider_ready(definition):
# +    def _provider_ready(...)  # 新增 builtin 委托分支
```

## 最终判定

- **Spec：PASS。** Task 15 简报要求全部满足：`ProviderService` 原子 CRUD 与回滚、统一路由校验、SSRF 边界、凭证旁路收敛、route/chat_model 原子持久化均有实现与回归测试；N1–N4 整改真实落地且语义正确。
- **Quality：PASS。** 二轮唯一 Important 回归（N1）已修复：`_provider_ready` 的 builtin 委托与 `ModelRouter._is_client_configured` 逐分支一致，RED→GREEN 有 git diff 与测试逻辑双重佐证；N2/N3 死代码清理无残留；N4 原子写签名匹配；剩余 setup/model_discovery 迁移为已明确记录的验收边界（无实际漏洞）。全部验证命令通过，未发现新增缺陷。**可合并。**
