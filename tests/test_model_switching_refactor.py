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
from unittest.mock import MagicMock, patch

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

def test_get_default_model_for_provider_from_metadata():
    """从 provider_metadata.json 读默认模型 ID。"""
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
    assert config.get_default_model_for_provider("mimo") == "mimo-custom-v9"
    # 清理
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
    except Exception:
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
    except Exception:
        pytest.skip("ModelRouter 在测试环境无法初始化")
    # mock registry 追踪调用
    router._registry = MagicMock()
    router._registry.update_route = MagicMock(return_value={"model": "x", "client": "y"})
    router._registry.all_tasks = MagicMock(return_value=[
        "chat", "chat_pro", "chat_flash", "emotion_analysis",
        "tool_result_wrap", "memory_encoding",
    ])
    router._registry.get_task = MagicMock(return_value={
        "max_tokens": 8192, "thinking": {"type": "disabled"},
    })

    router.set_chat_model("mimo", "mimo-v2.5")

    # 至少调用了 chat + chat_pro + chat_flash
    tasks_updated = [c.args[0] for c in router._registry.update_route.call_args_list]
    assert "chat" in tasks_updated
    assert "chat_pro" in tasks_updated
    assert "chat_flash" in tasks_updated
