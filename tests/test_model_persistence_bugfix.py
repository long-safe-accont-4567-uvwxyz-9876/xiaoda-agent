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
    # 模拟 agnes 已注册（避免 _lazy_register_provider 被调用）
    router._custom_clients.add("agnes")
    router._lazy_register_provider = MagicMock()

    # 保存原始 ROUTE_TABLE["chat"] 状态
    original_chat = dict(ROUTE_TABLE["chat"])
    try:
        # mock config_service 捕获 set 调用
        captured: dict[str, object] = {}

        class FakeCfg:
            def set(self, path, value):
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
        # 恢复 ROUTE_TABLE
        ROUTE_TABLE["chat"] = original_chat


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

    original_chat = dict(ROUTE_TABLE["chat"])
    try:
        captured: dict[str, object] = {}

        class FakeCfg:
            def set(self, path, value):
                captured[path] = value

        with patch("web.config_service.get_config_service", return_value=FakeCfg()):
            router.set_chat_model("agnes", "agnes-2.0-flash")

        routes_chat = captured["models.routes.chat"]
        assert isinstance(routes_chat["thinking"], bool)
        # agnes 切换时 thinking 被禁用
        assert routes_chat["thinking"] is False
    finally:
        ROUTE_TABLE["chat"] = original_chat


# ─────────────────────────────────────────────────────────────
# Bug #2-B: _restore_chat_model 必须捕获 LLMError 且不重新持久化
# ─────────────────────────────────────────────────────────────


def test_restore_chat_model_catches_llm_error():
    """_restore_chat_model 必须捕获 LLMError（继承 AppException 而非 OSError）。

    回归测试：旧实现 except (KeyError, ValueError, AttributeError, OSError)
    不捕获 LLMError。自定义 provider 注册失败时 set_chat_model 抛 LLMError，
    导致启动崩溃或异常向上传播。
    """
    from core.app_exception import LLMError
    from web.server import _restore_chat_model

    cfg = MagicMock()
    cfg.get.return_value = {"provider": "custom-unknown", "model_id": "custom-model"}

    core = MagicMock()
    # set_chat_model 抛 LLMError（自定义 provider 未注册）
    core.router.set_chat_model = MagicMock(side_effect=LLMError("provider 未注册"))
    core.router._current_chat_model = None

    # 不应抛 LLMError
    _restore_chat_model(cfg, core)

    # 应该走 fallback 路径
    cfg.get.assert_called_with("models.chat_model")


def test_restore_chat_model_fallback_does_not_persist_mimo():
    """fallback 时不应调用 set_chat_model，避免重新持久化 mimo 覆盖用户选择。

    回归测试：旧实现 fallback 调用 core.router.set_chat_model("mimo", MIMO_MODEL)
    会触发 set_chat_model 内部的持久化逻辑，把 models.chat_model 覆盖成 mimo，
    形成 sticky fallback —— 用户原选择永远无法恢复。
    修复后 fallback 只修改内存中的 ROUTE_TABLE，不触发持久化。
    """
    from core.app_exception import LLMError
    from web.server import _restore_chat_model

    cfg = MagicMock()
    cfg.get.return_value = {"provider": "custom-unknown", "model_id": "custom-model"}

    core = MagicMock()
    # set_chat_model 第一次抛 LLMError（恢复用户选择失败）
    core.router.set_chat_model = MagicMock(side_effect=LLMError("provider 未注册"))
    core.router._current_chat_model = None

    # 捕获 fallback 后是否重新持久化
    set_chat_model_call_count = {"count": 0}
    original_side_effect = core.router.set_chat_model.side_effect

    def counted_side_effect(*args, **kwargs):
        set_chat_model_call_count["count"] += 1
        if original_side_effect:
            raise original_side_effect

    core.router.set_chat_model.side_effect = counted_side_effect

    with patch("model_router.ROUTE_TABLE", {"chat": {"model": "old", "client": "old"}}):
        with patch("model_router.MIMO_MODEL", "mimo-v2.5"):
            _restore_chat_model(cfg, core)

    # set_chat_model 应只被调用一次（恢复尝试），fallback 不应再调用
    assert set_chat_model_call_count["count"] == 1, (
        "fallback 不应再调用 set_chat_model，否则会重新持久化 mimo 覆盖用户选择"
    )

    # 应该直接修改内存中的 ROUTE_TABLE["chat"]
    # 通过 _current_chat_model 验证 fallback 生效
    assert core.router._current_chat_model == {"provider": "mimo", "model_id": "mimo-v2.5"}


def test_restore_chat_model_success_path():
    """正常路径：set_chat_model 成功时不应触发 fallback。"""
    from web.server import _restore_chat_model

    cfg = MagicMock()
    cfg.get.return_value = {"provider": "agnes", "model_id": "agnes-2.0-flash"}

    core = MagicMock()
    core.router.set_chat_model = MagicMock(return_value={"provider": "agnes", "model_id": "agnes-2.0-flash"})

    _restore_chat_model(cfg, core)

    core.router.set_chat_model.assert_called_once_with("agnes", "agnes-2.0-flash")


def test_restore_chat_model_no_saved_preference_returns_early():
    """无保存的偏好时应直接返回，不调用 set_chat_model。"""
    from web.server import _restore_chat_model

    cfg = MagicMock()
    cfg.get.return_value = None  # 无保存的偏好

    core = MagicMock()
    core.router.set_chat_model = MagicMock()

    _restore_chat_model(cfg, core)

    core.router.set_chat_model.assert_not_called()
