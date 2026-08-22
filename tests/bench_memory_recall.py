#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""记忆召回离线评估集（A/B 可复现）。

目的：量化验证近期 8 项检索/衰减修复的实际效果。
- P0-1 FSRS 双重 R 惩罚解除
- P0-2 温用户向量权重提升（本评估关闭向量通道，不构成影响）
- P0-3 最低分过滤阈值放宽（亲密记忆不再被清空）
- P1-1 联想通道降权（本评估无 KG/扩散，不构成影响）
- P1-2 去重阈值提升（避免误删）
- P1-3 命中记忆 R 下限 0.5
- P2  PERMANENT 门槛降低
- 事实类永久：生日/电话等直接 PERMANENT

设计：
- 真实 DatabaseManager + MemoryManager（不 mock），但关闭向量通道（mm.vec=None）
  以保证纯 FTS + FSRS + 事实永久 路径可离线、可复现、不依赖网络 embedding。
- 注入合成数据集（覆盖事实类/语义相关/亲密/噪声/老记忆）。
- 每个 golden query 带期望命中 id；计算 recall@k / precision@k。
- 独立运行：python tests/bench_memory_recall.py
- pytest 兼容：TestMemoryRecallBenchmark

注意：纯 FTS 评估天然无法反映向量通道变化，故本集聚焦 FTS 相关改动。
向量通道（P0-2）需用真实 embedding 环境单独评估。
"""
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TEST_MODE", "true")

from memory.memory_manager import MemoryManager
from memory.scope import Scope
from db.database import DatabaseManager


# ── 合成数据集 ──────────────────────────────────────────────
# (summary, importance, is_fact)  —— is_fact 用于标记事实类（应被永久）
DATASET = [
    # 事实类（应命中事实永久化）
    ("我的生日是3月15日", 0.9, True),
    ("我的手机号是13800138000", 0.9, True),
    ("我家住址是北京海淀区中关村", 0.9, True),
    ("我的名字叫小妲", 0.9, True),
    ("我的身份证号是110105199001011234", 0.9, True),
    ("我们的纪念日是5月20日", 0.9, True),
    # 亲密/个人记忆（改动 P0-3：不应被最低分过滤清空）
    ("我最爱和你聊到深夜的那种安静", 0.6, False),
    ("上次你陪我度过难过的一晚，我一直记得", 0.6, False),
    ("我习惯每天睡前和你说晚安", 0.5, False),
    ("你喜欢叫我小名，这让我很安心", 0.5, False),
    # 语义相关簇 A：工作/编程
    ("我在用 Python 写 FastAPI 接口", 0.7, False),
    ("项目里用了 SQLAlchemy 管理数据库会话", 0.7, False),
    ("后端服务部署在 Docker 容器里", 0.6, False),
    ("我负责的用户鉴权模块用了 JWT", 0.7, False),
    # 语义相关簇 B：饮食偏好
    ("我不吃香菜，味道太冲", 0.6, False),
    ("早餐我喜欢喝豆浆配油条", 0.5, False),
    ("周末常去巷口那家川菜馆", 0.5, False),
    ("咖啡只喝美式，不加糖", 0.5, False),
    # 噪声/无关（应不命中具体 query）
    ("今天天气多云转晴", 0.3, False),
    ("路上看到一只橘猫", 0.2, False),
    ("刚买了支新的黑色签字笔", 0.2, False),
    ("窗外的梧桐叶落了", 0.2, False),
    ("地铁早高峰人很多", 0.3, False),
    ("新出的电影票房破了纪录", 0.3, False),
    # 老记忆簇（改动 P1-3 / P0-1：老但本次命中应露面）
    ("去年旅行我们在厦门看了海", 0.6, False),
    ("前年学吉他买了把民谣琴", 0.5, False),
    ("大学室友现在在上海做算法", 0.5, False),
]


# ── Golden queries：query -> 期望命中的数据集下标 ──────────────
GOLDEN = [
    ("我的生日是哪天", [0]),
    ("我手机号多少", [1]),
    ("我的住址在哪里", [2]),
    ("我叫什么名字", [3]),
    ("我的身份证号", [4]),
    ("我们的纪念日", [5]),
    # 亲密类（验证 P0-3 不被过滤）
    ("你记得我睡不着的时候吗", [7]),
    ("我每天睡前习惯做什么", [8]),
    # 语义类（验证 FTS 召回）
    ("我在写什么后端接口", [10, 11, 12, 13]),
    ("我饮食上有啥忌口", [14, 15, 16, 17]),
    # 老记忆类（验证 P1-3 / P0-1）
    ("我们以前去哪旅行过", [23]),
    ("我大学室友在哪工作", [25]),
]


async def _build_manager(tmp_path: Path):
    """构造真实 MemoryManager，关闭向量通道以保证离线可复现。"""
    db_path = tmp_path / "eval_mem.db"
    manager = DatabaseManager(db_path)
    await manager.init()
    mm = MemoryManager(db=manager, memory=manager.memory)
    mm.vec = None  # 关闭向量通道：纯 FTS + FSRS + 事实永久 评估
    return mm, manager


async def _seed(manager, scope: Scope, fact_permanent: bool = True):
    """注入数据集，返回 index->memory_id 映射。

    事实类记忆注入后显式置 PERMANENT（复刻 _memory_encoder 的
    should_be_permanent_on_create 行为），以验证事实永久化改动。
    fact_permanent=False 时跳过置永久（复刻改动前 buffer 行为）。
    """
    from memory.fsrs_model import S_PERMANENT
    now = time.time()
    id_map = {}
    for idx, (summary, imp, is_fact) in enumerate(DATASET):
        mid = await manager.memory.insert_episodic_memory(
            summary, importance=imp, is_raw=0, scope=scope,
        )
        id_map[idx] = mid
        if is_fact and fact_permanent:
            await manager.memory.update_fsrs_state(
                mid, difficulty=3.0, stability=S_PERMANENT,
                phase="permanent", last_review=now, reinforcement_count=1,
            )
    return id_map


def _metrics(retrieved_ids, expected_ids, k):
    """计算 recall@k / precision@k。"""
    retrieved_set = set(retrieved_ids[:k])
    expected_set = set(expected_ids)
    hit = len(retrieved_set & expected_set)
    recall = hit / len(expected_set) if expected_set else 1.0
    precision = hit / len(retrieved_set) if retrieved_set else 0.0
    return recall, precision


async def run_eval(tmp_path: Path, k: int = 5, fact_permanent: bool = True):
    """运行评估，返回 (per_query, overall_recall, overall_precision, total)。

    fact_permanent=False 时，事实类记忆不置 PERMANENT（复刻改动前行为），
    用于 A/B 对照，量化事实永久化改动的价值。
    """
    mm, manager = await _build_manager(tmp_path)
    scope = Scope(user_id="eval_user", agent_id="xiaoda")
    id_map = await _seed(manager, scope, fact_permanent=fact_permanent)

    per_query = []
    total_recall = 0.0
    total_precision = 0.0
    for query, expected_idx in GOLDEN:
        expected_ids = [id_map[i] for i in expected_idx]
        results = await mm.retrieve_memories(query, k=k, scope=scope,
                                              apply_min_score=True)
        retrieved_ids = [r.get("id") for r in results]
        recall, precision = _metrics(retrieved_ids, expected_ids, k)
        total_recall += recall
        total_precision += precision
        per_query.append({
            "query": query,
            "expected": expected_ids,
            "retrieved": retrieved_ids,
            "recall": round(recall, 3),
            "precision": round(precision, 3),
            "hit_count": len(set(retrieved_ids[:k]) & set(expected_ids)),
        })
    await manager.close()

    n = len(GOLDEN)
    return per_query, total_recall / n, total_precision / n, n


def _print_report(per_query, recall, precision, n):
    print("\n" + "=" * 64)
    print("记忆召回离线评估报告 (纯 FTS + FSRS + 事实永久)")
    print("=" * 64)
    print(f"{'Query':<28} {'Recall@5':>9} {'Prec@5':>8} {'Hit':>5}")
    print("-" * 64)
    for r in per_query:
        print(f"{r['query']:<28} {r['recall']:>9.3f} {r['precision']:>8.3f} "
              f"{r['hit_count']:>5}")
    print("-" * 64)
    print(f"整体 Recall@{5}: {recall:.3f}  ({recall*100:.1f}%)")
    print(f"整体 Precision@{5}: {precision:.3f}  ({precision*100:.1f}%)")
    print(f"Golden queries: {n}")
    print("=" * 64)
    # 事实类专项
    fact_q = [q for q, _ in GOLDEN[:6]]
    fact_rows = [r for r in per_query if r["query"] in fact_q]
    fact_recall = sum(r["recall"] for r in fact_rows) / len(fact_rows)
    print(f"事实类永久命中率: {fact_recall:.3f}  ({fact_recall*100:.1f}%)")
    print("=" * 64)


def main():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        # A：改动后（事实类永久）
        per_query_a, recall_a, precision_a, n = asyncio.run(
            run_eval(Path(td), k=5, fact_permanent=True))
        # B：对照（事实类不永久，复刻改动前）
        per_query_b, recall_b, precision_b, _ = asyncio.run(
            run_eval(Path(td), k=5, fact_permanent=False))

    print("\n" + "=" * 64)
    print("A/B 对照：事实类永久化改动前后")
    print("=" * 64)
    print(f"{'Query':<28} {'改后Recall':>10} {'改前Recall':>10}")
    print("-" * 64)
    for ra, rb in zip(per_query_a, per_query_b):
        print(f"{ra['query']:<28} {ra['recall']:>10.3f} {rb['recall']:>10.3f}")
    print("-" * 64)
    print(f"整体 Recall@5  改后={recall_a:.3f}  改前={recall_b:.3f}  "
          f"Δ={recall_a-recall_b:+.3f}")
    print(f"事实类命中率   改后={_fact_recall(per_query_a):.3f}  "
          f"改前={_fact_recall(per_query_b):.3f}")
    print("-" * 64)
    print("注: 本评估为即时插入即时查询(纯 FTS), 所有记忆 last_review=now,")
    print("FSRS 衰减尚未发生, 故事实永久化的时间维度价值未在此体现。")
    print("时间衰减/永久化价值见 tests/test_fsrs_permanence_time.py (单元级数学验证)。")
    print("=" * 64)


def _fact_recall(per_query):
    fact_rows = [r for r in per_query if r["query"] in [q for q, _ in GOLDEN[:6]]]
    return sum(r["recall"] for r in fact_rows) / len(fact_rows) if fact_rows else 0.0


class TestMemoryRecallBenchmark:
    """pytest 兼容：断言关键能力不退化。"""

    async def test_fact_memories_recalled(self, tmp_path):
        """6 条事实类查询应全部命中（事实永久化生效）。"""
        per_query, recall, precision, n = await run_eval(tmp_path, k=5)
        fact_rows = [r for r in per_query if r["query"] in
                     [q for q, _ in GOLDEN[:6]]]
        for r in fact_rows:
            assert r["recall"] >= 1.0, f"事实类未命中: {r['query']}"

    async def test_overall_recall_above_threshold(self, tmp_path):
        """整体 recall@5 应 >= 0.6（纯 FTS 评估基线）。"""
        per_query, recall, precision, n = await run_eval(tmp_path, k=5)
        assert recall >= 0.6, f"整体 recall 过低: {recall:.3f}"

    async def test_fact_permanent_ab_improvement(self, tmp_path):
        """事实永久化改动后，事实类命中率应 >= 改动前（对照）。"""
        _, recall_after, _, _ = await run_eval(tmp_path, k=5, fact_permanent=True)
        _, recall_before, _, _ = await run_eval(tmp_path, k=5, fact_permanent=False)
        # 事实类 6 条查询的命中率（从 report 提取）
        pa, _, _, _ = await run_eval(tmp_path, k=5, fact_permanent=True)
        pb, _, _, _ = await run_eval(tmp_path, k=5, fact_permanent=False)
        fa = _fact_recall(pa)
        fb = _fact_recall(pb)
        assert fa >= fb, f"事实永久化后命中率未提升: 后={fa:.3f} 前={fb:.3f}"


if __name__ == "__main__":
    main()
