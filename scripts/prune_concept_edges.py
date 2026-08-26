#!/usr/bin/env python3
"""concept_edges 剪枝维护脚本。

背景（2026-08-27 扫描结论）：KeyExtractor 的通用停用词表未覆盖会话角色词
（"爸爸" DF≈77%、"小妲"≈62%、"用户"≈59%、"回应/回复"…），导致几乎任意
两个 concept_nodes 都能凑够"共享≥3 keys"而被 auto_link/curator 双向互连，
2463 节点膨胀出 100 万条权重全为 1.0 的 co-occurrence 边（近稠密图），
占库体积约 3/4，get_edge_snapshot 全表拉取读放大数秒级。

策略：按存活节点 keys 的文档频率（DF）过滤高扩散 key 后重算
"共享 ≥ min_shared 强 key"的保留边集，其余删除。与 db_concept 中新增的
DF 过滤建边逻辑保持同一阈值，剪枝后新边不再回弹为稠密图。

用法：
    python3 scripts/prune_concept_edges.py            # dry-run，只打印统计
    python3 scripts/prune_concept_edges.py --apply    # 分批删除
建议在 --apply 前执行 scripts/db_backup.sh 或确认当日备份可用。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter

# 与 db_concept.ConceptDB.MAX_KEY_DF_RATIO 保持一致
DEFAULT_MAX_KEY_DF_RATIO = 0.05
DEFAULT_MIN_SHARED = 3
BATCH_SIZE = 20_000


def load_alive_key_maps(conn: sqlite3.Connection) -> dict[str, set[str]]:
    cur = conn.execute("SELECT id, keys FROM concept_nodes WHERE valid_to IS NULL")
    return {
        row[0]: set(json.loads(row[1] or "[]"))
        for row in cur.fetchall()
    }


def build_keep_set(key_maps: dict[str, set[str]], max_df_ratio: float,
                   min_shared: int) -> tuple[set[tuple[str, str]], set[str]]:
    """返回保留的有向边集合与被过滤的高频 key 集合。"""
    df: Counter[str] = Counter()
    for ks in key_maps.values():
        for k in ks:
            df[k] += 1
    n = max(1, len(key_maps))
    stopkeys = {k for k, v in df.items() if v / n > max_df_ratio}
    strong = {nid: ks - stopkeys for nid, ks in key_maps.items()}

    ids = sorted(strong)
    keep: set[tuple[str, str]] = set()
    for i, a in enumerate(ids):
        sa = strong[a]
        if not sa:
            continue
        for b in ids[i + 1:]:
            if len(sa & strong[b]) >= min_shared:
                keep.add((a, b))
                keep.add((b, a))
    return keep, stopkeys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="/mnt/usb2/nahida-data/db/agent.db")
    parser.add_argument("--apply", action="store_true", help="执行删除（默认 dry-run）")
    parser.add_argument("--max-key-df-ratio", type=float, default=DEFAULT_MAX_KEY_DF_RATIO)
    parser.add_argument("--min-shared", type=int, default=DEFAULT_MIN_SHARED)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=rw", uri=True, timeout=30)
    conn.row_factory = None
    t0 = time.time()
    key_maps = load_alive_key_maps(conn)
    print(f"[load] alive_nodes={len(key_maps)} ({time.time() - t0:.1f}s)")

    keep, stopkeys = build_keep_set(key_maps, args.max_key_df_ratio, args.min_shared)
    print(f"[plan] stopkeys={len(stopkeys)} sample={sorted(stopkeys)[:8]}")
    print(f"[plan] keep_directed_edges={len(keep)} "
          f"(df_ratio<={args.max_key_df_ratio}, min_shared>={args.min_shared})")

    total = conn.execute("SELECT COUNT(*) FROM concept_edges").fetchone()[0]
    todo: list[tuple[str, str]] = []
    for source_id, target_id in conn.execute(
            "SELECT source_id, target_id FROM concept_edges"):
        if (source_id, target_id) not in keep:
            todo.append((source_id, target_id))
    print(f"[plan] total={total} keep={total - len(todo)} delete={len(todo)}")

    if not args.apply:
        print("[dry-run] 未修改数据库。加 --apply 执行删除。")
        conn.close()
        return 0
    if not todo:
        print("[apply] 无需删除。")
        conn.close()
        return 0

    deleted = 0
    t1 = time.time()
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(
            "DELETE FROM concept_edges WHERE source_id = ? AND target_id = ?",
            batch,
        )
        conn.commit()
        deleted += len(batch)
        rate = deleted / max(time.time() - t1, 0.001)
        print(f"[delete] {deleted}/{len(todo)} ({rate:.0f} rows/s)", end="\r")
    print(f"\n[delete] done {deleted} rows in {time.time() - t1:.1f}s")

    remain = conn.execute("SELECT COUNT(*) FROM concept_edges").fetchone()[0]
    print(f"[verify] remaining_edges={remain} (expect {total - deleted})")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
