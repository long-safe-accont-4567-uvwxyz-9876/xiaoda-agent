"""模型切换逻辑彻底重构回归测试。

覆盖核心约束：
1. 用户改过模型后不被任何路径覆盖
2. 默认值从 provider_metadata.json 读
3. set_chat_model 失败时回滚
4. 降级链不污染全局 ROUTE_TABLE
5. 死路由启动时清理
6. _restore_chat_model 失败不覆盖持久化
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def fresh_registry():
    """每个测试用独立的 Registry 实例，避免相互污染。"""
    from model_router import ModelRouteRegistry
    # 用最小默认表初始化
    default_table = {
        "chat": {"model": "mimo-v2.5", "max_tokens": 131072, "client": "mimo",
                 "thinking": {"type": "disabled"}},
        "chat_pro": {"model": "mimo-v2.5", "max_tokens": 131072, "client": "mimo",
                     "thinking": {"type": "enabled", "budget_tokens": 4096}},
        "chat_flash": {"model": "mimo-v2.5", "max_tokens": 6144, "client": "mimo",
                       "thinking": {"type": "disabled"}},
        "chat_agnes": {"model": "agnes-2.0-flash", "max_tokens": 131072, "client": "agnes",
                       "thinking": {"type": "disabled"}},
    }
    # mock ConfigService 避免真实落盘
    mock_cfg = MagicMock()
    mock_cfg.set = MagicMock()
    return ModelRouteRegistry(default_table, config_service=mock_cfg), mock_cfg


def test_registry_get_task_returns_deep_copy(fresh_registry):
    """get_task 必须返回深拷贝，调用方修改不影响内部状态。"""
    reg, _ = fresh_registry
    task = reg.get_task("chat")
    assert task is not None
    task["model"] = "POLLUTED"
    task["thinking"]["type"] = "POLLUTED"
    # 内部状态不变
    again = reg.get_task("chat")
    assert again["model"] == "mimo-v2.5"
    assert again["thinking"]["type"] == "disabled"


def test_registry_update_route_atomic_success(fresh_registry):
    """update_route 成功时同时更新内存和持久化。"""
    reg, mock_cfg = fresh_registry
    result = reg.update_route("chat", model_id="agnes-2.0-flash", provider="agnes")
    assert result["model"] == "agnes-2.0-flash"
    assert result["client"] == "agnes"
    # 内存已更新
    assert reg.get_task("chat")["model"] == "agnes-2.0-flash"
    # 持久化被调用
    assert mock_cfg.set.called
    call_args = mock_cfg.set.call_args_list
    # 至少持久化了 chat 路由和 chat_model
    paths_persisted = [c.args[0] for c in call_args]
    assert "models.routes.chat" in paths_persisted


def test_registry_update_route_rollback_on_persist_failure(fresh_registry):
    """持久化失败时内存必须回滚到旧值。"""
    reg, mock_cfg = fresh_registry
    # 第一次 set 抛异常
    mock_cfg.set.side_effect = RuntimeError("disk full")
    original_model = reg.get_task("chat")["model"]
    with pytest.raises(RuntimeError):
        reg.update_route("chat", model_id="agnes-2.0-flash", provider="agnes")
    # 内存回滚
    assert reg.get_task("chat")["model"] == original_model


def test_registry_snapshot_task_independent_of_get_task(fresh_registry):
    """snapshot_task 与 get_task 返回独立的拷贝。"""
    reg, _ = fresh_registry
    s1 = reg.snapshot_task("chat")
    s2 = reg.snapshot_task("chat")
    assert s1 is not s2
    s1["model"] = "X"
    assert s2["model"] == "mimo-v2.5"


def test_registry_all_tasks_returns_list(fresh_registry):
    """all_tasks 返回 task 名称列表。"""
    reg, _ = fresh_registry
    tasks = reg.all_tasks()
    assert isinstance(tasks, list)
    assert "chat" in tasks
    assert "chat_pro" in tasks


def test_registry_replace_table_bulk_update(fresh_registry):
    """replace_table 一次性替换整个表（启动时用）。"""
    reg, mock_cfg = fresh_registry
    new_table = {
        "chat": {"model": "agnes-2.0-flash", "max_tokens": 8192, "client": "agnes",
                 "thinking": {"type": "disabled"}},
        "chat_pro": {"model": "agnes-2.0-flash", "max_tokens": 8192, "client": "agnes",
                     "thinking": {"type": "disabled"}},
    }
    reg.replace_table(new_table)
    assert reg.get_task("chat")["model"] == "agnes-2.0-flash"
    assert reg.get_task("chat_pro")["client"] == "agnes"
    # replace_table 不触发持久化（启动时用，持久化由调用方负责）
    assert not mock_cfg.set.called


# ── Task 2: get_default_model_for_provider ──

def test_get_default_model_for_provider_from_metadata(monkeypatch):
    """从 provider_metadata.json 读默认模型 ID。"""
    # 清掉可能由 CI/本地环境 export 的环境变量，避免 env 优先级覆盖 metadata 默认值
    monkeypatch.delenv("MIMO_MODEL_NAME", raising=False)
    monkeypatch.delenv("AGNES_TEXT_MODEL", raising=False)
    monkeypatch.delenv("DEEPSEEK_MODEL_NAME", raising=False)
    import config
    config._PROVIDER_METADATA_CACHE = None
    from config import get_default_model_for_provider
    assert get_default_model_for_provider("mimo") == "mimo-v2.5"
    assert get_default_model_for_provider("agnes") == "agnes-2.0-flash"
    assert get_default_model_for_provider("deepseek") == "deepseek-chat"


def test_get_default_model_for_provider_env_override(monkeypatch):
    """环境变量优先级最高。"""
    monkeypatch.setenv("MIMO_MODEL_NAME", "mimo-custom-v9")
    # 清除缓存
    import config
    config._PROVIDER_METADATA_CACHE = None
    try:
        assert config.get_default_model_for_provider("mimo") == "mimo-custom-v9"
    finally:
        # 清理：monkeypatch 会自动还原 env，但全局缓存不会自动还原，
        # 放进 try/finally 避免 assert 失败时缓存不还原污染后续测试
        monkeypatch.delenv("MIMO_MODEL_NAME", raising=False)
        config._PROVIDER_METADATA_CACHE = None


def test_get_default_model_for_provider_unknown_returns_empty():
    """未知 provider 返回空串（不抛异常）。"""
    from config import get_default_model_for_provider
    assert get_default_model_for_provider("unknown_provider_xxx") == ""


# ── Task 3: 删除 _save() 反向同步死代码 ──

def test_config_service_no_mark_startup_complete_method():
    """ConfigService 不再有 mark_startup_complete 方法（已删除）。"""
    from web.config_service import ConfigService
    assert not hasattr(ConfigService, "mark_startup_complete"), \
        "mark_startup_complete 应该已删除（死代码）"


def test_config_service_no_startup_complete_field():
    """ConfigService 实例不再有 _startup_complete 字段。"""
    from web.config_service import ConfigService
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        svc = ConfigService(path=tmp_path)
        assert not hasattr(svc, "_startup_complete"), \
            "_startup_complete 字段应该已删除"
    finally:
        tmp_path.unlink(missing_ok=True)


def test_config_service_save_does_not_touch_route_table():
    """_save() 不得反向从 ROUTE_TABLE 恢复 _data（已删除该逻辑）。"""
    import inspect
    from web.config_service import ConfigService
    source = inspect.getsource(ConfigService._save)
    # 不应再实际导入或调用 ROUTE_TABLE（注释中提及不算）
    # 移除注释行后检查
    import re
    code_only = re.sub(r'#.*', '', source)
    assert "from model_router import ROUTE_TABLE" not in code_only, \
        "_save() 不应再导入 ROUTE_TABLE（反向同步死代码已删除）"
    assert "ROUTE_TABLE.get" not in code_only, \
        "_save() 不应再调用 ROUTE_TABLE.get（反向同步死代码已删除）"
    assert "ROUTE_TABLE.items" not in code_only, \
        "_save() 不应再遍历 ROUTE_TABLE（反向同步死代码已删除）"
    assert "restoring _data from ROUTE_TABLE" not in source


# ── Task 4: set_chat_model 原子化 ──

@pytest.fixture
def mock_config_service(monkeypatch):
    """Mock ConfigService 单例，避免测试污染生产配置文件。"""
    mock_cfg = MagicMock()
    mock_cfg.set = MagicMock()
    # patch get_config_service 返回 mock
    import web.config_service
    monkeypatch.setattr(web.config_service, "get_config_service", lambda: mock_cfg)
    # 同时 patch ModelRouteRegistry._get_cfg 已注入的实例
    return mock_cfg


def test_set_chat_model_rolls_back_on_provider_not_registered(mock_config_service):
    """set_chat_model 在 provider 未注册时回滚 ROUTE_TABLE。"""
    from model_router import ModelRouter, ROUTE_TABLE
    try:
        router = ModelRouter(api_key="fake")
    except (ImportError, OSError, ValueError, RuntimeError):
        pytest.skip("ModelRouter 在测试环境无法初始化")
    # 注入 mock registry 避免真实持久化
    from model_router import ModelRouteRegistry
    router._registry = ModelRouteRegistry(ROUTE_TABLE, config_service=mock_config_service)

    original_model = ROUTE_TABLE["chat"]["model"]
    original_client = ROUTE_TABLE["chat"]["client"]

    # 尝试切换到未注册的自定义 provider
    from core.app_exception import LLMError
    with pytest.raises(LLMError):
        router.set_chat_model("unknown_provider_xyz", "some-model")

    # ROUTE_TABLE 未被污染（新实现先验证再改状态）
    assert ROUTE_TABLE["chat"]["model"] == original_model
    assert ROUTE_TABLE["chat"]["client"] == original_client


def test_set_chat_model_persists_all_synced_tasks(mock_config_service):
    """set_chat_model 成功时通过 Registry 更新所有同步 task。"""
    from model_router import ModelRouter
    try:
        router = ModelRouter(api_key="fake")
    except (ImportError, OSError, ValueError, RuntimeError):
        pytest.skip("ModelRouter 在测试环境无法初始化")
    # mock registry 追踪调用
    router._registry = MagicMock()
    router._registry.update_route = MagicMock(return_value={"model": "x", "client": "y"})
    # chat_pro/chat_flash 已合并进 chat，sync_tasks 只剩 4 个 task
    _sync_tasks = ("chat", "emotion_analysis", "tool_result_wrap", "memory_encoding")
    router._registry.all_tasks = MagicMock(return_value=list(_sync_tasks))
    router._registry.get_task = MagicMock(return_value={
        "max_tokens": 8192, "thinking": {"type": "disabled"},
    })

    router.set_chat_model("mimo", "mimo-v2.5")

    # sync_tasks 只含 ("chat", "emotion_analysis", "tool_result_wrap", "memory_encoding")，
    # chat_pro/chat_flash 已合并进 chat，不再单独同步
    tasks_updated = [c.args[0] for c in router._registry.update_route.call_args_list]
    assert set(tasks_updated) == set(_sync_tasks), (
        f"应只同步 {_sync_tasks}，实际同步了 {tasks_updated}"
    )
    assert "chat" in tasks_updated
    assert "chat_pro" not in tasks_updated
    assert "chat_flash" not in tasks_updated


# ── Task 5: _restore_chat_model 不硬编码 fallback ──

def test_restore_chat_model_does_not_overwrite_persistence_on_failure(tmp_path, monkeypatch):
    """_restore_chat_model 失败时不覆盖 ConfigService 持久化值。

    CR-7 修复：原测试用 agnes（内置 provider），_restore_chat_model 的
    `if provider not in builtin and provider not in _custom_clients` 对 agnes
    不抛错（agnes 在 builtin 集合），走 success 路径，根本不进 fallback 分支。
    断言"持久化仍是 agnes"是 trivially true（两个分支都不写文件）。
    修复：改用未注册的自定义 provider "custom_unregistered_x"，真正触发 fallback，
    断言持久化值未被覆盖 + 内存回退到 DEFAULT_PROVIDER。
    """
    import json as _json
    # 准备一个持久化文件，用户已选未注册的自定义 provider
    overrides_file = tmp_path / "webui_overrides.json"
    overrides_file.write_text(_json.dumps({
        "models": {
            "chat_model": {"provider": "custom_unregistered_x", "model_id": "custom-model-x"},
            "routes": {"chat": {"model": "custom-model-x", "client": "custom_unregistered_x",
                                "max_tokens": 8192, "thinking": False, "timeout": 60}},
        }
    }), encoding="utf-8")

    from web.config_service import ConfigService
    cfg = ConfigService(path=overrides_file)

    # mock core：custom provider 未注册（不在 _custom_clients，触发 fallback）
    mock_core = MagicMock()
    mock_core.router._custom_clients = {}  # custom_unregistered_x 不在已注册列表
    mock_core.router._current_chat_model = None
    # _restore_chat_model 会用 ModelRouteRegistry 包装 ROUTE_TABLE（MagicMock 的 _registry 不是真实例）
    # 这会操作真实 ROUTE_TABLE，需要 try/finally 还原

    # mock ConfigService 单例
    import web.config_service
    monkeypatch.setattr(web.config_service, "get_config_service", lambda: cfg)

    # 快照 ROUTE_TABLE 用于还原（_restore_chat_model 的 fallback 会改 sync_tasks 内存）
    from model_router import ROUTE_TABLE
    _orig_table = copy.deepcopy(ROUTE_TABLE)
    try:
        # 执行 _restore_chat_model（应进 fallback 分支：provider 未注册 → 抛 LLMError → 内存回退）
        from web.server import _restore_chat_model
        _restore_chat_model(cfg, mock_core)

        # 关键断言 1：持久化值仍是 custom_unregistered_x（未被覆盖为 mimo）
        # 这是 sticky fallback 根因守护——fallback 不许写持久化
        saved = _json.loads(overrides_file.read_text(encoding="utf-8"))
        assert saved["models"]["chat_model"]["provider"] == "custom_unregistered_x", (
            f"持久化值应保留用户选择 custom_unregistered_x，实际 {saved['models']['chat_model']['provider']}"
        )
        assert saved["models"]["chat_model"]["model_id"] == "custom-model-x"

        # 关键断言 2：内存回退到 DEFAULT_PROVIDER（_current_chat_model 被设为默认）
        from config import DEFAULT_PROVIDER, get_default_model_for_provider
        _expected_fb_model = get_default_model_for_provider(DEFAULT_PROVIDER)
        assert mock_core.router._current_chat_model == {
            "provider": DEFAULT_PROVIDER, "model_id": _expected_fb_model,
        }, (
            f"内存应回退到 {DEFAULT_PROVIDER}/{_expected_fb_model}，"
            f"实际 {mock_core.router._current_chat_model}"
        )
    finally:
        # 还原 ROUTE_TABLE（fallback 分支 persist=False 改了 sync_tasks 内存）
        ROUTE_TABLE.clear()
        ROUTE_TABLE.update(_orig_table)


# ─────────────────────────────────────────────────────────────
# Task 6: _try_fallback_chain 不污染全局 ROUTE_TABLE
# ─────────────────────────────────────────────────────────────


def test_try_fallback_chain_uses_registry_snapshot():
    """_try_fallback_chain 应通过 self._registry.snapshot_task() 读取降级配置，
    而非直接读 ROUTE_TABLE（避免降级期间修改污染全局状态）。

    源码守护测试：确保重构落地，不会回退到旧实现。
    """
    import inspect
    from model_router import ModelRouter
    source = inspect.getsource(ModelRouter._try_fallback_chain)
    # 不应直接读 ROUTE_TABLE.get（应走 registry 快照）
    assert "ROUTE_TABLE.get(fallback_type)" not in source, (
        "_try_fallback_chain 应改用 self._registry.snapshot_task(fallback_type)，"
        "而非 ROUTE_TABLE.get(fallback_type)"
    )
    assert "ROUTE_TABLE.get(\"chat_agnes\")" not in source
    assert "ROUTE_TABLE.get('chat_agnes')" not in source
    # 应使用 registry 快照
    assert "self._registry.snapshot_task" in source or "self._registry.get_task" in source, (
        "_try_fallback_chain 应通过 self._registry.snapshot_task/get_task 读取降级配置"
    )


def test_fallback_chain_does_not_pollute_route_table():
    """降级链调用后 ROUTE_TABLE 全局状态不变。"""
    from model_router import ModelRouter, ROUTE_TABLE
    try:
        router = ModelRouter(api_key="fake")
    except (ImportError, OSError, ValueError, RuntimeError):
        pytest.skip("ModelRouter 在测试环境无法初始化")
    # 初始化 _registry（如果 __init__ 没初始化）
    if not hasattr(router, "_registry"):
        from model_router import ModelRouteRegistry
        router._registry = ModelRouteRegistry(ROUTE_TABLE)

    # 降级链：chat → chat_agnes（chat_pro/chat_flash 已合并进 chat）
    original_chat = copy.deepcopy(ROUTE_TABLE["chat"])
    original_chat_agnes = copy.deepcopy(ROUTE_TABLE["chat_agnes"])

    # mock _route_with_retry 返回成功（避免真实 LLM 调用）——用 AsyncMock 因为被 await
    router._route_with_retry = AsyncMock(return_value="fake_response")
    router._filter_tools_for_model = MagicMock(return_value=[])
    router._is_client_configured = MagicMock(return_value=True)

    import asyncio
    fake_error = Exception("simulated LLM failure")
    async def _run():
        await router._try_fallback_chain(
            fake_error, "chat", [], 0.7, False, None, None, 60,
            "user1", "session1", None, original_max_tokens=8192,
        )
    asyncio.run(_run())

    # ROUTE_TABLE 未被污染（降级链读取的是 registry 快照深拷贝）
    assert ROUTE_TABLE["chat"] == original_chat
    assert ROUTE_TABLE["chat_agnes"] == original_chat_agnes


def test_fallback_chain_agnes_uses_snapshot():
    """agnes 降级路径也应通过 registry 快照读取 chat_agnes 配置。"""
    import inspect
    from model_router import ModelRouter
    source = inspect.getsource(ModelRouter._try_fallback_chain)
    # 检查 agnes_config 的赋值来源
    assert "agnes_config = self._registry.snapshot_task" in source or \
           "agnes_config = self._registry.get_task" in source, (
        "agnes_config 应通过 self._registry.snapshot_task/get_task 读取"
    )


# ─────────────────────────────────────────────────────────────
# Task 7: 启动时清理死路由
# ─────────────────────────────────────────────────────────────


def test_apply_route_overrides_cleans_dead_routes(tmp_path):
    """启动时清理持久化文件中 ROUTE_TABLE 已不存在的死路由。

    回归场景：旧版本曾使用 chat_mimo/chat_mini/chat_ultra 等 task，升级后
    ROUTE_TABLE 已删除这些条目，但持久化文件 webui_overrides.json 还残留，
    导致 WebUI 显示僵尸路由让用户困惑，且每次启动都尝试应用无效覆盖。

    修复：_apply_route_overrides 检测 ROUTE_TABLE 中不存在的 task，从持久化文件删除。
    """
    import json as _json
    overrides_file = tmp_path / "webui_overrides.json"
    overrides_file.write_text(_json.dumps({
        "models": {
            "routes": {
                "chat": {"model": "mimo-v2.5", "client": "mimo",
                         "max_tokens": 131072, "thinking": False, "timeout": 60},
                "chat_mimo": {"model": "mimo-v2.5", "client": "mimo",
                              "max_tokens": 131072, "thinking": False, "timeout": 60},
                "chat_mini": {"model": "mimo-v2.5", "client": "mimo",
                              "max_tokens": 4096, "thinking": False, "timeout": 60},
                "chat_ultra": {"model": "mimo-v2.5", "client": "mimo",
                               "max_tokens": 1048576, "thinking": False, "timeout": 60},
            }
        }
    }), encoding="utf-8")

    from web.config_service import ConfigService
    cfg = ConfigService(path=overrides_file)

    mock_core = MagicMock()
    mock_core.router.TASK_TIMEOUTS = {}

    from model_router import ROUTE_TABLE
    # 确保 ROUTE_TABLE 中没有 chat_mimo/chat_mini/chat_ultra
    dead_routes = ("chat_mimo", "chat_mini", "chat_ultra")
    for dr in dead_routes:
        assert dr not in ROUTE_TABLE, f"测试前置失败：ROUTE_TABLE 不应有 {dr}"

    # 快照 ROUTE_TABLE 用于还原（存活路由 chat 的覆盖会通过 registry.update_route(persist=False)
    # 改真实 ROUTE_TABLE["chat"]，需在 finally 还原避免污染后续测试）
    _orig_table = copy.deepcopy(ROUTE_TABLE)
    try:
        from web.server import _apply_route_overrides
        _apply_route_overrides(cfg, mock_core, ROUTE_TABLE)

        # 死路由已从持久化文件删除
        saved = _json.loads(overrides_file.read_text(encoding="utf-8"))
        saved_routes = saved["models"]["routes"]
        assert "chat_mimo" not in saved_routes
        assert "chat_mini" not in saved_routes
        assert "chat_ultra" not in saved_routes
        # 存活路由保留
        assert "chat" in saved_routes
    finally:
        ROUTE_TABLE.clear()
        ROUTE_TABLE.update(_orig_table)


def test_apply_route_overrides_preserves_valid_routes(tmp_path):
    """死路由清理不影响存活路由的覆盖应用。"""
    import json as _json
    overrides_file = tmp_path / "webui_overrides.json"
    overrides_file.write_text(_json.dumps({
        "models": {
            "routes": {
                "chat": {"model": "agnes-2.0-flash", "client": "agnes",
                         "max_tokens": 8192, "thinking": False, "timeout": 90},
                "chat_agnes": {"model": "agnes-2.0-flash", "client": "agnes",
                               "max_tokens": 8192, "thinking": False, "timeout": 90},
            }
        }
    }), encoding="utf-8")

    from web.config_service import ConfigService
    cfg = ConfigService(path=overrides_file)

    mock_core = MagicMock()
    mock_core.router.TASK_TIMEOUTS = {}

    from model_router import ROUTE_TABLE
    # 快照整个 entry（包括 max_tokens/thinking），避免硬编码还原 client="mimo" 改错
    # chat_pro/chat_flash 已合并进 chat，改用 chat_agnes 作为第二个存活路由
    original_chat = copy.deepcopy(ROUTE_TABLE["chat"])
    original_chat_agnes = copy.deepcopy(ROUTE_TABLE["chat_agnes"])
    try:
        from web.server import _apply_route_overrides
        _apply_route_overrides(cfg, mock_core, ROUTE_TABLE)

        # 存活路由的覆盖已应用
        assert ROUTE_TABLE["chat"]["model"] == "agnes-2.0-flash"
        assert ROUTE_TABLE["chat"]["client"] == "agnes"
        assert ROUTE_TABLE["chat_agnes"]["model"] == "agnes-2.0-flash"
        assert mock_core.router.TASK_TIMEOUTS["chat"] == 90
    finally:
        # 整体还原（包括 max_tokens/thinking，不只 model/client）
        ROUTE_TABLE["chat"] = copy.deepcopy(original_chat)
        ROUTE_TABLE["chat_agnes"] = copy.deepcopy(original_chat_agnes)


# ─────────────────────────────────────────────────────────────
# Task 8: web/routers/models.py update_route 走 Registry
# ─────────────────────────────────────────────────────────────


def test_update_route_api_uses_registry():
    """WebUI update_route API 应通过 Registry 更新，不直接改 ROUTE_TABLE[task]。

    源码守护测试：确保 API 重构落地，不会回退到直接修改 ROUTE_TABLE。
    旧实现 `entry = ROUTE_TABLE[task]; entry["model"] = ...` 直接修改全局 dict，
    失败时不回滚，且持久化与内存可能不一致。新实现通过 registry.update_route 原子化。
    """
    import inspect
    from web.routers.models import update_route
    source = inspect.getsource(update_route)
    # 不应直接赋值修改 ROUTE_TABLE[task] 的字段（检查赋值语句，避免误匹配 final_entry["model"] 读取）
    assert "entry[\"model\"] =" not in source, (
        "update_route 不应直接赋值 entry[\"model\"]，应通过 registry.update_route()"
    )
    assert "entry['model'] =" not in source
    assert "entry[\"client\"] =" not in source
    assert "entry['client'] =" not in source
    assert "entry[\"max_tokens\"] =" not in source
    assert "entry[\"thinking\"] =" not in source
    # 应调用 registry
    assert "_registry" in source, (
        "update_route 应通过 core.router._registry.update_route() 原子化更新"
    )
    assert "registry.update_route" in source


def test_update_route_api_persists_via_registry(tmp_path):
    """update_route 通过 Registry 更新后，ConfigService 持久化被调用。"""
    import json as _json
    overrides_file = tmp_path / "webui_overrides.json"
    # 预置 agnes provider，避免被 "provider 不存在" 拦截
    overrides_file.write_text(_json.dumps({
        "models": {"routes": {}, "providers": {"agnes": {"label": "Agnes"}}}
    }), encoding="utf-8")

    from web.config_service import ConfigService
    cfg = ConfigService(path=overrides_file)

    from model_router import ModelRouter, ROUTE_TABLE
    from model_router import ModelRouteRegistry
    # 构造一个真实的 router 实例，registry 持有 cfg
    router_obj = ModelRouter.__new__(ModelRouter)
    router_obj._registry = ModelRouteRegistry(ROUTE_TABLE, config_service=cfg)
    router_obj.TASK_TIMEOUTS = {"chat": 60}

    original_chat = copy.deepcopy(ROUTE_TABLE["chat"])
    try:
        # 构造 mock request
        mock_request = MagicMock()
        mock_request.app.state.core.router = router_obj

        # patch _cfg 返回我们的 cfg，_router_of 返回 router_obj
        with patch("web.routers.models._cfg", return_value=cfg), \
             patch("web.routers.models._router_of", return_value=router_obj), \
             patch("web.routers.models._audit", new_callable=AsyncMock), \
             patch("web.routers.models._broadcast_changed", new_callable=AsyncMock):
            from web.routers.models import update_route
            import asyncio
            asyncio.run(update_route("chat", {
                "model": "agnes-2.0-flash", "provider": "agnes",
                "max_tokens": 8192, "thinking": False, "timeout": 90,
            }, mock_request))

        # 内存已更新
        assert ROUTE_TABLE["chat"]["model"] == "agnes-2.0-flash"
        assert ROUTE_TABLE["chat"]["client"] == "agnes"
        # 持久化已写入
        saved = _json.loads(overrides_file.read_text(encoding="utf-8"))
        assert saved["models"]["routes"]["chat"]["model"] == "agnes-2.0-flash"
        assert saved["models"]["routes"]["chat"]["client"] == "agnes"
        # task == chat 时同步 models.chat_model
        assert saved["models"]["chat_model"]["provider"] == "agnes"
        assert saved["models"]["chat_model"]["model_id"] == "agnes-2.0-flash"
    finally:
        # 整体还原（包括 max_tokens/thinking，测试写入了 max_tokens=8192, thinking=False）
        ROUTE_TABLE["chat"] = copy.deepcopy(original_chat)
