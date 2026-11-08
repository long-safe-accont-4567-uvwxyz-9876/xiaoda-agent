"""路由表默认值刷新（2026-11-08，中等-7）。

ROUTE_TABLE 在 import 时按当时的默认 provider/model 构建。运行期
set_default_provider 改了默认值后，那些仍是出厂值的路由项会继续指向旧
provider；本模块记住出厂快照，只重写「当前值 == 出厂值」的项，用户或 WebUI
显式改过的路由不动。

放在独立模块而不是 model_router.py：后者已临近 900 行上限，且这部分与
路由执行无关。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from llm_gateway.agent_target import runtime_default_provider

_TABLE: dict[str, dict] | None = None
_SNAPSHOT: dict[str, dict[str, Any]] = {}


def install_route_table(table: dict[str, dict]) -> None:
    """记住路由表引用与出厂快照（可选；不调用时 refresh 首次自行安装）。"""
    global _TABLE
    _TABLE = table
    _SNAPSHOT.clear()
    for task, cfg in table.items():
        _SNAPSHOT[task] = {"client": cfg.get("client"), "model": cfg.get("model")}


def _ensure_installed() -> None:
    """首次调用时从 model_router 取表并快照（此时表仍是出厂值）。"""
    if _TABLE is not None:
        return
    from model_router import ROUTE_TABLE

    install_route_table(ROUTE_TABLE)


def refresh_route_defaults() -> int:
    """把仍是出厂默认值的路由项同步到当前默认 provider/model，返回改动数。"""
    _ensure_installed()
    if _TABLE is None:
        return 0
    provider = runtime_default_provider()
    if not provider:
        return 0
    from config import get_default_model_for_provider

    model = str(get_default_model_for_provider(provider) or "")
    changed = 0
    for task, snap in _SNAPSHOT.items():
        entry = _TABLE.get(task)
        if not entry:
            continue
        if entry.get("client") != snap["client"] or entry.get("model") != snap["model"]:
            continue
        entry["client"] = provider
        if model:
            entry["model"] = model
        # 刷新后同步快照，多次切换默认值时仍能继续跟随
        _SNAPSHOT[task] = {"client": entry["client"], "model": entry["model"]}
        changed += 1
    if changed:
        logger.info("router.route_defaults_refreshed provider={} tasks={}", provider, changed)
    return changed
