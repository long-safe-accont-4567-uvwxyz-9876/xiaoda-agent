"""TDD 测试：模型切换持久化与审计日志修复。

覆盖两个回归 bug：
- Bug #1: POST /agents/{name}/model 返回 HTTP 500，因为 _audit 使用不存在的
  core.db.transaction() API，AttributeError 未被捕获
- Bug #2-A: model_router.set_chat_model 只持久化 models.chat_model，
  未同步 models.routes.chat，导致 _apply_route_overrides 与 _restore_chat_model
  启动顺序覆盖用户选择
- Bug #2-B: _restore_chat_model 未捕获 LLMError，且 fallback 调用
  set_chat_model("mimo", ...) 会重新持久化 mimo，形成 sticky fallback

Issue: 更变子 Agent 的模型返回 HTTP 500；主 Agent 的模型每次重启都会覆盖成 Mimo
"""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────
# Bug #1: agents.py _audit 不应使用不存在的 db.transaction()
# ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agents_audit_does_not_use_transaction_method():
    """_audit 不应调用 db.transaction()，且任何异常都不应向上传播。

    回归测试：DatabaseManager 没有 transaction() 方法，旧实现抛 AttributeError
    导致 HTTP 500。修复后 _audit 直接调用 insert_audit_log + commit，且
    except Exception 兜底所有异常。
    """
    from web.routers.agents import _audit

    # 构造 mock：db 故意没有 transaction 属性，模拟真实 DatabaseManager
    db = MagicMock()
    # 显式删除 transaction 属性，确保 _audit 不依赖它
    if hasattr(db, "transaction"):
        del db.transaction
    db.insert_audit_log = AsyncMock()
    db.commit = AsyncMock()

    request = MagicMock()
    request.app.state.core = MagicMock(db=db)

    # 调用不应抛任何异常
    await _audit(request, "model", "xiaoli -> agnes/agnes-2.0-flash")

    # 应该直接调用 insert_audit_log 和 commit，不调用 transaction
    db.insert_audit_log.assert_awaited_once()
    db.commit.assert_awaited_once()
    assert not hasattr(db, "transaction") or not getattr(db, "transaction", None)


@pytest.mark.asyncio
async def test_agents_audit_swallows_db_errors():
    """_audit 应吞下 insert_audit_log 或 commit 抛出的任何异常。

    审计日志失败不应影响业务流程（不应导致 HTTP 500）。
    """
    from web.routers.agents import _audit

    db = MagicMock()
    db.insert_audit_log = AsyncMock(side_effect=RuntimeError("db locked"))
    db.commit = AsyncMock()

    request = MagicMock()
    request.app.state.core = MagicMock(db=db)

    # 不应抛异常
    await _audit(request, "model", "test action")


@pytest.mark.asyncio
async def test_agents_audit_swallows_attribute_error():
    """_audit 应吞下 AttributeError（旧 bug 的根因）。

    回归：旧实现 `async with core.db.transaction()` 在 DatabaseManager
    没有 transaction 方法时抛 AttributeError，未被 except 捕获，导致 HTTP 500。
    """
    from web.routers.agents import _audit

    db = MagicMock()
    # insert_audit_log 抛 AttributeError 模拟旧 bug
    db.insert_audit_log = AsyncMock(side_effect=AttributeError("'DatabaseManager' object has no attribute 'transaction'"))
    db.commit = AsyncMock()

    request = MagicMock()
    request.app.state.core = MagicMock(db=db)

    # 不应抛 AttributeError
    await _audit(request, "model", "test action")


# ─────────────────────────────────────────────────────────────
# Bug #2-A: set_chat_model 必须同步持久化 models.routes.chat
# ─────────────────────────────────────────────────────────────


def test_set_chat_model_persists_both_chat_model_and_routes_chat():
    """set_chat_model 应同步写入 models.chat_model 和 models.routes.chat。

    回归测试：旧实现只写 models.chat_model，导致两套数据不同步。
    启动时 _apply_route_overrides 先应用 models.routes.chat，
    然后 _restore_chat_model 读取 models.chat_model 覆盖 ROUTE_TABLE，
    造成用户切换的 provider 被旧 models.routes.chat 反向覆盖。
    """
    from model_router import ROUTE_TABLE, ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._custom_clients = set()
    router._current_chat_model = None
    router.TASK_TIMEOUTS = {"chat": 60}
    # 初始化 _registry（新 set_chat_model 依赖它）
    from model_router import ModelRouteRegistry
    router._registry = ModelRouteRegistry(ROUTE_TABLE)
    # 模拟 agnes 已注册（避免 _lazy_register_provider 被调用）
    router._custom_clients.add("agnes")
    router._lazy_register_provider = MagicMock()

    # 快照整个 ROUTE_TABLE（set_chat_model 会改写所有 4 个 sync_tasks：
    # chat/emotion_analysis/tool_result_wrap/memory_encoding）
    _orig_table = copy.deepcopy(ROUTE_TABLE)
    try:
        # mock config_service 捕获 set 调用
        captured: dict[str, object] = {}

        class FakeCfg:
            def set(self, path, value):
                captured[path] = value

            def set_many(self, updates):
                for path, value in updates.items():
                    captured[path] = value

            def get(self, path, default=None):
                return captured.get(path, default)

        with patch("web.config_service.get_config_service", return_value=FakeCfg()):
            result = router.set_chat_model("agnes", "agnes-2.0-flash")

        # 必须同时写入两个路径
        assert "models.chat_model" in captured, "必须持久化 models.chat_model"
        assert "models.routes.chat" in captured, "必须同步持久化 models.routes.chat"

        # models.chat_model 字段
        chat_model = captured["models.chat_model"]
        assert chat_model["provider"] == "agnes"
        assert chat_model["model_id"] == "agnes-2.0-flash"

        # models.routes.chat 字段（与 update_route 保持一致）
        routes_chat = captured["models.routes.chat"]
        assert routes_chat["model"] == "agnes-2.0-flash"
        assert routes_chat["client"] == "agnes"
        assert "max_tokens" in routes_chat
        assert "thinking" in routes_chat
        assert "timeout" in routes_chat
        assert isinstance(routes_chat["thinking"], bool)

        assert result == {"provider": "agnes", "model_id": "agnes-2.0-flash"}
    finally:
        # 整体还原 ROUTE_TABLE（finally 只还原 chat 会残留其他 sync_tasks 被改值）
        ROUTE_TABLE.clear()
        ROUTE_TABLE.update(_orig_table)


def test_set_chat_model_routes_chat_thinking_field_is_bool():
    """models.routes.chat.thinking 必须是 bool 而非 dict。

    与 web/routers/models.py update_route 的持久化格式保持一致，
    否则 _apply_route_overrides 读取时会类型不匹配。
    """
    from model_router import ROUTE_TABLE, ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router._custom_clients = {"agnes"}
    router._current_chat_model = None
    router.TASK_TIMEOUTS = {"chat": 60}
    router._lazy_register_provider = MagicMock()
    # 初始化 _registry（新 set_chat_model 依赖它）
    from model_router import ModelRouteRegistry
    router._registry = ModelRouteRegistry(ROUTE_TABLE)

    _orig_table = copy.deepcopy(ROUTE_TABLE)
    try:
        captured: dict[str, object] = {}

        class FakeCfg:
            def set(self, path, value):
                captured[path] = value

            def set_many(self, updates):
                for path, value in updates.items():
                    captured[path] = value

        with patch("web.config_service.get_config_service", return_value=FakeCfg()):
            router.set_chat_model("agnes", "agnes-2.0-flash")

        routes_chat = captured["models.routes.chat"]
        assert isinstance(routes_chat["thinking"], bool)
        # agnes 切换时 thinking 被禁用
        assert routes_chat["thinking"] is False
    finally:
        # 整体还原 ROUTE_TABLE（set_chat_model 会改写所有 sync_tasks）
        ROUTE_TABLE.clear()
        ROUTE_TABLE.update(_orig_table)


# ─────────────────────────────────────────────────────────────
# Bug #2-B: _restore_chat_model 必须捕获 LLMError 且不重新持久化
# ─────────────────────────────────────────────────────────────


def test_restore_chat_model_catches_llm_error():
    """_restore_chat_model 必须捕获 LLMError（继承 AppException 而非 OSError）。

    回归测试：旧实现 except (KeyError, ValueError, AttributeError, OSError)
    不捕获 LLMError。自定义 provider 注册失败时 set_chat_model 抛 LLMError，
    导致启动崩溃或异常向上传播。

    注意：新实现不再调用 set_chat_model，此测试保留为回归守护，并 patch
    ROUTE_TABLE 防止污染全局状态（测试隔离）。
    """
    from core.app_exception import LLMError
    from web.server import _restore_chat_model

    cfg = MagicMock()
    cfg.get.return_value = {"provider": "custom-unknown", "model_id": "custom-model"}

    core = MagicMock()
    # set_chat_model 抛 LLMError（自定义 provider 未注册）——新实现不会调用它
    core.router.set_chat_model = MagicMock(side_effect=LLMError("provider 未注册"))
    core.router._current_chat_model = None
    # 显式设为空 dict，避免 MagicMock 的 _custom_clients.get() 返回真值
    # 导致走 success 路径污染真实 ROUTE_TABLE
    core.router._custom_clients = {}

    # patch ROUTE_TABLE 防止污染全局状态
    with patch("model_router.ROUTE_TABLE", {"chat": {"model": "old", "client": "old"}}):
        with patch("model_router.MIMO_MODEL", "mimo-v2.5"):
            # 不应抛 LLMError
            _restore_chat_model(cfg, core)

    # 应该走 fallback 路径
    cfg.get.assert_called_with("models.chat_model")


def test_restore_chat_model_fallback_does_not_persist_mimo():
    """fallback 时不应调用 set_chat_model，避免重新持久化 mimo 覆盖用户选择。

    回归测试：旧实现 fallback 调用 core.router.set_chat_model("mimo", MIMO_MODEL)
    会触发 set_chat_model 内部的持久化逻辑，把 models.chat_model 覆盖成 mimo，
    形成 sticky fallback —— 用户原选择永远无法恢复。
    修复后 _restore_chat_model 完全不调用 set_chat_model，只直接修改
    ROUTE_TABLE["chat"] 和 _current_chat_model，不触发持久化。

    注意：fallback 模型不再硬编码为 "mimo-v2.5"，而是从
    config.get_default_model_for_provider(DEFAULT_PROVIDER) 动态读取
    （用户约束：默认用 MiMo，但模型 ID 不在代码里硬编码）。
    """
    from web.server import _restore_chat_model

    cfg = MagicMock()
    cfg.get.return_value = {"provider": "custom-unknown", "model_id": "custom-model"}

    core = MagicMock()
    # _restore_chat_model 不再调用 set_chat_model，所以不需要 side_effect
    core.router.set_chat_model = MagicMock()
    core.router._current_chat_model = None
    core.router._custom_clients = {}  # provider 不可用

    # 动态读取预期 fallback 值（不再硬编码 "mimo-v2.5"）
    from config import DEFAULT_PROVIDER, get_default_model_for_provider
    expected_provider = DEFAULT_PROVIDER
    expected_model = get_default_model_for_provider(DEFAULT_PROVIDER)
    assert expected_model, "测试前置失败：provider_metadata.json 必须配置 mimo 的 default_model"

    with patch("model_router.ROUTE_TABLE", {"chat": {"model": "old", "client": "old"}}):
        _restore_chat_model(cfg, core)

    # set_chat_model 不应被调用（新实现直接修改 ROUTE_TABLE，不走 set_chat_model）
    core.router.set_chat_model.assert_not_called()

    # 应该直接修改内存中的 _current_chat_model（fallback 到动态默认模型）
    assert core.router._current_chat_model == {
        "provider": expected_provider, "model_id": expected_model,
    }


def test_restore_chat_model_success_path():
    """正常路径：provider 可用时直接修改 ROUTE_TABLE，不调用 set_chat_model。

    新实现：_restore_chat_model 不再调用 set_chat_model（会全局同步覆盖其他路由），
    而是直接修改 ROUTE_TABLE["chat"] 和 _current_chat_model。
    """
    from web.server import _restore_chat_model

    cfg = MagicMock()
    cfg.get.return_value = {"provider": "agnes", "model_id": "agnes-2.0-flash"}

    core = MagicMock()
    core.router.set_chat_model = MagicMock()
    core.router._current_chat_model = None
    core.router._custom_clients = {"agnes": "client"}  # agnes 已注册
    core.router._agnes_client = MagicMock()  # agnes 内置 transport

    # 捕获 patched dict 引用，断言在 patch 块内进行（测试隔离）
    patched_rt = {"chat": {"model": "old", "client": "old"}}
    with patch("model_router.ROUTE_TABLE", patched_rt):
        _restore_chat_model(cfg, core)

        # set_chat_model 不应被调用（新实现直接修改 ROUTE_TABLE）
        core.router.set_chat_model.assert_not_called()

        # 应该直接修改 patched ROUTE_TABLE 和 _current_chat_model
        assert patched_rt["chat"]["model"] == "agnes-2.0-flash"
        assert patched_rt["chat"]["client"] == "agnes"
        assert core.router._current_chat_model == {"provider": "agnes", "model_id": "agnes-2.0-flash"}


def test_restore_chat_model_no_saved_preference_returns_early():
    """无保存的偏好时应直接返回，不调用 set_chat_model。"""
    from web.server import _restore_chat_model

    cfg = MagicMock()
    cfg.get.return_value = None  # 无保存的偏好

    core = MagicMock()
    core.router.set_chat_model = MagicMock()

    _restore_chat_model(cfg, core)

    core.router.set_chat_model.assert_not_called()


# ─────────────────────────────────────────────────────────────
# Bug #3: cfg.get("models.*") 返回引用导致 _data 被直接变异污染
# ─────────────────────────────────────────────────────────────


def test_config_service_get_models_returns_deep_copy():
    """cfg.get("models.*") 必须返回深拷贝，防止调用方通过引用变异 _data。

    根因修复：Python 陷阱 — dict 的 get/[] 返回内部对象的引用。
    旧实现 cfg.get("models.routes") 返回 _data["models"]["routes"] 的引用，
    调用方直接修改返回值会污染 _data 而不触发 set()/_save()。
    随后非 models 路径的 set() 触发 _save() 将污染的 _data 持久化，
    导致用户设置的 agnes 被神秘覆盖为 mimo。

    修复：get() 对 models. 路径返回 json 深拷贝，切断引用链。
    """
    import json
    import os
    import tempfile
    from pathlib import Path

    from web.config_service import ConfigService

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump({
            "models": {
                "chat_model": {"provider": "agnes", "model_id": "agnes-2.0-flash"},
                "routes": {"chat": {"model": "agnes-2.0-flash", "client": "agnes"}},
            }
        }, f)
        tmp_path = f.name

    try:
        cfg = ConfigService(path=Path(tmp_path))

        # 获取引用并尝试变异
        chat_model = cfg.get("models.chat_model")
        chat_model["provider"] = "mimo"
        chat_model["model_id"] = "mimo-v2.5"

        routes = cfg.get("models.routes")
        routes["chat"]["client"] = "mimo"
        routes["chat"]["model"] = "mimo-v2.5"

        # 再次获取，应该还是 agnes（深拷贝防护）
        chat_model_2 = cfg.get("models.chat_model")
        assert chat_model_2["provider"] == "agnes", (
            "深拷贝修复失败: 通过引用变异污染了 _data"
        )
        assert chat_model_2["model_id"] == "agnes-2.0-flash"

        routes_2 = cfg.get("models.routes")
        assert routes_2["chat"]["client"] == "agnes", (
            "深拷贝修复失败: routes.chat 被引用变异污染"
        )
        assert routes_2["chat"]["model"] == "agnes-2.0-flash"
    finally:
        os.unlink(tmp_path)


def test_config_service_get_non_models_returns_reference():
    """非 models 路径不受深拷贝影响，保持原有行为（性能考虑）。"""
    import os
    import tempfile
    from pathlib import Path

    from web.config_service import ConfigService

    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        import json
        json.dump({"mail": {"enabled": True, "mode": "off"}}, f)
        tmp_path = f.name

    try:
        cfg = ConfigService(path=Path(tmp_path))
        assert cfg.get("mail.enabled") == True
        assert cfg.get("mail.mode") == "off"
    finally:
        os.unlink(tmp_path)


def test_tracked_dict_update_does_not_mutate_caller():
    """_TrackedDict.update() 不应原地修改调用方传入的 dict。

    回归测试：旧实现 update() 中 `args[0][k] = _wrap_tracked(...)` 会修改调用方的
    原始 dict，导致引用共享问题。修复后通过 __setitem__ 逐项写入，不影响调用方。
    """
    from web.config_service import _TrackedDict

    td = _TrackedDict(_track_path="root")
    caller_dict = {"key": {"nested": 1}, "plain": "value"}

    td.update(caller_dict)

    # 调用方的 dict 不应被修改（值不应被替换为 _TrackedDict）
    assert type(caller_dict["key"]) is dict, (
        "update() 不应原地修改调用方 dict 的值类型"
    )
    assert caller_dict["key"] == {"nested": 1}
    assert caller_dict["plain"] == "value"

    # 但 _TrackedDict 内部应该正确包装
    assert isinstance(td["key"], _TrackedDict)
    assert td["plain"] == "value"
