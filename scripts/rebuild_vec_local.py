#!/usr/bin/env python3
"""重建向量库：1024 维（BGE-M3 远程 API）→ 512 维（BGE-small-zh-v1.5 本地推理）。

背景（香橙派本地 Embedding，2026-08-07 决策）：
- 向量库从远程 bge-m3（1024 维）切换到本地 BGE-small-zh-v1.5（512 维），
  vec0 虚拟表结构无法原地改维度，必须整库重建并重新向量化。
- 文本源不变（仍取 agent.db 各业务表），仅换 Embedding 模型。

用法：
  python scripts/rebuild_vec_local.py [--model-dir DIR] [--dry-run]

流程：
1. 备份旧向量库 agent_vec.db → agent_vec.db.bak-<ts>
2. 读取旧向量库各表 rowid 集合（孤儿行——源表已删的——跳过并报告）
3. 用本地 Provider 批量向量化（默认批 32）
4. 重建 4 张 vec0 表（512 维）并写入
5. 输出统计（总数/孤儿/失败/耗时）

注意：
- 需在项目 .venv 中运行（依赖 onnxruntime/tokenizers/sqlite_vec）
- 运行前请停止服务，避免旧库被占用 / 新库写入冲突
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
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

BATCH = 32


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


def _resolve_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重建向量库为本地 Embedding（512 维）")
    parser.add_argument("--db", default=None, help="主数据库 agent.db 路径")
    parser.add_argument("--vec-db", default=None, help="向量库路径（默认 <db 同名>_vec.db）")
    parser.add_argument("--model-dir", default=None, help="本地模型目录（含 model.onnx/tokenizer.json）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不重建")
    args = parser.parse_args()

    # 默认路径：优先取应用配置，保证与运行时一致
    try:
        from config import DATA_DIR
        default_db = DATA_DIR / "agent.db"
    except Exception:  # noqa: BLE001
        _data_dir = os.getenv("KIOXIA_DATA_DIR", "") or str(Path.home() / ".ai-agent" / "data")
        default_db = Path(_data_dir) / "db" / "agent.db"

    args.db = Path(args.db) if args.db else default_db
    args.vec_db = Path(args.vec_db) if args.vec_db else args.db.with_name(args.db.stem + "_vec.db")
    if args.model_dir:
        args.model_dir = Path(args.model_dir)
    else:
        env_dir = __import__("os").getenv("LOCAL_EMBED_MODEL_DIR", "")
        args.model_dir = Path(env_dir) if env_dir else args.db.with_name("models/bge-small-zh-v1.5")
    return args


def main() -> int:
    args = _resolve_args()
    if not args.db.exists():
        print(f"[error] 主数据库不存在: {args.db}")
        return 1
    if not args.vec_db.exists():
        print(f"[error] 向量库不存在: {args.vec_db}")
        return 1
    if not (args.model_dir / "model.onnx").exists() and not (args.model_dir / "onnx/model.onnx").exists():
        print(f"[error] 模型目录缺少 model.onnx: {args.model_dir}")
        return 1

    from memory.local_embed import LocalEmbeddingProvider

    print(f"[info] 主库    : {args.db}", flush=True)
    print(f"[info] 向量库  : {args.vec_db}", flush=True)
    print(f"[info] 模型目录: {args.model_dir}", flush=True)

    provider = LocalEmbeddingProvider(args.model_dir, query_prefix="")
    if not provider.load():
        print(f"[error] 本地模型加载失败: {provider._load_error}")
        return 1
    dims = provider.dimensions
    print(f"[info] 本地模型维度: {dims}", flush=True)

    # 1) 读旧库 rowid
    old_rowids = _load_old_rowids(args.vec_db)
    total_old = sum(len(v) for v in old_rowids.values())
    print(f"[info] 旧库 rowid: { {k: len(v) for k, v in old_rowids.items()} } 合计={total_old}", flush=True)

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
    for table in TEXT_SOURCES:
        conn.execute(f"CREATE VIRTUAL TABLE {table} USING vec0(embedding float[{dims}])")
    conn.commit()

    t0 = time.time()
    total_ok = 0
    total_fail = 0
    for table, rows in items.items():
        ok, fail = 0, 0
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            texts = [t for _, t in chunk]
            vecs = provider.encode_batch(texts)
            for (rid, _t), vec in zip(chunk, vecs):
                if not vec:
                    fail += 1
                    continue
                conn.execute(
                    f"INSERT INTO {table}(rowid, embedding) VALUES (?, vec_f32(?))",
                    [rid, json.dumps(vec)],
                )
                ok += 1
            conn.commit()
            if i and i % (BATCH * 100) == 0:
                print(f"  {table}: {i}/{len(rows)}", flush=True)
        total_ok += ok
        total_fail += fail
        print(f"[info] {table}: 写入={ok} 失败={fail}", flush=True)

    conn.close()
    elapsed = time.time() - t0

    # 5) 原子替换
    args.vec_db.unlink()
    new_path.rename(args.vec_db)
    print(f"[done] 重建完成: 写入={total_ok} 失败={total_fail} 耗时={elapsed:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
