"""model_router Phase 2（ModelRouteRegistry 抽出）结构契约测试。

背景：ModelRouteRegistry 是 ROUTE_TABLE 的唯一读写入口（原子更新 +
持久化回滚），自 model_router.py 抽为独立模块 model_router_registry.py，
函数体逐字节搬移。model_router 同名 re-export 保持
`from model_router import ModelRouteRegistry` 兼容。

行为契约（与 test_route_registry*.py 的功能测试互补，这里只锁结构 +
核心原子性）：
    1. 独立可导入，无 model_router 依赖（防循环导入）
    2. model_router.ModelRouteRegistry is model_router_registry.ModelRouteRegistry
    3. update_route 原子性：持久化失败回滚内存
    4. get_task 深拷贝 / get_task_ref 引用语义
    5. replace_table 保持 _table 对象身份（生产中即 ROUTE_TABLE 本身）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import model_router
from model_router_registry import ModelRouteRegistry


def _initial_table() -> dict:
    return {
        "chat": {"model": "m1", "client": "mimo", "max_tokens": 100,
                 "thinking": {"type": "disabled"}},
        "chat_agnes": {"model": "a1", "client": "agnes", "max_tokens": 50,
                       "thinking": {"type": "disabled"}},
    }


# ── 1/2. 独立导入 + re-export 同对象 ─────────────────────────────

def test_registry_imports_standalone():
    import importlib
    mod = importlib.import_module("model_router_registry")
    assert hasattr(mod, "ModelRouteRegistry")


def test_model_router_reexports_same_class():
    assert model_router.ModelRouteRegistry is ModelRouteRegistry


# ── 3. update_route 原子性 ───────────────────────────────────────

def test_update_route_rolls_back_on_persist_failure():
    class BoomCfg:
        def set(self, *a, **k):
            raise OSError("disk full")

    table = _initial_table()
    reg = ModelRouteRegistry(table, config_service=BoomCfg())
    with pytest.raises(RuntimeError):
        reg.update_route("chat", model_id="m2", provider="mimo")
    # 回滚：内存仍是旧值
    assert table["chat"]["model"] == "m1"


def test_update_route_persist_off_no_cfg_call():
    class SpyCfg:
        def __init__(self):
            self.calls = 0

        def set(self, *a, **k):
            self.calls += 1

    spy = SpyCfg()
    reg = ModelRouteRegistry(_initial_table(), config_service=spy)
    reg.update_route("chat", model_id="m2", provider="mimo", persist=False)
    assert spy.calls == 0


def test_update_route_unknown_task_raises():
    reg = ModelRouteRegistry(_initial_table())
    with pytest.raises(KeyError):
        reg.update_route("nope", model_id="m", provider="p")


# ── 4. 深拷贝 vs 引用语义 ────────────────────────────────────────

def test_get_task_returns_deepcopy():
    table = _initial_table()
    reg = ModelRouteRegistry(table)
    got = reg.get_task("chat")
    got["model"] = "MUTATED"
    assert table["chat"]["model"] == "m1"


def test_get_task_ref_returns_reference():
    table = _initial_table()
    reg = ModelRouteRegistry(table)
    assert reg.get_task_ref("chat") is table["chat"]


# ── 5. replace_table 保持身份 ────────────────────────────────────

def test_replace_table_preserves_identity():
    table = _initial_table()
    reg = ModelRouteRegistry(table)
    inner = reg._table
    reg.replace_table({"chat": {"model": "x", "client": "y"}})
    assert reg._table is inner
    assert table["chat"]["model"] == "x"
    assert "chat_agnes" not in table
