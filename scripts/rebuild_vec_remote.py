#!/usr/bin/env python3
"""重建向量库：512 维（BGE-small-zh-v1.5 本地）→ 1024 维（BGE-M3 远程 API）。

背景（2026-08-13 决策）：
- 记忆检索 embedding 从本地 NPU/CPU（bge-small-zh-v1.5，512 维）切回远程
  硅基流动 bge-m3（1024 维），vec0 虚拟表无法原地改维度，必须整库重建。
- 文本源不变（仍取 agent.db 各业务表），仅换 Embedding 模型。

用法：
  python scripts/rebuild_vec_remote.py [--model BAAI/bge-m3] [--dry-run]

流程：
1. 备份旧向量库 agent_vec.db → agent_vec.db.bak-<ts>
2. 读取旧向量库各表 rowid 集合（孤儿行跳过并报告）
3. 用远程 API 批量向量化（默认批 16，并发 8）
4. 重建 4 张 vec0 表（1024 维）并写入
5. 输出统计（总数/孤儿/失败/耗时）

注意：
- API Key 优先级：--api-key > env EMBED_API_KEY > env SILICONFLOW_API_KEY
- 运行前请停止服务（systemctl stop nahida-web）
- 重建后删除 {db_stem}_brute/ numpy 索引缓存目录，运行时自动重建
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# 脚本可独立运行：项目根插入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 四张向量表 → agent.db 文本源 SQL（rowid → 向量化文本）
TEXT_SOURCES = {
    "memories_vec": (
        "SELECT summary FROM episodic_memories WHERE id=?",
        "episodic_memories.summary",
    ),
    "memories_child_vec": (
        "SELECT embed_content FROM memory_child_chunks WHERE id=?",
        "memory_child_chunks.embed_content",
    ),
    "kg_entities_vec": (
        "SELECT name || ': ' || summary FROM kg_entities_v2 WHERE rowid=?",
        "kg_entities_v2(name: summary)",
    ),
    "kg_relations_vec": (
        "SELECT fact FROM kg_relations_v2 WHERE rowid=?",
        "kg_relations_v2.fact",
    ),
}

DIMENSIONS = 1024


def _connect_vec(path: Path) -> sqlite3.Connection:
    """打开向量库并加载 sqlite_vec 扩展。"""
    import sqlite_vec

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _load_old_rowids(vec_path: Path) -> dict[str, list[int]]:
    """从旧向量库读取各表全部 rowid。"""
    rowids: dict[str, list[int]] = {}
    if not vec_path.exists():
        return rowids
    conn = _connect_vec(vec_path)
    try:
        for table in TEXT_SOURCES:
            try:
                rows = conn.execute(f"SELECT rowid FROM {table}").fetchall()
                rowids[table] = [r[0] for r in rows]
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 读取 {table} rowid 失败: {e}", flush=True)
                rowids[table] = []
    finally:
        conn.close()
    return rowids


def _embed_batch(texts: list[str], model: str, base_url: str, api_key: str) -> list[list[float]]:
    """调用 OpenAI 兼容 /embeddings，返回按 index 排序的向量列表（失败项为 None）。"""
    import httpx

    resp = httpx.post(
        base_url.rstrip("/") + "/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "input": texts},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    ordered: list[list[float]] = [None] * len(texts)  # type: ignore[list-item]
    for item in data:
        idx = item["index"]
        if idx < len(ordered):
            ordered[idx] = item["embedding"]
    return ordered


def _resolve_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重建向量库为远程 API Embedding（1024 维）")
    parser.add_argument("--db", default=None, help="主数据库 agent.db 路径")
    parser.add_argument("--vec-db", default=None, help="向量库路径（默认 <db 同名>_vec.db）")
    parser.add_argument("--model", default="BAAI/bge-m3", help="API embedding 模型名")
    parser.add_argument("--base-url", default="https://api.siliconflow.cn/v1", help="OpenAI 兼容 base_url")
    parser.add_argument("--api-key", default="", help="API Key（默认 env EMBED_API_KEY / SILICONFLOW_API_KEY）")
    parser.add_argument("--batch", type=int, default=16, help="每批文本条数")
    parser.add_argument("--concurrency", type=int, default=8, help="并发批次数")
    parser.add_argument("--dry-run", action="store_true", help="只统计不重建")
    args = parser.parse_args()

    try:
        from config import DATA_DIR
        default_db = DATA_DIR / "agent.db"
    except Exception:  # noqa: BLE001
        _data_dir = os.getenv("KIOXIA_DATA_DIR", "") or str(Path.home() / ".ai-agent" / "data")
        default_db = Path(_data_dir) / "db" / "agent.db"

    args.db = Path(args.db) if args.db else default_db
    args.vec_db = Path(args.vec_db) if args.vec_db else args.db.with_name(args.db.stem + "_vec.db")
    args.api_key = args.api_key or os.getenv("EMBED_API_KEY", "") or os.getenv("SILICONFLOW_API_KEY", "")
    return args


def main() -> int:
    args = _resolve_args()
    if not args.db.exists():
        print(f"[error] 主数据库不存在: {args.db}")
        return 1
    if not args.vec_db.exists():
        print(f"[error] 向量库不存在: {args.vec_db}")
        return 1
    if not args.api_key:
        print("[error] 缺少 API Key（--api-key 或 env EMBED_API_KEY / SILICONFLOW_API_KEY）")
        return 1

    print(f"[info] 主库    : {args.db}", flush=True)
    print(f"[info] 向量库  : {args.vec_db}", flush=True)
    print(f"[info] 模型    : {args.model}（{DIMENSIONS} 维）", flush=True)
    print(f"[info] BaseURL : {args.base_url}", flush=True)

    # 1) 读旧库 rowid
    old_rowids = _load_old_rowids(args.vec_db)
    total_old = sum(len(v) for v in old_rowids.values())
    print(f"[info] 旧库 rowid: { {k: len(v) for k, v in old_rowids.items()} } 合计={total_old}", flush=True)
    if total_old == 0:
        print("[error] 旧向量库为空，无需重建")
        return 1

    # 2) 读文本源，过滤孤儿行
    adb = sqlite3.connect(str(args.db))
    items: dict[str, list[tuple[int, str]]] = {}
    orphan_counts: dict[str, int] = {}
    for table, (sql, desc) in TEXT_SOURCES.items():
        found = []
        orphans = 0
        for rid in old_rowids.get(table, []):
            row = adb.execute(sql, [rid]).fetchone()
            if row and row[0] and str(row[0]).strip():
                found.append((rid, str(row[0])))
            else:
                orphans += 1
        items[table] = found
        orphan_counts[table] = orphans
        print(f"[info] {table}: 待向量化={len(found)} 孤儿(跳过)={orphans}", flush=True)
    adb.close()

    if args.dry_run:
        print("[info] dry-run 完成，未写入。", flush=True)
        return 0

    # 3) 备份旧库
    bak = args.vec_db.with_name(args.vec_db.name + f".bak-{int(time.time())}")
    shutil.copy2(args.vec_db, bak)
    print(f"[info] 已备份: {bak}", flush=True)

    # 4) 重建（新文件 → 原子替换）
    new_path = args.vec_db.with_name(args.vec_db.name + ".rebuild")
    if new_path.exists():
        new_path.unlink()
    conn = _connect_vec(new_path)
    # 重建期间关闭 fsync/journal：向量库在 USB 盘上 jbd2 fsync 极慢（实测主线程
    # 卡在 jbd2_log_wait_commit 数分钟），每批 commit 一次 fsync 直接堵死写入。
    # 重建是一次性批量写，旧库已备份（.bak-<ts>），断电丢失可接受。
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA journal_mode=OFF")
    for table in TEXT_SOURCES:
        conn.execute(f"CREATE VIRTUAL TABLE {table} USING vec0(embedding float[{DIMENSIONS}])")
    conn.commit()

    # 5) 并发向量化 + 写入
    t0 = time.time()
    total_ok = 0
    total_fail = 0
    batches: list[tuple[str, list[tuple[int, str]]]] = []
    for table, rows in items.items():
        for i in range(0, len(rows), args.batch):
            batches.append((table, rows[i:i + args.batch]))

    done = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {
            ex.submit(_embed_batch, [t for _, t in chunk], args.model, args.base_url, args.api_key): (table, chunk)
            for table, chunk in batches
        }
        for fut in as_completed(futures):
            table, chunk = futures[fut]
            try:
                vecs = fut.result()
            except Exception as e:  # noqa: BLE001
                print(f"[warn] {table} 批次失败: {e}", flush=True)
                total_fail += len(chunk)
                continue
            ok = 0
            for (rid, _t), vec in zip(chunk, vecs):
                if not vec:
                    total_fail += 1
                    continue
                conn.execute(
                    f"INSERT INTO {table}(rowid, embedding) VALUES (?, vec_f32(?))",
                    [rid, json.dumps(vec)],
                )
                ok += 1
            conn.commit()
            total_ok += ok
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(batches)} 批次，已写入 {total_ok} 条，耗时 {time.time()-t0:.0f}s", flush=True)

    conn.close()
    elapsed = time.time() - t0

    # 6) 原子替换 + 清理 numpy 索引缓存
    args.vec_db.unlink()
    new_path.rename(args.vec_db)
    brute_dir = args.vec_db.with_name(args.vec_db.stem + "_brute")
    if brute_dir.exists():
        shutil.rmtree(brute_dir, ignore_errors=True)
        print(f"[info] 已删除 numpy 索引缓存: {brute_dir}", flush=True)
    print(f"[done] 重建完成: 写入={total_ok} 失败={total_fail} 耗时={elapsed:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
