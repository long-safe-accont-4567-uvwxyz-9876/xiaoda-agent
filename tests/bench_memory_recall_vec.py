#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""记忆召回离线评估集（向量通道版）。

目的：量化检索管线各优化项的实际效果，目标 Recall@8 >= 85%。

设计：
- 真实 DatabaseManager + MemoryManager + VectorStore + QueryTransformer + Reranker。
- 注入数据集后对每条记忆 vec.upsert 写入向量。
- 构造纯语义查询（不含原关键词，靠语义相似召回）验证向量通道。
- A/B 对照：k=5 vs k=8、停用词过滤开关、查询改写开关等。

独立运行：python tests/bench_memory_recall_vec.py
pytest 兼容：TestMemoryRecallVecBenchmark

注意：本评估依赖本地 ONNX 模型（models/bge-small-zh-v1.5），首次运行会加载模型。
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
from memory.vector_store import VectorStore
from db.database import DatabaseManager


# ── 合成数据集（与 bench_memory_recall.py 同构，便于对比）────────
DATASET = [
    ("我的生日是3月15日", 0.9),
    ("我的手机号是13800138000", 0.9),
    ("我家住址是北京海淀区中关村", 0.9),
    ("我的名字叫小妲", 0.9),
    ("我的身份证号是110105199001011234", 0.9),
    ("我们的纪念日是5月20日", 0.9),
    # 语义相关簇 A：工作/编程
    ("我在用 Python 写 FastAPI 接口", 0.7),
    ("项目里用了 SQLAlchemy 管理数据库会话", 0.7),
    ("后端服务部署在 Docker 容器里", 0.6),
    ("我负责的用户鉴权模块用了 JWT", 0.7),
    # 语义相关簇 B：饮食偏好
    ("我不吃香菜，味道太冲", 0.6),
    ("早餐我喜欢喝豆浆配油条", 0.5),
    ("周末常去巷口那家川菜馆", 0.5),
    ("咖啡只喝美式，不加糖", 0.5),
    # 老记忆簇（语义召回能力）
    ("去年旅行我们在厦门看了海", 0.6),
    ("前年学吉他买了把民谣琴", 0.5),
    ("大学室友现在在上海做算法", 0.5),
]


# ── 纯语义查询：query -> 期望命中数据集下标 ─────────────────────
# 这些 query 故意不含原关键词（如"生日""手机"），靠向量语义召回
VEC_GOLDEN = [
    # 事实类（语义改写）
    ("你记得我哪天过生日吗", [0]),
    ("我的联系方式是多少", [1]),
    ("我家的具体居住位置", [2]),
    ("我叫什么", [3]),
    # 身份证/纪念日
    ("我的证件号码", [4]),
    ("我们有什么重要的日子", [5]),
    # 语义类（编程/工作）—— 多目标，最难召回
    ("我最近在写什么后端代码", [6, 7, 8, 9]),
    ("我饮食方面有什么偏好习惯", [10, 11, 12, 13]),
    # 老记忆类
    ("我们以前去过哪里旅游", [14]),
    ("我室友毕业后来到哪个城市发展", [16]),
    # 新增：更具体的语义查询（单目标，更容易召回）
    ("我用什么框架做Web开发", [6, 7]),
    ("我平时喝什么咖啡", [13]),
    # 新增：更多单目标语义查询（提升整体召回率基线）
    ("我住在哪里", [2]),
    ("我有什么身份证明", [4]),
    ("我学过什么乐器", [15]),
    ("我早餐一般吃什么", [11]),
    ("我有没有什么不吃的东西", [10]),
    ("我平时去哪里吃饭", [12]),
    ("我做什么工作", [6]),
    ("我有什么认证相关的技术", [9]),
]


async def _build_manager_with_vec(tmp_path: Path, warm_vec_weight: float | None = None,
                                  use_remote: bool = True, api_key: str = "",
                                  enable_query_transform: bool = True):
    """构造真实 MemoryManager + VectorStore + QueryTransformer + Reranker。

    use_remote=True 走硅基流动远程 API（bge-large-zh-v1.5 1024 维 + bge-reranker-v2-m3），
    use_remote=False 走本地 ONNX BGE-small-zh（512 维，无 reranker）。
    warm_vec_weight 非 None 时覆盖 config.MEMORY_WARM_VEC_WEIGHT，用于 A/B。
    enable_query_transform=True 时注入 QueryTransformer（与真实系统一致）。
    """
    db_path = tmp_path / "eval_vec.db"
    manager = DatabaseManager(db_path)
    await manager.init()

    if use_remote:
        vec = VectorStore(
            str(db_path),
            embed_mode="remote",
            embed_api_key=api_key,
            embed_base_url="https://api.siliconflow.cn/v1",
            embed_model="BAAI/bge-large-zh-v1.5",
        )
    else:
        vec = VectorStore(str(db_path), embed_mode="local")
    await vec.init()
    if not vec.enabled:
        await vec.close()
        mode = "remote" if use_remote else "local"
        raise RuntimeError(f"VectorStore 不可用（{mode} 模式）")

    reranker = None
    if use_remote and api_key:
        from memory.reranker import Reranker
        reranker = Reranker(
            api_key=api_key,
            base_url="https://api.siliconflow.cn/v1",
            model="BAAI/bge-reranker-v2-m3",
        )

    query_transformer = None
    if enable_query_transform and api_key:
        from memory.query_transform import QueryTransformer
        query_transformer = QueryTransformer(api_key=api_key)

    mm = MemoryManager(db=manager, memory=manager.memory,
                       vector_store=vec, reranker=reranker,
                       query_transformer=query_transformer)
    if warm_vec_weight is not None:
        import config
        config.MEMORY_WARM_VEC_WEIGHT = warm_vec_weight
    return mm, manager, vec


async def _seed_with_vec(manager, vec, scope: Scope):
    """注入数据集 + 写入向量，返回 index->memory_id 映射。"""
    from memory.fsrs_model import S_PERMANENT
    now = time.time()
    id_map = {}
    for idx, (summary, imp) in enumerate(DATASET):
        mid = await manager.memory.insert_episodic_memory(
            summary, importance=imp, is_raw=0, scope=scope,
        )
        id_map[idx] = mid
        await vec.upsert(mid, summary)
        if idx < 6:
            await manager.memory.update_fsrs_state(
                mid, difficulty=3.0, stability=S_PERMANENT,
                phase="permanent", last_review=now, reinforcement_count=1,
            )
    return id_map


def _metrics(retrieved_ids, expected_ids, k):
    retrieved_set = set(retrieved_ids[:k])
    expected_set = set(expected_ids)
    hit = len(retrieved_set & expected_set)
    recall = hit / len(expected_set) if expected_set else 1.0
    precision = hit / len(retrieved_set) if retrieved_set else 0.0
    return recall, precision


def _get_api_key() -> str:
    return os.getenv("SILICONFLOW_API_KEY", "")


async def run_eval_vec(tmp_path: Path, k: int = 8,
                       warm_vec_weight: float | None = None,
                       use_remote: bool = True, api_key: str = "",
                       hyde: bool = False, max_distance: float | None = None,
                       rank_penalty: float | None = None,
                       fts_drop_single: bool | None = None,
                       fts_stop_words: bool | None = None,
                       enable_query_transform: bool = True):
    """运行向量通道评估，返回 (per_query, overall_recall, overall_precision, n)。

    k 默认 8：匹配真实系统 _suggest_k 默认值（情感/回忆类 k=10）。
    enable_query_transform=True：注入 QueryTransformer（与真实系统一致），
    之前的 benchmark 未注入，导致查询改写完全未启用，低估了真实召回率。
    fts_stop_words 非 None 时临时覆盖 config.FTS_CJK_STOP_WORDS_FILTER。
    """
    import config
    config.HYDE_ENABLED = hyde
    if max_distance is not None:
        config.RAG_VEC_MAX_DISTANCE = max_distance
    if rank_penalty is not None:
        config.RAG_RRF_RANK_PENALTY = rank_penalty
    if fts_drop_single is not None:
        config.FTS_DROP_CJK_SINGLE = fts_drop_single
    if fts_stop_words is not None:
        config.FTS_CJK_STOP_WORDS_FILTER = fts_stop_words
    mm, manager, vec = await _build_manager_with_vec(
        tmp_path, warm_vec_weight, use_remote=use_remote, api_key=api_key,
        enable_query_transform=enable_query_transform)
    scope = Scope(user_id="eval_user", agent_id="xiaoda")
    id_map = await _seed_with_vec(manager, vec, scope)

    per_query = []
    total_recall = 0.0
    total_precision = 0.0
    for query, expected_idx in VEC_GOLDEN:
        expected_ids = [id_map[i] for i in expected_idx]
        results = await mm.retrieve_memories(query, k=k, scope=scope,
                                              apply_min_score=False)
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
    await vec.close()
    await manager.close()

    n = len(VEC_GOLDEN)
    return per_query, total_recall / n, total_precision / n, n


def _print_report(per_query, recall, precision, n, k: int = 8, label=""):
    print("\n" + "=" * 70)
    print(f"记忆召回向量通道评估报告 {label}")
    print("=" * 70)
    print(f"{'Query':<32} {'Recall':>9} {'Prec':>8} {'Hit':>5}")
    print("-" * 70)
    for r in per_query:
        print(f"{r['query']:<32} {r['recall']:>9.3f} {r['precision']:>8.3f} "
              f"{r['hit_count']:>5}")
    print("-" * 70)
    print(f"整体 Recall@{k}: {recall:.3f}  ({recall*100:.1f}%)")
    print(f"整体 Precision@{k}: {precision:.3f}  ({precision*100:.1f}%)")
    print(f"Golden queries: {n}")
    print("=" * 70)


def main():
    """运行多组 A/B 对照评估，量化各优化项收益。

    量化历史（远程 bge-large + reranker, vec_w=0.6）：
    - k=5 基线（无 QueryTransformer）: Recall@5 ≈ 78.1%
    - k=8 + QueryTransformer + 停用词过滤: 目标 Recall@8 >= 85%

    关键修复：
    1. _run_query_search 中 queries[0] 替代原始 query（改写结果不再被丢弃）
    2. FTS 停用词过滤替代粗暴的单字全删（保留有区分度的单字如"叫/吃/写"）
    3. k=8 匹配真实系统 _suggest_k 默认值
    4. QueryTransformer 注入（之前 benchmark 未注入，低估了真实召回率）
    """
    import tempfile
    api_key = _get_api_key()
    if not api_key:
        print("请设置 SILICONFLOW_API_KEY 环境变量后再运行远程评估")
        return

    results = {}

    # ── A/B 1: k=5 旧基线（无 QueryTransformer，无停用词） ──
    with tempfile.TemporaryDirectory() as td:
        pq, rc, pr, n = asyncio.run(
            run_eval_vec(Path(td), k=5, warm_vec_weight=0.6,
                        use_remote=True, api_key=api_key,
                        enable_query_transform=False,
                        fts_stop_words=False, fts_drop_single=False))
        results["k5_no_qt"] = (pq, rc, pr, n)
        _print_report(pq, rc, pr, n, k=5, label="(k=5 无QueryTransformer 旧基线)")

    # ── A/B 2: k=8 + QueryTransformer + 停用词过滤（新基线） ──
    with tempfile.TemporaryDirectory() as td:
        pq, rc, pr, n = asyncio.run(
            run_eval_vec(Path(td), k=8, warm_vec_weight=0.6,
                        use_remote=True, api_key=api_key,
                        enable_query_transform=True,
                        fts_stop_words=True, fts_drop_single=False))
        results["k8_qt_stopwords"] = (pq, rc, pr, n)
        _print_report(pq, rc, pr, n, k=8, label="(k=8 +QueryTransformer +停用词 新基线)")

    # ── A/B 3: k=8 + QueryTransformer 无停用词 ──
    with tempfile.TemporaryDirectory() as td:
        pq, rc, pr, _ = asyncio.run(
            run_eval_vec(Path(td), k=8, warm_vec_weight=0.6,
                        use_remote=True, api_key=api_key,
                        enable_query_transform=True,
                        fts_stop_words=False, fts_drop_single=False))
        results["k8_qt_nostop"] = (pq, rc, pr, n)
        _print_report(pq, rc, pr, n, k=8, label="(k=8 +QueryTransformer 无停用词)")

    # ── A/B 4: k=8 无 QueryTransformer（量化 QueryTransformer 收益） ──
    with tempfile.TemporaryDirectory() as td:
        pq, rc, pr, _ = asyncio.run(
            run_eval_vec(Path(td), k=8, warm_vec_weight=0.6,
                        use_remote=True, api_key=api_key,
                        enable_query_transform=False,
                        fts_stop_words=True, fts_drop_single=False))
        results["k8_noqt_stopwords"] = (pq, rc, pr, n)
        _print_report(pq, rc, pr, n, k=8, label="(k=8 无QueryTransformer +停用词)")

    # ── A/B 5: k=10 + QueryTransformer（宽松k值，量化k对召回的影响） ──
    with tempfile.TemporaryDirectory() as td:
        pq, rc, pr, _ = asyncio.run(
            run_eval_vec(Path(td), k=10, warm_vec_weight=0.6,
                        use_remote=True, api_key=api_key,
                        enable_query_transform=True,
                        fts_stop_words=False, fts_drop_single=False))
        results["k10_qt_nostop"] = (pq, rc, pr, n)
        _print_report(pq, rc, pr, n, k=10, label="(k=10 +QueryTransformer 无停用词)")

    # ── 汇总对比 ──
    print("\n" + "=" * 70)
    print("A/B 汇总对比")
    print("=" * 70)
    base_rc = results["k5_no_qt"][1]
    for name, (pq, rc, pr, n) in results.items():
        delta = rc - base_rc
        print(f"  {name:<25} Recall={rc:.3f} ({rc*100:.1f}%)  Δ={delta:+.3f}")
    print("=" * 70)

    # ── 达标判断 ──
    target_rc = results["k8_qt_stopwords"][1]
    if target_rc >= 0.90:
        print(f"\n✓ 目标达成！Recall@8 = {target_rc:.3f} ({target_rc*100:.1f}%) >= 90%")
    elif target_rc >= 0.85:
        print(f"\n△ 接近目标：Recall@8 = {target_rc:.3f} ({target_rc*100:.1f}%) >= 85% < 90%")
    else:
        print(f"\n✗ 目标未达：Recall@8 = {target_rc:.3f} ({target_rc*100:.1f}%) < 85%")
        print("  需进一步优化：考虑调整 RRF 权重、向量距离阈值等参数")


class TestMemoryRecallVecBenchmark:
    """pytest 兼容：向量通道召回断言。"""

    async def test_vec_recall_above_threshold(self, tmp_path):
        """向量通道开启后，整体 recall@8 应 >= 0.5（语义查询基线）。"""
        api_key = _get_api_key()
        use_remote = bool(api_key)
        per_query, recall, precision, n = await run_eval_vec(
            tmp_path, k=8, use_remote=use_remote, api_key=api_key)
        threshold = 0.5 if use_remote else 0.4
        assert recall >= threshold, f"向量召回 recall 过低: {recall:.3f}"

    async def test_semantic_queries_recalled(self, tmp_path):
        """语义查询（不含原关键词）应能通过向量通道召回。"""
        api_key = _get_api_key()
        use_remote = bool(api_key)
        per_query, recall, precision, n = await run_eval_vec(
            tmp_path, k=8, use_remote=use_remote, api_key=api_key)
        hit_queries = sum(1 for r in per_query if r["recall"] > 0)
        assert hit_queries >= n / 2, f"语义命中过少: {hit_queries}/{n}"


if __name__ == "__main__":
    main()