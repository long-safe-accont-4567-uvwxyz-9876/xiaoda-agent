#!/usr/bin/env python3
"""向量检索延迟基准——验证 10 万条数据量下的检索延迟预估。

背景（2026-08-07，香橙派本地 Embedding 接入）：
- 生产向量库当前 ~1.4 万条（sqlite-vec vec0 暴力扫描，实测 ~5ms/次）
- 理论预估：暴力扫描延迟随数据量线性增长，10 万条约 ~30-40ms；
  引入 HNSW 索引则 ~10ms 且几乎不随数据量增长
- 本脚本生成随机 512 维向量（对齐 BGE-small-zh-v1.5 输出维度），真实建表测量：
  1) sqlite-vec vec0 暴力扫描（当前生产方案）
  2) [可选] Faiss HNSW 对比（--faiss 启用；未安装则跳过）
  3) numpy 精确 KNN 参考（裸矩阵乘上界参考）

用法：
  python scripts/bench_vec_search.py                     # 默认 10 万条
  python scripts/bench_vec_search.py --n 10000 --fast    # 快速冒烟验证
  python scripts/bench_vec_search.py --n 100000 --faiss  # 含 HNSW 对比
  python scripts/bench_vec_search.py --db /path/x.db     # 复用已建库跳过插入
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DIMS = 512
DEFAULT_N = 100_000
BATCH = 1000  # 每批插入条数，控制峰值内存（约 2MB/批）


def _connect_vec(path: Path) -> sqlite3.Connection:
    """打开向量库并加载 sqlite_vec 扩展。"""
    import sqlite_vec

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _rand_vecs(n: int, dims: int, seed: int) -> np.ndarray:
    """生成 L2 归一化的随机向量（对齐生产 BGE 归一化输出）。"""
    rng = np.random.default_rng(seed=seed)
    v = rng.normal(size=(n, dims)).astype(np.float32)
    norm = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.maximum(norm, 1e-8)


def bench_sqlite_vec(path: Path, n: int, k: int, queries: int, dims: int) -> tuple[dict, float]:
    """sqlite-vec vec0 暴力扫描基准。返回 (统计, 构建秒)。"""
    from sqlite_vec import serialize_float32

    exists = path.exists() and path.stat().st_size > 4096
    conn = _connect_vec(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS items USING vec0(embedding float[{dims}])")

    if exists:
        build_s = 0.0
        n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    else:
        t0 = time.perf_counter()
        for i in range(0, n, BATCH):
            vecs = _rand_vecs(min(BATCH, n - i), dims, seed=1000 + i)
            rows = [(i + j + 1, serialize_float32(vecs[j])) for j in range(len(vecs))]
            conn.executemany(
                f"INSERT INTO items(rowid, embedding) VALUES (?, vec_f32(?))", rows
            )
            conn.commit()
        build_s = time.perf_counter() - t0

    # 预热 3 次
    warm = _rand_vecs(3, dims, seed=42)
    for w in warm:
        conn.execute(
            "SELECT rowid, distance FROM items WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            [serialize_float32(w), k],
        ).fetchall()

    lat: list[float] = []
    qs = _rand_vecs(queries, dims, seed=7)
    for q in qs:
        t0 = time.perf_counter()
        conn.execute(
            "SELECT rowid, distance FROM items WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            [serialize_float32(q), k],
        ).fetchall()
        lat.append((time.perf_counter() - t0) * 1000)
    conn.close()

    stats = _summarize(lat)
    stats["n"] = n
    stats["build_s"] = build_s
    return stats, build_s


def bench_faiss_hnsw(path: Path, n: int, k: int, queries: int, dims: int) -> tuple[dict, float] | None:
    """Faiss HNSW 基准（近似检索，efSearch=64）。未安装则返回 None。"""
    try:
        import faiss
    except ImportError:
        return None

    # 向量直接内存生成（HNSW 需全量 add，内存约 200MB + 索引开销）
    data = _rand_vecs(n, dims, seed=1000)
    idx = faiss.IndexHNSWFlat(dims, 32)
    idx.hnsw.efConstruction = 200
    t0 = time.perf_counter()
    idx.add(data)
    build_s = time.perf_counter() - t0
    idx.hnsw.efSearch = 64

    qs = _rand_vecs(queries, dims, seed=7)
    lat: list[float] = []
    for q in qs:
        t0 = time.perf_counter()
        idx.search(q[None, :], k)
        lat.append((time.perf_counter() - t0) * 1000)

    stats = _summarize(lat)
    stats["n"] = n
    stats["build_s"] = build_s
    return stats, build_s


def bench_numpy_knn(n: int, k: int, queries: int, dims: int, db_path: Path) -> tuple[dict, float] | None:
    """numpy 精确 KNN 参考：从 sqlite-vec 库读出全部向量后做矩阵乘（暴力上界参考）。"""
    try:
        conn = _connect_vec(db_path)
        rows = conn.execute("SELECT embedding FROM items").fetchall()
        conn.close()
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None

    data = np.stack([np.frombuffer(r[0], dtype=np.float32) for r in rows])
    qs = _rand_vecs(queries, dims, seed=7)
    lat: list[float] = []
    for q in qs:
        t0 = time.perf_counter()
        scores = data @ q  # L2 归一化向量点积等价于余弦相似度
        np.argpartition(-scores, k)[:k]
        lat.append((time.perf_counter() - t0) * 1000)

    stats = _summarize(lat)
    stats["n"] = len(data)
    stats["build_s"] = 0.0
    return stats, 0.0


def _summarize(lat: list[float]) -> dict:
    lat_sorted = sorted(lat)
    return {
        "mean_ms": round(statistics.mean(lat), 2),
        "median_ms": round(statistics.median(lat), 2),
        "p95_ms": round(lat_sorted[int(len(lat_sorted) * 0.95)], 2),
        "p99_ms": round(lat_sorted[int(len(lat_sorted) * 0.99)], 2),
        "queries": len(lat),
    }


def _fmt(name: str, stats: dict) -> str:
    return (
        f"[{name}] n={stats['n']:,} 构建={stats['build_s']:.1f}s\n"
        f"   查询({stats['queries']}次,k=10): mean={stats['mean_ms']}ms "
        f"median={stats['median_ms']}ms p95={stats['p95_ms']}ms p99={stats['p99_ms']}ms"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="向量检索延迟基准（10 万条量级预估验证）")
    parser.add_argument("--n", type=int, default=DEFAULT_N, help="向量条数（默认 100000）")
    parser.add_argument("--dims", type=int, default=DIMS, help="向量维度（默认 512）")
    parser.add_argument("--k", type=int, default=10, help="top-k（默认 10）")
    parser.add_argument("--queries", type=int, default=100, help="查询次数（默认 100）")
    parser.add_argument("--faiss", action="store_true", help="启用 Faiss HNSW 对比（需已安装）")
    parser.add_argument("--db", default=None, help="sqlite-vec 临时库路径（默认 logs/bench_vec.db）")
    parser.add_argument("--keep", action="store_true", help="保留临时库文件")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else ROOT / "logs" / "bench_vec.db"

    print(f"=== 数据量={args.n:,} 维度={args.dims} k={args.k} 查询={args.queries} 次 ===", flush=True)
    print(f"=== 环境: {sys.platform} / cpu_count={os.cpu_count()} ===", flush=True)
    print("", flush=True)

    results: list[str] = []

    # 1) sqlite-vec（当前生产方案）
    stats, _ = bench_sqlite_vec(db_path, args.n, args.k, args.queries, args.dims)
    results.append(_fmt("sqlite-vec vec0 暴力扫描（当前生产）", stats))

    # 2) numpy 精确 KNN 参考（同一份库数据）
    ref = bench_numpy_knn(args.n, args.k, args.queries, args.dims, db_path)
    if ref:
        results.append(_fmt("numpy 精确 KNN 参考（上界参考）", ref[0]))

    # 3) Faiss HNSW 对比（可选）
    if args.faiss:
        faiss_stats = bench_faiss_hnsw(db_path, args.n, args.k, args.queries, args.dims)
        if faiss_stats is None:
            print("[skip] Faiss HNSW：faiss 未安装（pip install faiss-cpu）", flush=True)
        else:
            results.append(_fmt("Faiss HNSW (M=32, efSearch=64)", faiss_stats[0]))

    print("\n".join(results), flush=True)
    print("", flush=True)

    # 线性增长校验：10 万 / 1.4 万 ≈ 7 倍 → 暴力扫描应从 ~5ms 涨到 ~35ms
    if stats["n"] >= 80_000:
        proj = round(5.0 * stats["n"] / 14_000, 1)
        print(f"[结论] 暴力扫描线性预估 {proj}ms vs 实测 {stats['mean_ms']}ms", flush=True)

    if not args.keep:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
