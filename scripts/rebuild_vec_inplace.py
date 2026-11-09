#!/usr/bin/env python3
"""不停机原地增量重建向量库（本地 bge-large-zh-v1.5，1024 维）。

为什么不用 scripts/rebuild_vec_local.py：那个脚本走「新建 .rebuild 库 + 原子替换」，
必须停服。本机实测编码吞吐约 2 秒/条，全量 15009 条需要 8 小时以上，停服窗口
不可接受。四张 vec0 表维度同为 1024，源表 id 与 vec 表 rowid 一一对应，因此可以
直接对现有 agent_vec.db 做 INSERT OR REPLACE 原地覆盖，服务不中断。

进度位点：agent_vec.db 内的普通表 vec_rebuild_progress(vec_table, source_id, done_at)，
每批提交后落盘；中断后重跑自动跳过已完成行，可停可续。

优先级：先补「源表有、vec 表没有」的缺口行（当前 89 + 215），再按 id 升序给存量
行换空间。

用法：
  python scripts/rebuild_vec_inplace.py --status
  python scripts/rebuild_vec_inplace.py --time-budget 900
  python scripts/rebuild_vec_inplace.py --table memories_vec --limit 200
  python scripts/rebuild_vec_inplace.py --check

注意：
- 不建新库、不替换文件、不需要停服；写锁争用由 busy_timeout 兜底。
- 自检只对「已重建」的行抽样，未重建的老行仍停在旧模型空间。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

import sqlite_vec

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from local_ai.integration.embedding import LocalEmbeddingService  # noqa: E402

# 四张向量表 → agent.db 文本源（全量枚举，id 列 + 待向量化文本）
TABLES = {
    "memories_vec": (
        "SELECT id, summary FROM episodic_memories"
        " WHERE summary IS NOT NULL AND TRIM(summary) <> ''"
    ),
    "memories_child_vec": (
        "SELECT id, embed_content FROM memory_child_chunks"
        " WHERE embed_content IS NOT NULL AND TRIM(embed_content) <> ''"
    ),
    "kg_entities_vec": (
        "SELECT rowid, name || ': ' || COALESCE(summary, '') FROM kg_entities_v2"
    ),
    "kg_relations_vec": (
        "SELECT rowid, fact FROM kg_relations_v2"
        " WHERE fact IS NOT NULL AND TRIM(fact) <> ''"
    ),
}

BATCH = 8               # 与 AdaptiveEmbeddingProvider 内部拆批一致
CHECK_SAMPLES = 20      # 每张表自检抽样条数
CHECK_MAX_DIST = 0.01   # 自距离阈值（float32 往返误差量级）
CHECK_TIE_K = 8         # 自检取 top-k 判断是否存在等距并列
CHECK_TIE_EPS = 1e-6    # 等距并列的余弦距离误差（同文本重复子块距离相同）
YIELD_S = 0.05          # 批间让出，避免长时间独占写锁
# 2026-11-08 选定的热区目标就是 8000（memories_child_vec 保留最新 5424 条，其余下沉归档）。
# 缺省触发水位必须 >= 该目标：一旦低于当前热区，任何不带 --no-auto-compress 的运行都会
# 先把热表压到 --hot-low（默认 4000），被删子块此后只能靠 FTS 召回，属不可逆下沉。
HOT_TARGET_ROWS = 8000
DEFAULT_MAX_ROWS = HOT_TARGET_ROWS  # 索引总行数上限（自动压缩目标 + 单次运行触发上限）
# 压缩策略：小表与主记忆表受保护，预算优先给 memories_child_vec，
# 超出的部分按 rowid 从旧到新删除（新数据可检索，旧数据退化为 FTS 关键词）。
PROTECTED_TABLES = ("kg_entities_vec", "kg_relations_vec", "memories_vec")

ARCHIVE_DDL = """
CREATE TABLE IF NOT EXISTS vec_archive (
    vec_table   TEXT NOT NULL,
    source_id   INTEGER NOT NULL,
    embedding   BLOB NOT NULL,
    archived_at TEXT NOT NULL,
    PRIMARY KEY (vec_table, source_id)
)
"""


def _hot_total(conn: sqlite3.Connection) -> int:
    return sum(int(conn.execute(f"SELECT COUNT(1) FROM {t}").fetchone()[0]) for t in TABLES)


def _default_watermark_guard(hot_total: int, max_rows: int, *, explicit: bool) -> str | None:
    """护栏：缺省触发水位低于当前热区时明确告警。

    自动压缩会在任何不带 --no-auto-compress 的运行前先执行，一旦触发就把热表压到
    --hot-low（默认 4000）；被删子块此后只能靠 FTS 关键词召回，补回要重编码约 4 小时。
    缺省水位必须 >= 选定目标（HOT_TARGET_ROWS），这条告警防止以后有人把默认值调低却
    无人察觉；用户显式传 --max-rows 时视为已知代价，不再告警。
    """
    if explicit or hot_total <= max_rows:
        return None
    return (
        f"[guard] 缺省触发水位 {max_rows} 低于当前热区 {hot_total} 条："
        f"这次运行会先把热区压到 --hot-low（默认 4000），旧子块此后只能靠 FTS 召回。"
        "要保留当前热区请显式加 --no-auto-compress；要自定义目标请显式传 --max-rows/--hot-low。"
    )


def _cosine_dist(conn: sqlite3.Connection, table: str, blob: bytes, rowid: int) -> float:
    """查询某行与查询向量的余弦距离；行不存在时返回 1.0（最差）。"""
    row = conn.execute(
        f"SELECT vec_distance_cosine(?, embedding) FROM {table} WHERE rowid=?", [blob, rowid]
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else 1.0


def _tie_pass(*, query_text: str, top1_text: str | None, top1_dist: float,
               self_dist: float) -> bool:
    """等距并列判定：源文本逐字相同，且两者余弦距离在并列误差内。"""
    return (
        top1_text is not None
        and top1_text == query_text
        and abs(top1_dist - self_dist) <= CHECK_TIE_EPS
    )

PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS vec_rebuild_progress (
    vec_table TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    done_at   TEXT NOT NULL,
    PRIMARY KEY (vec_table, source_id)
)
"""


def _data_dir() -> Path:
    return Path(os.getenv("KIOXIA_DATA_DIR", "") or "/mnt/kioxia/nahida-data")


def _open(path: Path, *, load_vec: bool = False, timeout: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=timeout, check_same_thread=False)
    if load_vec:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


_LOOP = asyncio.new_event_loop()


def _embed(svc, texts: list[str]) -> list[list[float]]:
    """LocalEmbeddingService.embed 是 async 的，脚本内用常驻事件循环同步化。"""
    return _LOOP.run_until_complete(svc.embed(texts))


def _embed_one(svc, text: str) -> list[float]:
    out = _embed(svc, [text])
    return out[0] if out else []


def _source_items(adb: sqlite3.Connection, table: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for rid, txt in adb.execute(TABLES[table]):
        if rid is None or txt is None:
            continue
        text = str(txt).strip()
        if text:
            out.append((int(rid), text))
    return out


def _vec_rowids(conn: sqlite3.Connection, table: str) -> set[int]:
    try:
        return {int(r[0]) for r in conn.execute(f"SELECT rowid FROM {table}")}
    except sqlite3.Error:
        return set()


def _done_ids(conn: sqlite3.Connection, table: str) -> set[int]:
    try:
        return {
            int(r[0])
            for r in conn.execute(
                "SELECT source_id FROM vec_rebuild_progress WHERE vec_table=?", [table]
            )
        }
    except sqlite3.Error:
        return set()


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(1) FROM {table}").fetchone()[0])


def _plan_compress(conn: sqlite3.Connection, max_rows: int) -> list[tuple[str, int, list[int]]]:
    """计算压缩方案：[(表名, 保留条数, 待删除 rowid 列表)]。"""
    counts = {t: _count(conn, t) for t in TABLES}
    total = sum(counts.values())
    if total <= max_rows:
        return []
    protected = sum(counts[t] for t in PROTECTED_TABLES)
    budget = max_rows - protected
    if budget < 0:
        raise SystemExit(
            f"[error] 受保护表（{'/'.join(PROTECTED_TABLES)}）合计 {protected} 条"
            f"已超上限 {max_rows}，拒绝压缩"
        )
    plan: list[tuple[str, int, list[int]]] = []
    for table in TABLES:
        if table in PROTECTED_TABLES:
            continue
        keep = max(0, min(counts[table], budget))
        budget -= keep
        drop = counts[table] - keep
        if drop <= 0:
            continue
        rows = [
            int(r[0])
            for r in conn.execute(
                f"SELECT rowid FROM {table} ORDER BY rowid DESC LIMIT -1 OFFSET ?", [keep]
            )
        ]
        plan.append((table, keep, rows))
    return plan


def _compress(conn: sqlite3.Connection, max_rows: int, *, apply: bool) -> int:
    """把 vec 表压到 max_rows 以内：保留 rowid 最新的行，旧的删掉并同步清理进度位点。"""
    plan = _plan_compress(conn, max_rows)
    if not plan:
        print(f"[compress] 总量未超上限 {max_rows}，无需压缩", flush=True)
        return 0
    total_drop = sum(len(rows) for _, _, rows in plan)
    for table, keep, rows in plan:
        print(f"[compress] {table}: 保留最新 {keep} 条，删除 {len(rows)} 条", flush=True)
    if not apply:
        print(f"[compress] 预演：共将删除 {total_drop} 条（正式执行需 --apply）", flush=True)
        return 0
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for table, _keep, rows in plan:
        for i in range(0, len(rows), 500):
            chunk = rows[i : i + 500]
            # 先原样搬到归档表（含 float32 向量），再从热表删除；恢复时不用重新编码。
            conn.executemany(
                "INSERT OR REPLACE INTO vec_archive(vec_table, source_id, embedding, archived_at)"
                f" SELECT ?, rowid, embedding, ? FROM {table} WHERE rowid = ?",
                [(table, now, r) for r in chunk],
            )
            conn.executemany(f"DELETE FROM {table} WHERE rowid = ?", [(r,) for r in chunk])
            conn.executemany(
                "DELETE FROM vec_rebuild_progress WHERE vec_table = ? AND source_id = ?",
                [(table, r) for r in chunk],
            )
            conn.commit()
            time.sleep(0.02)
    now_total = sum(_count(conn, t) for t in TABLES)
    print(f"[compress] 已删除 {total_drop} 条，四表总量现为 {now_total} 条", flush=True)
    return total_drop


def _report(conn: sqlite3.Connection, adb: sqlite3.Connection, tables: list[str]) -> None:
    print(f"{'表':<22}{'源':>8}{'vec':>8}{'已重建':>8}{'待重建':>8}{'缺口':>8}", flush=True)
    for table in tables:
        src = {i for i, _ in _source_items(adb, table)}
        vec = _vec_rowids(conn, table)
        done = _done_ids(conn, table)
        print(
            f"{table:<22}{len(src):>8}{len(vec):>8}"
            f"{len(done & src):>8}{len(src - done):>8}{len(src - vec):>8}",
            flush=True,
        )


def _check(conn: sqlite3.Connection, adb: sqlite3.Connection, svc, tables: list[str]) -> int:
    """抽样验证：同文本重编码与库内向量余弦距离应接近 0，且 KNN top1 命中自身。

    等距并列口径（2026-11-09 修正）：源表存在同文本重复子块——memories_child_vec
    12433 行去重后仅 7847 条文本，「实体: 小妲」一组就占 885 行——同文本必然同向量，
    距离完全相等时 vec0 返回哪一行属于实现细节。仅当 top1 与自身的余弦距离差在
    CHECK_TIE_EPS 内、且 top1 的源文本与查询文本逐字相同时才判通过；文本近似但
    不相同的行仍按严格 top1 失败处理，不放宽成「任意近似都算过」。
    """
    random.seed(42)
    bad = 0
    for table in tables:
        done = sorted(_done_ids(conn, table))
        if not done:
            print(f"[check] {table}: 无已重建行，跳过", flush=True)
            continue
        text_map = dict(_source_items(adb, table))
        picks = [i for i in random.sample(done, min(CHECK_SAMPLES, len(done))) if i in text_map]
        if not picks:
            print(f"[check] {table}: 已重建行在源表已不存在，跳过", flush=True)
            continue
        vecs = _embed(svc, [text_map[i] for i in picks])
        ok_dist = ok_top1 = tied_ok = checked = 0
        worst = 0.0
        for rid, vec in zip(picks, vecs):
            if not vec:
                continue
            blob = sqlite_vec.serialize_float32(vec)
            checked += 1
            dist = _cosine_dist(conn, table, blob, rid)
            worst = max(worst, dist)
            if dist < CHECK_MAX_DIST:
                ok_dist += 1
            hits = conn.execute(
                f"SELECT rowid FROM {table} WHERE embedding MATCH ? AND k=?",
                [blob, CHECK_TIE_K],
            ).fetchall()
            if not hits:
                continue
            top1 = int(hits[0][0])
            if top1 == rid:
                ok_top1 += 1
                continue
            # MATCH 的 distance 列走表自身度量（本库 vec0 是默认 L2），与自距离用的
            # 余弦不可直接相减，这里对 top1 重新按余弦算一次再判并列。
            if _tie_pass(
                query_text=text_map[rid],
                top1_text=text_map.get(top1),
                top1_dist=_cosine_dist(conn, table, blob, top1),
                self_dist=dist,
            ):
                tied_ok += 1
        verdict = (
            "PASS"
            if checked and ok_dist == checked and ok_top1 + tied_ok == checked
            else "FAIL"
        )
        if verdict == "FAIL":
            bad += 1
        print(
            f"[check] {table}: 抽样={checked} 自距离通过={ok_dist} top1命中={ok_top1}"
            f" 等距并列={tied_ok} 最差自距离={worst:.4f} 阈值={CHECK_MAX_DIST} {verdict}",
            flush=True,
        )
    return 0 if bad == 0 else 2


def _parse() -> argparse.Namespace:
    d = _data_dir()
    p = argparse.ArgumentParser(description="不停机原地增量重建向量库（bge-large-zh-v1.5，1024 维）")
    p.add_argument("--db", default=str(d / "db" / "agent.db"))
    p.add_argument("--vec-db", default=str(d / "db" / "agent_vec.db"))
    p.add_argument("--model-dir", default=str(d / "models" / "bge-large-zh-v1.5"))
    p.add_argument("--table", default=None, choices=sorted(TABLES), help="只处理单张表")
    # default=None 用来区分「用户显式传了水位」与「走缺省值」：护栏只对缺省值告警。
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help=f"本次运行处理行数上限（触发上限，缺省 {DEFAULT_MAX_ROWS}）",
    )
    p.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help=f"热区触发水位，超过即自动压缩（缺省 {DEFAULT_MAX_ROWS}）",
    )
    p.add_argument("--hot-low", type=int, default=4000, help="压缩目标条数")
    p.add_argument("--no-auto-compress", action="store_true", help="跳开跑前的自动压缩")
    p.add_argument("--compress", action="store_true", help="只做压缩（配合 --apply 才真正删除）")
    p.add_argument("--apply", action="store_true", help="配合 --compress 真正执行删除")
    p.add_argument("--gaps-only", action="store_true", help="只补源表有、vec 表没有的缺口行")
    p.add_argument("--time-budget", type=float, default=0.0, help="本次最多运行秒数（0=不限）")
    p.add_argument("--status", action="store_true", help="只打印进度")
    p.add_argument("--check", action="store_true", help="只做自检")
    return p.parse_args()


def main() -> int:
    args = _parse()
    # 缺省水位：--limit / --max-rows 未显式传时统一取 DEFAULT_MAX_ROWS（=HOT_TARGET_ROWS）。
    explicit_max_rows = args.max_rows is not None
    if args.max_rows is None:
        args.max_rows = DEFAULT_MAX_ROWS
    if args.limit is None:
        args.limit = DEFAULT_MAX_ROWS
    db, vec_db, model_dir = Path(args.db), Path(args.vec_db), Path(args.model_dir)
    for path in (db, vec_db):
        if not path.exists():
            print(f"[error] 数据库不存在: {path}")
            return 1

    adb = _open(db)
    conn = _open(vec_db, load_vec=True)
    conn.execute(PROGRESS_DDL)
    conn.execute(ARCHIVE_DDL)
    conn.commit()
    tables = [args.table] if args.table else list(TABLES)

    if args.status:
        _report(conn, adb, tables)
        return 0

    if args.compress:
        _compress(conn, args.hot_low, apply=args.apply)
        _report(conn, adb, tables)
        return 0

    # 自动压缩：每次开跑前先把索引压到上限，保证单次运行不超过触发上限。
    if not args.no_auto_compress:
        hot = _hot_total(conn)
        guard = _default_watermark_guard(hot, args.max_rows, explicit=explicit_max_rows)
        if guard:
            print(guard, flush=True)
        if hot > args.max_rows:
            print(f"[compact] 热区 {hot} 条超触发水位 {args.max_rows}，压缩到 {args.hot_low}", flush=True)
            _compress(conn, args.hot_low, apply=True)
        else:
            print(f"[compact] 热区 {hot} 条未超触发水位 {args.max_rows}，跳过", flush=True)

    svc = LocalEmbeddingService.bundled(model_dir)
    if not svc.load():
        print(f"[error] 本地模型加载失败: {model_dir}")
        return 1
    print(
        f"[info] 模型就绪 dir={model_dir} dims={getattr(svc, 'dimensions', '?')}"
        f" backend={getattr(svc, 'source', '?')}",
        flush=True,
    )

    if args.check:
        return _check(conn, adb, svc, tables)

    t_start = time.time()
    total_ok = 0
    stopped = False
    for table in tables:
        items = _source_items(adb, table)
        text_map = dict(items)
        vec = _vec_rowids(conn, table)
        done = _done_ids(conn, table)
        gap = [i for i, _ in items if i not in vec]
        rest = [i for i, _ in items if i in vec and i not in done]
        order = gap if args.gaps_only else gap + rest
        if args.limit:
            order = order[: max(0, args.limit - total_ok)]
        print(
            f"[plan] {table}: 缺口={len(gap)} 存量待换={len(rest)} 本次计划={len(order)}",
            flush=True,
        )
        ok = 0
        for pos in range(0, len(order), BATCH):
            if args.time_budget and time.time() - t_start > args.time_budget:
                stopped = True
                break
            batch = order[pos:pos + BATCH]
            texts = [text_map[i] for i in batch]
            try:
                vecs = _embed(svc, texts)
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] encode_batch 异常，退化为逐条: {exc}", flush=True)
                vecs = []
            if len(vecs) != len(batch):
                vecs = [_embed_one(svc, t) for t in texts]
            rows = [(i, sqlite_vec.serialize_float32(v)) for i, v in zip(batch, vecs) if v]
            if not rows:
                print(f"[warn] {table} 批次 offset={pos} 全部编码失败，跳过", flush=True)
                continue
            # vec0 虚拟表不支持 INSERT OR REPLACE（冲突解析未实现），
            # 同事务内先删后插；失败一起回滚，不会留下空洞。
            conn.executemany(f"DELETE FROM {table} WHERE rowid = ?", [(i,) for i, _ in rows])
            conn.executemany(
                f"INSERT INTO {table}(rowid, embedding) VALUES (?, vec_f32(?))", rows
            )
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            conn.executemany(
                "INSERT OR REPLACE INTO vec_rebuild_progress(vec_table, source_id, done_at)"
                " VALUES (?, ?, ?)",
                [(table, i, now) for i, _ in rows],
            )
            conn.commit()
            ok += len(rows)
            total_ok += len(rows)
            time.sleep(YIELD_S)
            if ok % (BATCH * 10) == 0:
                rate = ok / max(1e-6, time.time() - t_start)
                print(f"  {table}: +{ok} 速度={rate:.2f} 条/秒", flush=True)
        print(f"[done] {table}: 本次写入={ok}", flush=True)
        if stopped:
            print(f"[stop] 到达时间预算 {args.time_budget}s，停在 {table}", flush=True)
            break

    elapsed = time.time() - t_start
    rate = total_ok / elapsed if elapsed > 0 else 0
    print(f"[summary] 本次写入={total_ok} 耗时={elapsed:.1f}s 吞吐={rate:.3f} 条/秒", flush=True)
    _report(conn, adb, tables)
    return 0


if __name__ == "__main__":
    sys.exit(main())
