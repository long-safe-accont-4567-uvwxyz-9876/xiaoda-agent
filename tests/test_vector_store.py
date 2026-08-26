"""memory/vector_store 定点回归：T1 重建任务保活 / D2 缓存加载向量化 / D1 候选集精确 L2。

三项优化（2026-08-26）：
- T1 裸 create_task GC 隐患：_auto_rebuild 挂强引用 + done-callback 记录异常；
- D2 EmbedCache 启动加载整矩阵一次 tolist()（与旧逐行转换值语义逐位一致）；
- D1 _search_candidates_exact numpy 批量 L2 与纯 Python 兜底结果一致性。
"""
from __future__ import annotations

import asyncio
import struct
from unittest.mock import MagicMock

import numpy as np
import pytest

from memory.vector_store import EmbedCache, VectorStore

# ── T1: _spawn_auto_rebuild / _on_rebuild_done ─────────────────────────


def _make_vs_minimal() -> VectorStore:
    """构造最小 VectorStore 实例（跳过 __init__ 的重 IO）。"""
    vs = VectorStore.__new__(VectorStore)
    vs._rebuild_task = None
    vs._rebuild_in_progress = False
    vs._needs_rebuild = False
    return vs


@pytest.mark.asyncio
async def test_spawn_auto_rebuild_holds_strong_reference():
    """_spawn_auto_rebuild 将 Task 挂到实例属性，GC 不会中途回收。"""
    vs = _make_vs_minimal()
    entered = asyncio.Event()

    async def _stub_rebuild():
        entered.set()
        await asyncio.sleep(0)

    vs._auto_rebuild = _stub_rebuild  # type: ignore[assignment]
    vs._spawn_auto_rebuild()
    assert vs._rebuild_task is not None, "任务必须挂到实例属性"
    assert not vs._rebuild_task.done()
    await vs._rebuild_task
    await asyncio.sleep(0)  # 让 done-callback 跑完
    assert vs._rebuild_task is None, "完成后回调应清引用"


@pytest.mark.asyncio
async def test_spawn_auto_rebuild_reentrant_guard():
    """已有在途任务时，重复调用不创建新任务（防重入）。"""
    vs = _make_vs_minimal()
    started = []
    release = asyncio.Event()

    async def _slow():
        started.append(1)
        await release.wait()

    vs._auto_rebuild = _slow  # type: ignore[assignment]
    vs._spawn_auto_rebuild()
    first = vs._rebuild_task
    vs._spawn_auto_rebuild()  # 应被拦截
    assert vs._rebuild_task is first, "第二次调用不应创建新任务"
    release.set()
    await first
    await asyncio.sleep(0)
    assert vs._rebuild_task is None
    assert len(started) == 1, "stub 只应启动一次"


@pytest.mark.asyncio
async def test_on_rebuild_done_logs_unexpected_exception():
    """重建任务抛异常时，done-callback 记录 error 日志而非静默吞掉。"""
    vs = _make_vs_minimal()
    errors = []

    async def _boom():
        raise ValueError("boom_rebuild_test")

    vs._auto_rebuild = _boom  # type: ignore[assignment]

    import memory.vector_store as _vsm
    _orig_logger = _vsm.logger
    stub = MagicMock()
    # loguru logger.error(msg, arg) 传两个位置参数
    stub.error = lambda *args, **kw: errors.append(" ".join(str(a) for a in args))
    _vsm.logger = stub
    try:
        vs._spawn_auto_rebuild()
        await asyncio.sleep(0.05)
        await asyncio.sleep(0)  # done-callback
    finally:
        _vsm.logger = _orig_logger
    assert any("boom_rebuild_test" in str(e) for e in errors), (
        f"异常日志应包含 'boom_rebuild_test'，实际: {errors}")


# ── D2: EmbedCache 启动加载 tolist() ───────────────────────────────────


def test_embed_cache_persist_roundtrip_values_identical(tmp_path):
    """put → 磁盘 → 重新加载，值逐位一致（float64 list），顺序保留。"""
    p = str(tmp_path / "cache.npz")
    c1 = EmbedCache(max_size=256, persist_path=p)
    # npz 存 float32，roundtrip 后精度受 float32 约束——用可精确表达的值
    vecs = [
        [0.5, 0.25, 0.125, 0.0625],
        [1.0, 2.0, 3.0, 4.0],
        [5.5, 6.25, 7.125, 8.0625],
    ]
    for i, v in enumerate(vecs):
        c1.put(f"text_{i}", v)
    del c1  # 触发 __del__ 保存

    c2 = EmbedCache(max_size=256, persist_path=p)
    for i, expected in enumerate(vecs):
        got = c2.get(f"text_{i}")
        assert got is not None, f"text_{i} 缓存缺失"
        # float32 roundtrip 精度：绝对误差 < 1e-6
        for g, e in zip(got, expected):
            assert abs(g - e) < 1e-6, f"text_{i} 值不一致: {got} vs {expected}"
        assert all(isinstance(x, float) for x in got)


def test_embed_cache_load_respects_max_size_and_order(tmp_path):
    """max_size 截断：只保留前 N 条（按 npz 内 key 顺序）。"""
    p = str(tmp_path / "cache.npz")
    keys = np.array(["k1", "k2", "k3", "k4", "k5"])
    vecs = np.arange(10, dtype=np.float32).reshape(5, 2)
    np.savez(p, keys=keys, vecs=vecs)

    cache = EmbedCache(max_size=3, persist_path=p)
    assert list(cache._cache.keys()) == ["k1", "k2", "k3"], (
        f"应只保留前 3 条: {list(cache._cache.keys())}")
    assert cache._cache["k2"] == [2.0, 3.0]


def test_embed_cache_corrupt_file_degrades_to_empty(tmp_path):
    """损坏文件 → 警告 + 降级为空缓存，不抛异常。"""
    p = tmp_path / "cache.npz"
    p.write_bytes(b"not a valid npz file")
    cache = EmbedCache(max_size=256, persist_path=str(p))
    assert len(cache._cache) == 0, "损坏文件应降级为空缓存"


# ── D1: _search_candidates_exact numpy vs 纯 Python ────────────────────


class _FakeVecConn:
    """模拟 sqlite_vec 连接：按 batch 参数过滤返回预设行。"""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, sql, params=None):
        ids = set(params) if params else set()
        self._last = [r for r in self._rows if r[0] in ids]
        return self

    def fetchall(self):
        return list(self._last)


def _encode_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def test_search_candidates_exact_uniform_dims():
    """全维度一致时 numpy 路径：结果与纯 Python 兜底逐位一致。"""
    vecs = [
        (1, _encode_blob([1.0, 0.0, 0.0, 0.0])),
        (2, _encode_blob([0.0, 1.0, 0.0, 0.0])),
        (3, _encode_blob([1.0, 1.0, 0.0, 0.0])),
        (4, _encode_blob([0.0, 0.0, 0.0, 0.0])),
    ]
    query = [1.0, 0.0, 0.0, 0.0]
    vs = VectorStore.__new__(VectorStore)
    vs._vec_conn = _FakeVecConn(vecs)

    result_np = vs._search_candidates_exact("t", query, [1, 2, 3, 4], 10)
    row_ids = [r[0] for r in vecs]
    raw_blobs = [r[1] for r in vecs]
    result_fb = VectorStore._exact_fallback(row_ids, raw_blobs, query)
    result_fb.sort(key=lambda x: (x[1], x[0]))
    result_fb = result_fb[:10]

    assert len(result_np) == len(result_fb)
    for (id_np, d_np), (id_fb, d_fb) in zip(result_np, result_fb):
        assert id_np == id_fb
        assert abs(d_np - d_fb) < 1e-5, f"距离差: {d_np} vs {d_fb}"


def test_search_candidates_exact_mixed_dims_falls_back():
    """维度不一致行被兜底路径正确剔除（不出现在结果中）。"""
    vecs = [
        (1, _encode_blob([1.0, 0.0])),
        (2, _encode_blob([0.0, 1.0, 0.0, 0.0])),  # 维度不一致
        (3, _encode_blob([0.0, 0.0])),
    ]
    query = [1.0, 0.0]
    vs = VectorStore.__new__(VectorStore)
    vs._vec_conn = _FakeVecConn(vecs)

    result = vs._search_candidates_exact("t", query, [1, 2, 3], 10)
    ids_in_result = [r[0] for r in result]
    assert 2 not in ids_in_result, "维度不一致的行应被剔除"


def test_search_candidates_exact_tie_breaking_rowid_asc():
    """距离相同时按 rowid 升序（稳定排序）。"""
    vecs = [
        (10, _encode_blob([1.0, 0.0])),
        (5, _encode_blob([1.0, 0.0])),   # 距离相同，rowid 更小
        (20, _encode_blob([0.0, 1.0])),
    ]
    query = [1.0, 0.0]
    vs = VectorStore.__new__(VectorStore)
    vs._vec_conn = _FakeVecConn(vecs)

    result = vs._search_candidates_exact("t", query, [10, 5, 20], 10)
    assert result[0][0] == 5, f"并列距离应 rowid 升序: {result}"
    assert result[1][0] == 10


def test_search_candidates_exact_top_k_trim():
    """结果被截断到 top_k。"""
    vecs = [(i, _encode_blob([float(i), 0.0])) for i in range(1, 6)]
    query = [0.0, 0.0]
    vs = VectorStore.__new__(VectorStore)
    vs._vec_conn = _FakeVecConn(vecs)

    result = vs._search_candidates_exact("t", query, list(range(1, 6)), 2)
    assert len(result) == 2


def test_search_candidates_exact_empty_returns_empty():
    """空候选列表返回空结果。"""
    vs = VectorStore.__new__(VectorStore)
    vs._vec_conn = _FakeVecConn([])
    assert vs._search_candidates_exact("t", [1.0], [], 10) == []
    assert vs._search_candidates_exact("t", [1.0], [1], 0) == []
    assert vs._search_candidates_exact("t", [1.0], [1], -1) == []
