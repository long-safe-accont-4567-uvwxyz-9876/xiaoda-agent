"""Rust 混合加速接入层 — perf/rust-hybrid-poc

将扩散激活通道的 CPU 热点（_compute_idf + _direct_channel，实测 53ms/查询）
下沉到 rust_core.NodeIndex（常驻索引，实测 7.2ms/查询，7.3x）。

设计约束：
- 可开关：RUST_HYBRID_ENABLED=False（默认）时行为与主线完全一致；
- 回退安全：rust_core 不可导入 / .so 架构不符 / 运行时异常，一律静默回退
  纯 Python 路径，检索功能永不因本模块失败而中断；
- 语义不变：Rust 实现与 spreading_activation.py 逐字段对齐，
  tests/test_rust_hybrid_poc.py 用真实数据做等价性断言。
"""
from __future__ import annotations

import threading
from typing import Any

from loguru import logger

# 开关：统一走 config_constants（与项目其他开关一致），默认关闭。
# 开启需同时满足 ①开关 ②rust_core 可导入 ③节点数达标
try:
    from config_constants import RUST_HYBRID_ENABLED as _CFG_ENABLED
except ImportError:  # pragma: no cover - 独立使用 rust_hybrid 时兜底
    import os as _os

    _CFG_ENABLED = _os.getenv("RUST_HYBRID_ENABLED", "0") == "1"
RUST_HYBRID_ENABLED = _CFG_ENABLED
# 低于该节点数时 Python 路径已足够快（<5ms），不值得维护双份索引一致性
RUST_HYBRID_MIN_NODES = 500

_rust_core: Any | None = None
_import_attempted = False
_import_lock = threading.Lock()


def _try_import() -> Any | None:
    """惰性导入 rust_core 扩展模块（进程内单次尝试）。"""
    global _rust_core, _import_attempted
    if _import_attempted:
        return _rust_core
    with _import_lock:
        if _import_attempted:
            return _rust_core
        _import_attempted = True
        try:
            import rust_core as _rc  # type: ignore[import-not-found]

            _rust_core = _rc
            logger.info("rust_hybrid.module_loaded")
        except (ImportError, OSError) as e:
            logger.debug("rust_hybrid.module_unavailable error={}", str(e)[:120])
            _rust_core = None
    return _rust_core


class RustNodeIndex:
    """扩散通道常驻节点索引（Rust 加速封装）。

    与 SpreadingActivationEngine 的图快照同生命周期策略：TTL 内复用、
    记忆写入后由调用方 rebuild()。加载失败抛出原异常由调用方回退。
    """

    def __init__(self, alive_nodes: dict[str, dict]) -> None:
        rc = _try_import()
        if rc is None:
            raise RuntimeError("rust_core unavailable")
        node_ids: list[str] = []
        keys_json: list[str] = []
        texts: list[str] = []
        weights: list[float] = []
        for nid, node in alive_nodes.items():
            node_ids.append(nid)
            keys_json.append(node.get("keys", "[]"))
            texts.append(node.get("text", ""))
            weights.append(float(node.get("weight", 1.0)))
        self._index = rc.NodeIndex(node_ids, keys_json, texts, weights)
        self._rc = rc

    @property
    def size(self) -> int:
        return self._index.size

    def direct_channel(self, query: str, query_keys: list[str]) -> dict[str, float]:
        """语义与 spreading_activation._direct_channel 一致（含 IDF）。"""
        return self._index.direct_channel(query, query_keys)


def should_use_rust(alive_nodes_count: int) -> bool:
    """判定当前是否走 Rust 路径（开关 + 模块可用 + 规模阈值）。"""
    if not RUST_HYBRID_ENABLED:
        return False
    if alive_nodes_count < RUST_HYBRID_MIN_NODES:
        return False
    return _try_import() is not None
