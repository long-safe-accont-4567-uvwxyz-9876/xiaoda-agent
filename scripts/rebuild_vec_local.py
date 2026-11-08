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
1. 从 agent.db 四张业务表枚举 id 与待向量化文本。空文本在 Python 侧过滤，
   同时打印源表行数 / 待向量化条数 / 被过滤条数（含 id 示例），不再静默丢行
2. 备份旧向量库 agent_vec.db → agent_vec.db.bak-<ts>
3. 探测编码器实际能吃下的批大小：bge-large 的 ONNX 导出要求 batch 维恒为 1，
   多条会 INVALID_ARGUMENT 并被 encode_batch 吞成空列表；只有真能返回 2 条才用
   批 32，否则退化为逐条，探测异常也打印原因后退化
4. 批量向量化写入 .rebuild 新库。编码返回条数与请求不符时整批计失败并打印
   示例 id（不再靠 zip 静默截断成「写入 0 失败 0」）
5. 自检：抽样比对「文本自身向量 vs 库内存量向量」的余弦距离，并检查 KNN top1
   是否命中自身；同文本并列导致的未命中与真实编码错误分开统计
6. 自检通过后原子替换

注意：
- 需在项目 .venv 中运行（依赖 onnxruntime/tokenizers/sqlite_vec）
- 全程只写 <vec_db>.rebuild，自检通过后才原子 rename 覆盖正式库，服务无需停止；
  但 rename 之后仍在运行的进程持有旧 inode，需要重启服务才会打开新库
- .rebuild 里已有的 vec0 表会先读 sqlite_master 比对维度，与当前模型不符时
  直接报错退出（vec0 的 IF NOT EXISTS 不校验维度，硬插会整段抛 sqlite 异常）；
  残留确认无用后手工删除再重跑
- 中途失败不会替换旧库；.rebuild 残留可重跑续写
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

# 脚本可独立运行：项目根插入 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 四张向量表 → agent.db 文本源（全量枚举，id 列 + 待向量化文本）
# 空文本过滤放在 Python 侧：SQL 里 WHERE 掉的行会直接消失，既不打条数也进不了
# 自检抽样，无法和「本来就没有这行」区分开。
TEXT_SOURCES = {
    "memories_vec": (
        "SELECT id, summary FROM episodic_memories",
        "episodic_memories.summary",
    ),
    "memories_child_vec": (
        "SELECT id, embed_content FROM memory_child_chunks",
        "memory_child_chunks.embed_content",
    ),
    "kg_entities_vec": (
        "SELECT rowid, name || ': ' || COALESCE(summary, '') FROM kg_entities_v2",
        "kg_entities_v2(name: summary)",
    ),
    "kg_relations_vec": (
        "SELECT rowid, fact FROM kg_relations_v2",
        "kg_relations_v2.fact",
    ),
}

BATCH = 32          # 向量化批大小（探测不过时退化为 1）
CHECK_SAMPLES = 20  # 每张表自检抽样条数
CHECK_MAX_DIST = 0.01  # 自距离阈值（float32 往返误差量级）
FILTER_SAMPLE = 5   # 被过滤 id 的示例条数


def _connect_vec(path: Path) -> sqlite3.Connection:
    """打开向量库并加载 sqlite_vec 扩展。"""
    import sqlite_vec

    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _load_source_items(
    adb: sqlite3.Connection,
) -> tuple[dict[str, list[tuple[int, str]]], dict[str, dict[str, object]]]:
    """从业务表全量枚举 (id, 文本)，空文本在 Python 侧跳过并计数。"""
    items: dict[str, list[tuple[int, str]]] = {}
    stats: dict[str, dict[str, object]] = {}
    for table, (sql, desc) in TEXT_SOURCES.items():
        rows: list[tuple[int, str]] = []
        total = 0
        filtered = 0
        examples: list[int] = []
        for rid, txt in adb.execute(sql):
            total += 1
            text = str(txt).strip() if txt is not None else ""
            if rid is None or not text:
                filtered += 1
                if rid is not None and len(examples) < FILTER_SAMPLE:
                    examples.append(int(rid))
                continue
            rows.append((int(rid), text))
        items[table] = rows
        stats[table] = {"desc": desc, "total": total, "filtered": filtered, "examples": examples}
        print(
            f"[info] {table}: 源={desc} 源表行数={total}"
            f" 待向量化={len(rows)} 被过滤空文本={filtered}",
            flush=True,
        )
        if examples:
            print(f"[info] {table}: 被过滤 id 示例={examples}", flush=True)
    return items, stats


def _existing_rowids(conn: sqlite3.Connection, table: str) -> set[int]:
    """读取 .rebuild 里已写入的 rowid，用于断点续写。"""
    try:
        return {int(r[0]) for r in conn.execute(f"SELECT rowid FROM {table}")}
    except sqlite3.Error:
        return set()


def _existing_vec_dims(conn: sqlite3.Connection, table: str) -> int | None:
    """读取 .rebuild 里 vec0 表声明的维度；表不存在返回 None。

    CREATE VIRTUAL TABLE IF NOT EXISTS 不会校验已存在表的维度，512 维残留会让
    插入阶段整段抛 sqlite 异常中断，所以先比对再决定是否继续。
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    match = re.search(r"float\s*\[\s*(\d+)\s*\]", str(row[0]), re.IGNORECASE)
    return int(match.group(1)) if match else None


def _probe_batch(provider, dims: int) -> int:
    """探测编码器实际能吃下的批大小。

    只拿 1 条探测永远成功：bge-large 的 ONNX 导出要求 batch 维恒为 1，多条会在
    onnxruntime 里 INVALID_ARGUMENT，再被 encode_batch 的兜底吞成 []。必须用 2 条
    实测，返回 2 条且维度正确才用 BATCH，否则退化为逐条。探测异常同样要打印。
    """
    try:
        vecs = provider.encode_batch(["probe-0", "probe-1"])
    except Exception as exc:  # noqa: BLE001 - 探测失败必须留痕，不能静默退化
        print(f"[warn] 批大小探测异常，退化为 step=1: {exc!r}", flush=True)
        return 1
    good = len(vecs) == 2 and all(v and len(v) == dims for v in vecs)
    if good:
        print(f"[info] 批大小探测: 2 条 → 返回 2 条（维度 {dims}），使用批大小 {BATCH}", flush=True)
        return BATCH
    print(
        f"[warn] 批大小探测: 2 条 → 返回 {len(vecs)} 条"
        f"（维度 {[len(v) for v in vecs] or '空'}，编码器不支持 batch>1），退化为 step=1",
        flush=True,
    )
    return 1


def _self_check(
    conn: sqlite3.Connection,
    provider,
    items: dict[str, list[tuple[int, str]]],
    step: int,
    samples: int = CHECK_SAMPLES,
) -> dict[str, object]:
    """抽样验证：同一文本编码后与库内存量向量的余弦距离应接近 0，且 KNN top1 命中自身。

    top1 未命中按原因拆分：源表里同一文本出现多次时向量完全相同，KNN 只返回其中
    一个 rowid，命中别人属于并列（miss_dup），其余才算编码错误（miss_other）。
    """
    checked = 0
    self_ok = 0
    top1_ok = 0
    worst = 0.0
    miss_dup = 0
    miss_other = 0
    examples: list[str] = []
    aborted: str | None = None

    def _snapshot() -> dict[str, object]:
        return {
            "checked": checked,
            "self_ok": self_ok,
            "top1_ok": top1_ok,
            "worst": worst,
            "miss_dup": miss_dup,
            "miss_other": miss_other,
            "examples": examples,
            "aborted": aborted,
        }

    for table, rows in items.items():
        if not rows:
            continue
        counts = Counter(text for _, text in rows)
        picks = random.sample(rows, min(samples, len(rows)))
        for start in range(0, len(picks), step):
            batch = picks[start:start + step]
            vecs = provider.encode_batch([t for _, t in batch])
            if len(vecs) != len(batch):
                print(
                    f"[check] {table}: 编码返回条数不符 期望={len(batch)}"
                    f" 实际={len(vecs)}，该批样本跳过",
                    flush=True,
                )
            for (rid, text), vec in zip(batch, vecs):
                if not vec:
                    continue
                blob = json.dumps(vec)
                checked += 1
                try:
                    got = conn.execute(
                        f"SELECT vec_distance_cosine(vec_f32(?), embedding) FROM {table}"
                        " WHERE rowid=?",
                        [blob, rid],
                    ).fetchone()
                except sqlite3.Error as exc:
                    print(f"[check] {table} 自距离查询失败: {exc}", flush=True)
                    aborted = f"{table} 自距离查询失败: {exc}"
                    return _snapshot()
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
                    continue
                if counts[text] > 1:
                    miss_dup += 1
                else:
                    miss_other += 1
                if len(examples) < 5:
                    examples.append(f"{table}#{rid}")
    return _snapshot()


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
    model_onnx = args.model_dir / "model.onnx"
    nested_onnx = args.model_dir / "onnx" / "model.onnx"
    if not model_onnx.exists() and not nested_onnx.exists():
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
        items, stats = _load_source_items(adb)
    finally:
        adb.close()
    total = sum(len(v) for v in items.values())
    filtered_total = sum(int(s["filtered"]) for s in stats.values())
    filtered_examples = {
        table: s["examples"] for table, s in stats.items() if s["examples"]
    }
    print(
        f"[info] 合计待向量化: {total} 被过滤空文本: {filtered_total}"
        f" 被过滤 id 示例: {filtered_examples or '无'}",
        flush=True,
    )

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

    # 3) 重建（新文件 → 原子替换），已存在则断点续写；先校验维度
    new_path = args.vec_db.with_name(args.vec_db.name + ".rebuild")
    if new_path.exists():
        probe_conn = _connect_vec(new_path)
        try:
            stale = []
            for table in TEXT_SOURCES:
                got_dims = _existing_vec_dims(probe_conn, table)
                if got_dims is not None and got_dims != dims:
                    stale.append(f"{table}=float[{got_dims}]")
        finally:
            probe_conn.close()
        if stale:
            print(
                f"[error] .rebuild 已存在维度不符的向量表（期望 float[{dims}]）: {', '.join(stale)}",
                flush=True,
            )
            print(f"[error] 残留库不可续写，请确认无用后删除再重跑: rm -f {new_path}", flush=True)
            return 1
        print(f"[info] .rebuild 已存在且维度为 float[{dims}]，按断点续写", flush=True)

    conn = _connect_vec(new_path)
    for table in TEXT_SOURCES:
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(embedding float[{dims}])"
        )
    conn.commit()

    t0 = time.time()
    step = _probe_batch(provider, dims)
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
            if len(vecs) != len(chunk):
                # 编码器返回短列表：zip 会静默截断成「写入 0 失败 0」，整批计失败
                fail += len(chunk)
                print(
                    f"[warn] {table}: 编码返回条数不符 期望={len(chunk)}"
                    f" 实际={len(vecs)} 整批计失败 id示例={[rid for rid, _ in chunk[:3]]}",
                    flush=True,
                )
                conn.commit()
                continue
            for (rid, _text), vec in zip(chunk, vecs):
                if not vec or len(vec) != dims:
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

    if total_fail:
        print(
            f"[error] 有 {total_fail} 条向量化失败（编码返回短列表），"
            ".rebuild 不完整，保留旧库未替换，可重跑续写",
            flush=True,
        )
        return 1

    # 4) 自检
    random.seed(42)
    check = _self_check(conn, provider, items, step)
    conn.close()
    checked = int(check["checked"])
    self_ok = int(check["self_ok"])
    top1_ok = int(check["top1_ok"])
    worst = float(check["worst"])
    print(
        f"[check] 抽样={checked} 自距离通过={self_ok} top1命中自身={top1_ok}"
        f" 最差自距离={worst:.4f} 阈值={CHECK_MAX_DIST}",
        flush=True,
    )
    print(
        f"[check] top1 未命中拆分: 同文本并列={check['miss_dup']}"
        f" 真实编码错误={check['miss_other']} 示例={check['examples']}",
        flush=True,
    )
    print(
        f"[info] 空文本过滤汇总: 被过滤={filtered_total} 条 示例={filtered_examples or '无'}",
        flush=True,
    )

    if check["aborted"]:
        print(f"[error] 自检中断: {check['aborted']}，保留旧库与 .rebuild，未做替换", flush=True)
        return 1
    if checked == 0:
        print("[error] 自检无样本，拒绝替换", flush=True)
        return 1
    if self_ok < checked or top1_ok < checked:
        print(
            f"[error] 自检未通过（自距离 {self_ok}/{checked}，top1 {top1_ok}/{checked}），"
            "保留旧库与 .rebuild，未做替换",
            flush=True,
        )
        return 1

    # 5) 原子替换
    if args.vec_db.exists():
        args.vec_db.unlink()
    new_path.rename(args.vec_db)
    print(
        f"[done] 重建完成: 写入={total_ok} 失败={total_fail} 断点跳过={total_skip}"
        f" 被过滤={filtered_total} 耗时={elapsed:.1f}s",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
