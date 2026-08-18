#!/usr/bin/env python3
"""生产路径本地延迟基准：NPU 常驻流 embedding + numpy 暴力检索 端到端。

与生产完全同构：
- AdaptiveEmbeddingProvider（LOCAL_EMBED_BACKEND=npu，threshold=256）：
  短文本(<256 token)→CPU onnxruntime；长文本(>256 token)→NPU 常驻子进程
- NumpyBruteIndex：与生产同库（agent_vec.db）加载 + search

输出：embedding 延迟（短/长）、检索延迟、端到端汇总，并给出 vs 远程 API(5s) 对比。
用法：python scripts/bench_local_latency.py [--short N] [--long N] [--k 10]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DATA_DIR = os.getenv("KIOXIA_DATA_DIR", "") or str(Path.home() / ".ai-agent" / "data")
MODEL = Path(os.getenv("LOCAL_EMBED_MODEL_DIR", "") or str(Path(_DATA_DIR) / "models" / "bge-small-zh-v1.5"))
ADB = str(Path(_DATA_DIR) / "db" / "agent.db")
VDB = str(Path(_DATA_DIR) / "db" / "agent_vec.db")
API_BASELINE_MS = 5000.0  # 之前远程 API embedding 的典型端到端延迟（用户实测）


def _fmt(name: str, lat: list[float]) -> str:
    lat = sorted(lat)
    return (f"{name}: n={len(lat)} mean={statistics.mean(lat):.1f}ms "
            f"median={statistics.median(lat):.1f}ms "
            f"p95={lat[int(len(lat) * 0.95)]:.1f}ms "
            f"min={lat[0]:.1f}ms max={lat[-1]:.1f}ms")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--short", type=int, default=10, help="短文本（CPU 路径）条数")
    ap.add_argument("--long", type=int, default=10, help="长文本（NPU 路径）条数")
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    os.environ.setdefault("LOCAL_EMBED_BACKEND", "npu")
    from memory.npu_embed import AdaptiveEmbeddingProvider  # noqa: E402
    from memory.numpy_index import NumpyBruteIndex  # noqa: E402

    # ── 取真实文本：短/长各取足量后按 token 长度分桶 ──
    con = sqlite3.connect(ADB)
    rows = con.execute(
        "SELECT summary FROM episodic_memories "
        "WHERE summary IS NOT NULL AND trim(summary) != '' "
        "ORDER BY id DESC LIMIT 500"
    ).fetchall()
    con.close()
    texts = [r[0] for r in rows]

    provider = AdaptiveEmbeddingProvider(MODEL, query_prefix="")
    t0 = time.time()
    ok = provider.load()
    load_s = time.time() - t0
    print(f"[env] adaptive_embed load={ok} 用时={load_s:.2f}s "
          f"npu_ready={provider._npu.ready} threshold={provider._threshold}",
          flush=True)
    if not ok:
        print("[error] provider 加载失败")
        return 1

    # 按 token 长度分桶（与生产路由逻辑一致）
    lens = provider._token_lens(texts)
    short_texts = [t for t, l in zip(texts, lens) if l <= provider._threshold]
    long_texts = [t for t, l in zip(texts, lens) if l > provider._threshold]
    print(f"[data] 总文本={len(texts)} 短={len(short_texts)} 长={len(long_texts)} "
          f"token长度范围={min(lens)}~{max(lens)}", flush=True)

    # ── embedding 延迟（warmup 1 条后各测 N 条） ──
    short_lat: list[float] = []
    long_lat: list[float] = []
    samples_short = short_texts[:args.short]
    samples_long = long_texts[:args.long]
    if samples_short:
        provider.embed(samples_short[0])  # warmup（触发懒加载）
        for t in samples_short:
            s = time.perf_counter()
            provider.embed(t)
            short_lat.append((time.perf_counter() - s) * 1000)
    if samples_long:
        provider.embed(samples_long[0])  # warmup（NPU runner 首次推理）
        for t in samples_long:
            s = time.perf_counter()
            provider.embed(t)
            long_lat.append((time.perf_counter() - s) * 1000)
    print(f"\n[embedding 短文本 CPU 路径] {_fmt('latency', short_lat)}", flush=True)
    print(f"[embedding 长文本 NPU 路径] {_fmt('latency', long_lat)}", flush=True)

    # ── 检索延迟（numpy 暴力索引，与生产同库同参数） ──
    import sqlite_vec  # noqa: E402
    vcon = sqlite3.connect(VDB)
    vcon.enable_load_extension(True)
    sqlite_vec.load(vcon)
    idx = NumpyBruteIndex(dim=512, base_dir="/tmp/bench_brute")
    t0 = time.time()
    idx.load_from_db(vcon)
    print(f"[检索] numpy 索引加载(全量重建)={time.time()-t0:.1f}s stats={idx.stats['tables']}", flush=True)

    q = provider.embed(samples_short[0]) if samples_short else provider.embed(texts[0])
    idx.search("memories_child_vec", q, top_k=args.k)  # warmup
    search_lat: list[float] = []
    for i in range(args.short + args.long):
        tq = provider.embed(texts[i])
        if not tq:
            continue
        s = time.perf_counter()
        idx.search("memories_child_vec", tq, top_k=args.k)
        search_lat.append((time.perf_counter() - s) * 1000)
    vcon.close()
    print(f"[检索 numpy 精确 top-{args.k}] {_fmt('latency', search_lat)}", flush=True)

    # ── 端到端汇总 + API 对比 ──
    e2e = short_lat + long_lat + search_lat
    if e2e:
        mean = statistics.mean(e2e)
        print("\n=== 汇总 ===", flush=True)
        print(f"[端到端 embed+search] mean={mean:.1f}ms "
              f"p95={sorted(e2e)[int(len(e2e)*0.95)]:.1f}ms", flush=True)
        if short_lat and long_lat:
            worst_embed = max(statistics.mean(short_lat), statistics.mean(long_lat))
        else:
            worst_embed = max(short_lat + long_lat)
        worst = worst_embed + statistics.mean(search_lat) if search_lat else worst_embed
        print(f"[最坏路径] embed({worst_embed:.0f}ms) + search({statistics.mean(search_lat):.0f}ms) "
              f"≈ {worst:.0f}ms", flush=True)
        print(f"\n[对比] 之前远程 API ≈ {API_BASELINE_MS:.0f}ms → 现在本地 {worst:.0f}ms "
              f"（{API_BASELINE_MS / max(worst, 0.001):.0f}x 提升）", flush=True)

    provider.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
