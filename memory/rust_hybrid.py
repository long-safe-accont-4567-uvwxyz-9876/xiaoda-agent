"""Rust 混合加速接入层 — perf/rust-hybrid-poc

将扩散激活通道的 CPU 热点（_compute_idf + _direct_channel，实测 53ms/查询）
下沉到 rust_core.NodeIndex（常驻索引，实测 7.2ms/查询，7.3x）。

设计约束：
- 可开关：RUST_HYBRID_ENABLED=False（默认）时行为与主线完全一致；
- 回退安全：rust_core 不可导入 / .so 架构不符 / 二进制陈旧缺符号 /
  运行时异常，一律静默回退纯 Python 路径，检索功能永不因本模块失败而中断；
- 语义不变：Rust 实现与 spreading_activation.py 逐字段对齐，
  tests/test_rust_hybrid_poc.py 用真实数据做等价性断言。

二进制契约（CONTRACT_VERSION）：可导入 ≠ 契约满足——旧源码构建的 .so
能通过 import 却缺新类/方法，曾在测试处爆出
AttributeError: module 'rust_core' has no attribute 'NodeIndex'。
_try_import 对版本号+符号表双重校验，不符视同不可用走回退；
Rust 侧改 pyclass/pymethod 签名或语义时须同步 bump 双侧版本号
（lib.rs CONTRACT_VERSION ↔ 本文件 RUST_CORE_CONTRACT_VERSION），
build.sh 在同步产物前也会跑同一套探针。
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

# 二进制契约版本：与 rust_core/src/lib.rs 的 CONTRACT_VERSION 必须相等。
# 任何 pyclass/pymethod 的增删改（含语义变化）都要双侧同步 bump。
RUST_CORE_CONTRACT_VERSION = 2
# _try_import 返回模块必须满足的最小符号表（类 + 方法级探针）。
_REQUIRED_CONTRACT = ("NodeIndex", "CONTRACT_VERSION")


def _contract_ok(rc: Any) -> bool:
    """校验二进制契约：版本号相等 + 符号齐全。

    陈旧 .so（旧源码构建产物）或异机拷贝的 .so 可导入但不满足契约，
    一律视同不可用——调用方/测试按"扩展未构建"回退或 skip，
    而不是在使用点爆 AttributeError。
    """
    missing = [a for a in _REQUIRED_CONTRACT if not hasattr(rc, a)]
    if missing:
        logger.warning(
            "rust_hybrid.contract_missing symbols={} hint=bash rust_core/build.sh",
            ",".join(missing))
        return False
    if rc.CONTRACT_VERSION != RUST_CORE_CONTRACT_VERSION:
        logger.warning(
            "rust_hybrid.contract_version_mismatch binary={} python={} "
            "hint=bash rust_core/build.sh",
            rc.CONTRACT_VERSION, RUST_CORE_CONTRACT_VERSION)
        return False
    idx_cls = getattr(rc, "NodeIndex")
    for method in ("direct_channel", "load_edges", "spreading_channel", "size"):
        if not hasattr(idx_cls, method):
            logger.warning("rust_hybrid.contract_method_missing class=NodeIndex "
                           "method={} hint=bash rust_core/build.sh", method)
            return False
    return True

_rust_core: Any | None = None
_import_attempted = False
_import_lock = threading.Lock()


def _try_import() -> Any | None:
    """惰性导入 rust_core 扩展模块（进程内单次尝试）。

    导入成功后仍需通过 _contract_ok 契约校验（版本+符号），
    不满足契约视同不可用，返回 None 走纯 Python 回退。
    """
    global _rust_core, _import_attempted
    if _import_attempted:
        return _rust_core
    with _import_lock:
        if _import_attempted:
            return _rust_core
        _import_attempted = True
        try:
            import rust_core as _rc  # type: ignore[import-not-found]

            _rust_core = _rc if _contract_ok(_rc) else None
            if _rust_core is not None:
                logger.info("rust_hybrid.module_loaded contract=v{}",
                            RUST_CORE_CONTRACT_VERSION)
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

    def load_edges(self, rows: list[tuple[str, str, float]]) -> int:
        """驻留边快照（一次性），返回保留的边数。

        rows 与 db_concept.get_edge_snapshot 的行同构；两端 id 均在
        节点集内的边才保留（与游走期 alive 过滤等价）。
        """
        return self._index.load_edges(rows)

    def spreading_channel(self, seeds: list[tuple[str, float]],
                          radius: int = 3, decay: float = 0.5,
                          threshold: float = 0.05) -> dict[str, float]:
        """扩散激活图游走，语义与 spreading_activation._spreading_channel 一致。"""
        return self._index.spreading_channel(seeds, radius, decay, threshold)


def should_use_rust(alive_nodes_count: int) -> bool:
    """判定当前是否走 Rust 路径（开关 + 模块可用 + 规模阈值）。"""
    if not RUST_HYBRID_ENABLED:
        return False
    if alive_nodes_count < RUST_HYBRID_MIN_NODES:
        return False
    return _try_import() is not None
