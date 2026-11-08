"""路由表默认值刷新（2026-11-08，中等-7）。

ROUTE_TABLE 在 import 时按当时的默认 provider/model 构建。运行期
set_default_provider 改了默认值后，那些仍是出厂值的路由项会继续指向旧
provider；本模块记住出厂快照，只重写「当前值 == 出厂值」的项，用户或 WebUI
显式改过的路由不动。

2026-11-08 收尾（快照时机）：出厂快照必须在 ROUTE_TABLE 定义处、由
model_router 立即 install——那里表刚由代码常量（config.DEFAULT_PROVIDER /
MODEL_NAME / FLASH_MODEL_NAME / AGNES_TEXT_MODEL）构建，还没有经过
web/server.py:_apply_route_overrides 的 WebUI 覆盖，也没被 _restore_chat_model
或运行期 set_default_provider 改写。旧实现等到首次 refresh 才惰性 install，
于是 WebUI 覆盖被当成出厂值，用户改过的路由会被默认 provider 冲掉；反过来
若 install 之前就调过 set_default_provider，则这一轮默认值切换完全落空。

放在独立模块而不是 model_router.py：后者已临近 900 行上限，且这部分与
路由执行无关。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from llm_gateway.agent_target import runtime_default_provider

_TABLE: dict[str, dict] | None = None
_SNAPSHOT: dict[str, dict[str, Any]] = {}
# 参与「是否仍是出厂值」判定的字段：两者都相等才算没被改过
_SNAPSHOT_FIELDS = ("client", "model")


def install_route_table(
    table: dict[str, dict],
    factory: dict[str, dict[str, Any]] | None = None,
) -> None:
    """记住路由表引用，并取出厂快照。

    `factory` 省略时按传入表的当前值取——只允许在 ROUTE_TABLE 定义处
    （import 期）调用，那时表刚由代码常量构建、尚未被任何覆盖改写；
    显式传 `factory` 供测试还原快照用。
    """
    global _TABLE
    _TABLE = table
    source = factory if factory is not None else table
    _SNAPSHOT.clear()
    for task, cfg in source.items():
        _SNAPSHOT[task] = {field: cfg.get(field) for field in _SNAPSHOT_FIELDS}


def factory_snapshot() -> dict[str, dict[str, Any]]:
    """返回出厂快照副本（测试与诊断用；改副本不影响刷新判定）。"""
    return {task: dict(values) for task, values in _SNAPSHOT.items()}


def refresh_route_defaults() -> int:
    """把仍是出厂默认值的路由项同步到当前默认 provider/model，返回改动数。

    只重写「client 与 model 都还等于出厂快照」的项；任一处被改过都算用户或
    WebUI 的选择，原样保留。ROUTE_TABLE 尚未构建（未 install）时返回 0，不再
    反向 import model_router 去补装——那条惰性路径正是本次要消灭的时机缺陷。
    """
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
        if any(entry.get(field) != snap[field] for field in _SNAPSHOT_FIELDS):
            continue
        entry["client"] = provider
        if model:
            entry["model"] = model
        # 刷新后同步快照，多次切换默认值时仍能继续跟随
        _SNAPSHOT[task] = {field: entry.get(field) for field in _SNAPSHOT_FIELDS}
        changed += 1
    if changed:
        logger.info("router.route_defaults_refreshed provider={} tasks={}", provider, changed)
    return changed
