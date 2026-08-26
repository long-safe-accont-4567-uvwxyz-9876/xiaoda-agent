"""numpy 内存暴力向量索引 — sqlite-vec 暴力 KNN 的零依赖加速层。

背景（2026-08-08 决策，替代 hnswlib HNSW）：
- sqlite-vec 暴力 KNN 实测 33.5ms/次（13761 条 × 512 维，JSON 反序列化是主要开销）
- hnswlib HNSW 板端实测：memories_child_vec（11400 条）top-10 与暴力重合率仅 ~73%，
  ef/M/去重/rerank 均无法改善（512 维高维 + BGE 向量相似度高度集中 + 36% 重复向量
  → ANN 贪心图搜索召回先天受限），未达 ≥98% 验收标准，弃用。
- numpy 内存暴力：11400×512 float32 = 23MB 常驻内存，BLAS 点积 4.5ms/次，
  与 SQLite 暴力结果 100% 一致（同 L2 欧氏距离度量），零额外依赖。

设计约定：
- 与 sqlite-vec 对齐的距离：L2 欧氏距离（sqrt(sum((v-q)^2))），数值一致
- 删除：软删除（rowid 黑名单），节点保留在矩阵中，search 时排除；
  由全量重建（load_from_db）回收空间
- upsert：rowid 已存在 → 覆盖矩阵对应行；新 rowid → 追加（容量不足自动扩容）
- 持久化：每表 npz（data/rids）+ meta.json（count/deleted/rowid→pos 映射）
- SQLite 仍是唯一数据源（可回滚），本索引是加速副本，可随时全量重建
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from utils.atomic_write import atomic_write
except (ImportError, AttributeError):
    atomic_write = None  # type: ignore[assignment]
except Exception:
    logger.exception(".memory.numpy_index.unexpected")
    atomic_write = None  # type: ignore[assignment]

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore
    HAS_NUMPY = False

# 与 sqlite-vec 虚拟表一一对应
TABLES = ("memories_vec", "memories_child_vec", "kg_entities_vec", "kg_relations_vec")

_META_NAME = "meta.json"


class _TableBuffer:
    """单表的内存向量缓冲区（预分配容量 + rowid 映射 + 软删除）。"""

    __slots__ = ("data", "rids", "count", "pos_of_rowid", "deleted", "dim", "normsq")

    def __init__(self, dim: int, cap: int) -> None:
        self.dim = dim
        self.data = np.zeros((cap, dim), dtype=np.float32)
        self.rids = np.zeros(cap, dtype=np.int64)
        self.normsq = np.zeros(cap, dtype=np.float32)  # 每行 L2²（预计算加速距离）
        self.count = 0
        self.pos_of_rowid: dict[int, int] = {}
        self.deleted: set[int] = set()


class NumpyBruteIndex:
    """多表 numpy 内存暴力向量索引。

    线程安全：内部持 RLock；调用方（vector_store）也持有自己的锁，双保险。
    """

    def __init__(self, dim: int, base_dir: str | Path, *,
                 max_elements: int = 20000,
                 ef_construction: int = 0, m: int = 0) -> None:
        # ef_construction/m 仅保留以兼容 HnswIndex 调用签名（暴力检索不使用）
        self._dim = dim
        self._base_dir = Path(base_dir)
        self._max_elements = max_elements
        self._buffers: dict[str, _TableBuffer] = {}
        self._lock = threading.RLock()
        self._loaded = False
        self._load_error = ""

    # ── 状态 ──────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._loaded and HAS_NUMPY

    @property
    def dimensions(self) -> int:
        return self._dim

    @property
    def stats(self) -> dict:
        return {
            "loaded": self._loaded,
            "tables": {
                t: {
                    "count": buf.count,
                    "alive": buf.count - len(buf.deleted),
                    "deleted": len(buf.deleted),
                }
                for t, buf in self._buffers.items()
            },
        }

    def _ensure_buffer(self, table: str) -> _TableBuffer:
        buf = self._buffers.get(table)
        if buf is None:
            buf = _TableBuffer(self._dim, max(self._max_elements, 1000))
            self._buffers[table] = buf
        return buf

    def _resize(self, buf: _TableBuffer, need: int) -> None:
        if buf.count + need <= buf.data.shape[0]:
            return
        new_cap = max(buf.data.shape[0] * 2, buf.count + need + 1000)
        new_data = np.zeros((new_cap, self._dim), dtype=np.float32)
        new_data[: buf.count] = buf.data[: buf.count]
        new_rids = np.zeros(new_cap, dtype=np.int64)
        new_rids[: buf.count] = buf.rids[: buf.count]
        new_normsq = np.zeros(new_cap, dtype=np.float32)
        new_normsq[: buf.count] = buf.normsq[: buf.count]
        buf.data, buf.rids, buf.normsq = new_data, new_rids, new_normsq

    # ── 初始化与全量重建 ──────────────────────────────────

    def load_from_db(self, conn: Any) -> bool:
        """从 sqlite-vec 4 张表全量构建内存矩阵（首次建索引/重建）。"""
        if not HAS_NUMPY:
            self._load_error = "numpy not installed"
            logger.warning("numpy_index.unavailable reason=numpy_missing")
            return False
        try:
            self._buffers.clear()
            for table in TABLES:
                rows = conn.execute(
                    f"SELECT rowid, embedding FROM {table}"
                ).fetchall()
                if not rows:
                    self._ensure_buffer(table)
                    continue
                vecs: list[Any] = []
                ids: list[int] = []
                for rid, emb in rows:
                    raw = emb if isinstance(emb, (bytes, bytearray)) else None
                    if raw is None or len(raw) != self._dim * 4:
                        continue  # 脏数据跳过，SQLite 仍为源
                    vecs.append(np.frombuffer(raw, dtype=np.float32))
                    ids.append(int(rid))
                buf = _TableBuffer(self._dim, max(len(vecs) * 2 + 1000, 1000))
                if vecs:
                    buf.data[: len(vecs)] = np.stack(vecs).astype(np.float32)
                    buf.rids[: len(vecs)] = np.array(ids, dtype=np.int64)
                    buf.normsq[: len(vecs)] = np.sum(buf.data[: len(vecs)] ** 2, axis=1)
                    buf.count = len(vecs)
                    buf.pos_of_rowid = {rid: i for i, rid in enumerate(ids)}
                self._buffers[table] = buf
            self._loaded = True
            self._load_error = ""
            logger.info("numpy_index.rebuilt", dim=self._dim,
                        counts={t: b.count for t, b in self._buffers.items()})
            return True
        except Exception as e:  # noqa: BLE001
            self._loaded = False
            self._load_error = str(e)
            logger.warning("numpy_index.rebuild_failed error={}", str(e))
            return False

    # ── 增量写 ────────────────────────────────────────────

    def upsert(self, table: str, rowid: int, vec: list[float] | Any) -> bool:
        """写入/更新单条向量（rowid 已存在则覆盖矩阵行，否则追加）。"""
        if not self.ready or table not in TABLES:
            return False
        with self._lock:
            try:
                arr = np.asarray(vec, dtype=np.float32).reshape(-1)
                if arr.size != self._dim:
                    return False
                buf = self._ensure_buffer(table)
                rid = int(rowid)
                pos = buf.pos_of_rowid.get(rid)
                if pos is None:
                    self._resize(buf, 1)
                    pos = buf.count
                    buf.data[pos] = arr
                    buf.rids[pos] = rid
                    buf.normsq[pos] = float(arr @ arr)
                    buf.count += 1
                    buf.pos_of_rowid[rid] = pos
                else:
                    buf.data[pos] = arr
                    buf.normsq[pos] = float(arr @ arr)
                buf.deleted.discard(rid)
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning("numpy_index.upsert_failed table={} rowid={} error={}",
                               table, rowid, str(e))
                return False

    def delete(self, table: str, rowid: int) -> bool:
        """软删除（rowid 加入黑名单，search 排除；全量重建回收空间）。"""
        if not self.ready or table not in TABLES:
            return False
        with self._lock:
            try:
                buf = self._buffers.get(table)
                if buf is None or int(rowid) not in buf.pos_of_rowid:
                    return False
                buf.deleted.add(int(rowid))
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning("numpy_index.delete_failed table={} rowid={} error={}",
                               table, rowid, str(e))
                return False

    # ── 检索 ──────────────────────────────────────────────

    def search(self, table: str, vec: list[float] | Any, top_k: int = 5,
               candidate_ids: list[int] | None = None,
               ef: int | None = None) -> list[tuple[int, float]] | None:
        """精确暴力 KNN，返回 [(rowid, L2 distance), ...]（与 sqlite-vec 同度量）。

        - candidate_ids 提供时仅在该集合内检索
        - 软删除节点已排除
        - 返回 None 表示索引不可用/检索失败（调用方回退 SQLite）；
          返回 [] 表示正常检索但无匹配
        """
        if not self.ready or table not in TABLES:
            return None
        with self._lock:
            try:
                buf = self._buffers.get(table)
                if buf is None or buf.count == 0:
                    return []
                arr = np.asarray(vec, dtype=np.float32).reshape(-1)
                if arr.size != self._dim:
                    return None
                # dist² = ||data||² + ||q||² - 2·data·q（预计算 ||data||²）
                dots = buf.data[: buf.count] @ arr
                dist2 = buf.normsq[: buf.count] + float(arr @ arr) - 2.0 * dots
                dist2 = np.maximum(dist2, 0.0)
                if candidate_ids is not None:
                    cand = set(candidate_ids)
                    mask = np.zeros(buf.count, dtype=bool)
                    for rid in cand:
                        pos = buf.pos_of_rowid.get(rid)
                        if pos is not None:
                            mask[pos] = True
                else:
                    mask = np.ones(buf.count, dtype=bool)
                if buf.deleted:
                    for rid in buf.deleted:
                        pos = buf.pos_of_rowid.get(rid)
                        if pos is not None:
                            mask[pos] = False
                valid = np.flatnonzero(mask)
                if valid.size == 0:
                    return []
                k = min(top_k, valid.size)
                sub = dist2[valid]
                order = np.argpartition(sub, k - 1)[:k]
                order = order[np.argsort(sub[order], kind="stable")]
                results = []
                for idx in order:
                    pos = int(valid[idx])
                    results.append((int(buf.rids[pos]), float(np.sqrt(dist2[pos]))))
                # tie-breaking：distance 相同时按 rowid 稳定排序（与 sqlite 路径一致）
                results.sort(key=lambda r: (r[1], r[0]))
                return results[:top_k]
            except Exception as e:  # noqa: BLE001
                logger.warning("numpy_index.search_failed table={} error={}",
                               table, str(e))
                return None

    # ── 持久化 ────────────────────────────────────────────

    def save(self) -> bool:
        """保存全部矩阵与元数据到 base_dir（npz + meta.json）。"""
        if not self.ready:
            return False
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            meta: dict[str, Any] = {
                "dim": self._dim,
                "tables": {},
            }
            for table, buf in self._buffers.items():
                np.savez_compressed(
                    str(self._base_dir / f"{table}.npz"),
                    data=buf.data[: buf.count],
                    rids=buf.rids[: buf.count],
                )
                meta["tables"][table] = {
                    "count": buf.count,
                    "deleted": sorted(buf.deleted),
                }
            _meta_path = self._base_dir / _META_NAME
            _payload = json.dumps(meta)
            if atomic_write is not None:
                atomic_write(_meta_path, _payload, encoding="utf-8")
            else:
                _meta_path.write_text(_payload, encoding="utf-8")
            logger.info("numpy_index.saved", dir=str(self._base_dir))
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("numpy_index.save_failed error={}", str(e))
            return False

    def load(self) -> bool:
        """从磁盘恢复（meta.json + 各表 npz）。"""
        if not HAS_NUMPY:
            return False
        meta_path = self._base_dir / _META_NAME
        if not meta_path.exists():
            return False
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if meta.get("dim") != self._dim:
                logger.warning("numpy_index.load_dim_mismatch expected={} got={}",
                               self._dim, meta.get("dim"))
                return False
            for table, info in meta.get("tables", {}).items():
                path = self._base_dir / f"{table}.npz"
                if not path.exists():
                    continue
                npz = np.load(str(path))
                data, rids = npz["data"], npz["rids"]
                n = int(info["count"])
                buf = _TableBuffer(self._dim, max(n * 2 + 1000, 1000))
                buf.data[:n] = data
                buf.rids[:n] = rids
                buf.normsq[:n] = np.sum(data ** 2, axis=1)
                buf.count = n
                buf.pos_of_rowid = {int(rid): i for i, rid in enumerate(rids[:n])}
                buf.deleted = set(info.get("deleted", []))
                self._buffers[table] = buf
            self._loaded = True
            self._load_error = ""
            logger.info("numpy_index.loaded", dir=str(self._base_dir),
                        counts={t: b.count for t, b in self._buffers.items()})
            return True
        except Exception as e:  # noqa: BLE001
            self._buffers.clear()
            self._loaded = False
            self._load_error = str(e)
            logger.warning("numpy_index.load_failed error={}", str(e))
            return False

    def close(self) -> None:
        """释放内存矩阵。"""
        with self._lock:
            self._buffers.clear()
            self._loaded = False
