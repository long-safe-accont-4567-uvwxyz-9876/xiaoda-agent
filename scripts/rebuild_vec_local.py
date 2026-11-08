#!/usr/bin/env python3
"""重建向量库：按源表全量重新向量化（本地 bge-large-zh-v1.5，1024 维）。

背景：
- 历史上这个库有一批向量是远程 BGE-M3（1024 维）时代写入的，维度相同但模型
  空间不同。用本地 bge-large-zh-v1.5 查询时，同一段文本对自身的余弦距离接近 1，
  召回排序等于噪声，而且不报错。
- 旧版本按「旧向量库里已有 rowid」过滤，空库永远补不出数据（待向量化恒为 0），
  无法用于重建。现在改为直接从 agent.db 各业务表枚举 id。

用法：
  python scripts/rebuild_vec_local.py [--model-dir DIR] [--dry-run]

流程：
1. 从 agent.db 四张业务表枚举 id 与待向量化文本
2. 备份旧向量库 agent_vec.db → agent_vec.db.bak-<ts>
3. 用本地 Provider 批量向量化（默认批 32）写入 .rebuild 新库
4. 自检：抽样比对「文本自身向量 vs 库内存量向量」的余弦距离，并检查 KNN top1
   是否命中自身，打印通过/失败条数
5. 自检通过后原子替换

注意：
- 需在项目 .venv 中运行（依赖 onnxruntime/tokenizers/sqlite_vec）
- 运行前请停止服务，避免旧库被占用 / 新库写入冲突
- 中途失败不会替换旧库；.rebuild 残留可重跑续写，确认无用后再手工清理
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sqlite3
import sys
import time
from pathlib import Path

# 脚本可独立运行：项目根插入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 四张向量表 → agent.db 文本源（全量枚举，id 列 + 待向量化文本）
TEXT_SOURCES = {
    "memories_vec": (
        "SELECT id, summary FROM episodic_memories"
        " WHERE summary IS NOT NULL AND TRIM(summary) <> ''",
        "episodic_memories.summary",
    ),
    "memories_child_vec": (
        "SELECT id, embed_content FROM memory_child_chunks"
        " WHERE embed_content IS NOT NULL AND TRIM(embed_content) <> ''",
        "memory_child_chunks.embed_content",
    ),
    "kg_entities_vec": (
        "SELECT rowid, name || ': ' || COALESCE(summary, '') FROM kg_entities_v2",
        "kg_entities_v2(name: summary)",
    ),
    "kg_relations_vec": (
        "SELECT rowid, fact FROM kg_relations_v2"
        " WHERE fact IS NOT NULL AND TRIM(fact) <> ''",
        "kg_relations_v2.fact",
    ),
}

BATCH = 32          # 向量化批大小
CHECK_SAMPLES = 20  # 每张表自检抽样条数
CHECK_MAX_DIST = 0.01  # 自距离阈值（float32 往返误差量级）


def _connect_vec(path: Path) -> sqlite3.Connection:
    """打开向量库并加载 sqlite_vec 扩展。"""
    import sqlite_vec

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _load_source_items(adb: sqlite3.Connection) -> dict[str, list[tuple[int, str]]]:
    """从业务表全量枚举 (id, 文本)，跳过空文本行。"""
    items: dict[str, list[tuple[int, str]]] = {}
    for table, (sql, desc) in TEXT_SOURCES.items():
        rows: list[tuple[int, str]] = []
        for rid, txt in adb.execute(sql):
            if rid is None or txt is None:
                continue
            text = str(txt).strip()
            if not text:
                continue
            rows.append((int(rid), text))
        items[table] = rows
        print(f"[info] {table}: 源={desc} 待向量化={len(rows)}", flush=True)
    return items


def _existing_rowids(conn: sqlite3.Connection, table: str) -> set[int]:
    """读取 .rebuild 里已写入的 rowid，用于断点续写。"""
    try:
        return {int(r[0]) for r in conn.execute(f"SELECT rowid FROM {table}")}
    except sqlite3.Error:
        return set()


def _self_check(
    conn: sqlite3.Connection,
    provider,
    items: dict[str, list[tuple[int, str]]],
    samples: int = CHECK_SAMPLES,
) -> tuple[int, int, int, float]:
    """抽样验证：同一文本编码后与库内存量向量的余弦距离应接近 0，且 KNN top1 命中自身。"""
    checked = 0
    self_ok = 0
    top1_ok = 0
    worst = 0.0
    for table, rows in items.items():
        if not rows:
            continue
        picks = random.sample(rows, min(samples, len(rows)))
        vecs = provider.encode_batch([t for _, t in picks])
        for (rid, _text), vec in zip(picks, vecs):
            if not vec:
                continue
            blob = json.dumps(vec)
            checked += 1
            try:
                got = conn.execute(
                    f"SELECT vec_distance_cosine(vec_f32(?), embedding) FROM {table} WHERE rowid=?",
                    [blob, rid],
                ).fetchone()
            except sqlite3.Error as exc:
                print(f"[check] {table} 自距离查询失败: {exc}", flush=True)
                return checked, 0, 0, 1.0
            dist = float(got[0]) if got and got[0] is not None else 1.0
            worst = max(worst, dist)
            if dist < CHECK_MAX_DIST:
                self_ok += 1
            try:
                hits = conn.execute(
                    f"SELECT rowid FROM {table} WHERE embedding MATCH vec_f32(?) AND k = 1",
                    [blob],
                ).fetchall()
            except sqlite3.Error:
                hits = []
            if hits and int(hits[0][0]) == rid:
                top1_ok += 1
    return checked, self_ok, top1_ok, worst


def _resolve_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重建向量库（本地 bge-large-zh-v1.5，1024 维）")
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
        env_dir = os.getenv("LOCAL_EMBED_MODEL_DIR", "")
        args.model_dir = (
            Path(env_dir) if env_dir
            else Path("/mnt/kioxia/nahida-data/models/bge-large-zh-v1.5")
        )
    return args


def main() -> int:
    args = _resolve_args()
    if not args.db.exists():
        print(f"[error] 主数据库不存在: {args.db}")
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

    # 1) 源表枚举（源表驱动：不再依赖旧库 rowid）
    adb = sqlite3.connect(str(args.db))
    try:
        items = _load_source_items(adb)
    finally:
        adb.close()
    total = sum(len(v) for v in items.values())
    print(f"[info] 合计待向量化: {total}", flush=True)

    if args.dry_run:
        print("[info] dry-run 完成，未写入。", flush=True)
        return 0

    # 2) 备份旧库
    if args.vec_db.exists():
        bak = args.vec_db.with_name(args.vec_db.name + f".bak-{int(time.time())}")
        shutil.copy2(args.vec_db, bak)
        print(f"[info] 已备份: {bak} ({bak.stat().st_size} 字节)", flush=True)
    else:
        print("[info] 旧向量库不存在，跳过备份", flush=True)

    # 3) 重建（新文件 → 原子替换），已存在则断点续写
    new_path = args.vec_db.with_name(args.vec_db.name + ".rebuild")
    conn = _connect_vec(new_path)
    for table in TEXT_SOURCES:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(embedding float[{dims}])"
        )
    conn.commit()

    t0 = time.time()
    step = BATCH if provider.encode_batch(["probe"]) else 1
    print(f"[info] 批大小: {step}", flush=True)
    total_ok = 0
    total_fail = 0
    total_skip = 0
    for table, rows in items.items():
        done = _existing_rowids(conn, table)
        pending = [r for r in rows if r[0] not in done]
        if done:
            print(f"[info] {table}: 断点续写 已有={len(done)} 待写={len(pending)}", flush=True)
        ok, fail = 0, 0
        for i in range(0, len(pending), step):
            chunk = pending[i:i + step]
            vecs = provider.encode_batch([t for _, t in chunk])
            for (rid, _text), vec in zip(chunk, vecs):
                if not vec:
                    fail += 1
                    continue
                conn.execute(
                    f"INSERT INTO {table}(rowid, embedding) VALUES (?, vec_f32(?))",
                    [rid, json.dumps(vec)],
                )
                ok += 1
            conn.commit()
            if i and i % (step * 100) == 0:
                print(f"  {table}: {i}/{len(pending)}", flush=True)
        total_ok += ok
        total_fail += fail
        total_skip += len(done)
        print(f"[info] {table}: 写入={ok} 失败={fail} 断点跳过={len(done)}", flush=True)

    elapsed = time.time() - t0

    # 4) 自检
    random.seed(42)
    checked, self_ok, top1_ok, worst = _self_check(conn, provider, items)
    conn.close()
    print(
        f"[check] 抽样={checked} 自距离通过={self_ok} top1命中自身={top1_ok}"
        f" 最差自距离={worst:.4f} 阈值={CHECK_MAX_DIST}",
        flush=True,
    )

    if checked == 0:
        print("[error] 自检无样本，拒绝替换", flush=True)
        return 1
    if self_ok < checked or top1_ok < checked:
        print("[error] 自检未通过，保留旧库与 .rebuild，未做替换", flush=True)
        return 1

    # 5) 原子替换
    if args.vec_db.exists():
        args.vec_db.unlink()
    new_path.rename(args.vec_db)
    print(
        f"[done] 重建完成: 写入={total_ok} 失败={total_fail} 断点跳过={total_skip}"
        f" 耗时={elapsed:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
