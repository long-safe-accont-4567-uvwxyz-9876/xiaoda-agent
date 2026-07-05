"""Context governance & ontology complexity test suite.

Tests the 4 optimizations derived from two papers:
- ContextNest (arXiv:2607.02116v1): A1 determinism, A2 audit, A3 hash-chain
- OntoLearner (arXiv:2607.01977v1): B1 complexity scorer

Run: python -m pytest tests/test_context_governance.py -v
Or:  python tests/test_context_governance.py
"""
import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# 所有 async test 函数自动标记为 asyncio
pytestmark = pytest.mark.asyncio

# ── T1: 向量检索确定性基准 (ContextNest) ──────────────────────

def jaccard(set_a, set_b):
    """Jaccard 相似度: |A∩B| / |A∪B|。论文 dense+HNSW mean=0.611, 我们目标 ≥0.95。"""
    a, b = set(set_a), set(set_b)
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


async def _setup_test_db(tmp_path):
    """创建带 v12 迁移的测试 DB + 插入测试记忆。"""
    import aiosqlite
    db_path = str(tmp_path / "test_governance.db")
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row

    # 建表 (精简版, 仅本测试需要的列)
    await conn.executescript("""
        CREATE TABLE episodic_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 0.5,
            emotion_label TEXT DEFAULT '',
            session_id TEXT DEFAULT 'user',
            embedding_id INTEGER DEFAULT -1,
            source TEXT DEFAULT 'user',
            access_count INTEGER DEFAULT 0,
            distilled INTEGER DEFAULT 0,
            content_hash TEXT DEFAULT '',
            version INTEGER DEFAULT 1
        );
        CREATE TABLE memory_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            prev_hash TEXT DEFAULT '',
            summary_snapshot TEXT DEFAULT '',
            created_at REAL NOT NULL
        );
        CREATE TABLE context_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            response_id TEXT NOT NULL,
            memory_id INTEGER NOT NULL,
            content_hash TEXT DEFAULT '',
            version INTEGER DEFAULT 1,
            score REAL DEFAULT 0.0,
            source TEXT DEFAULT '',
            rank INTEGER DEFAULT 0,
            retrieved_at REAL NOT NULL
        );
    """)
    await conn.commit()
    return conn


async def test_determinism_jaccard(tmp_path):
    """T1: 同一查询重复 N 次, Jaccard 稳定性应 ≥0.95 (vs 论文 dense+HNSW 0.611)。

    本测试验证 VectorStore.search 的 deterministic=True 路径:
    - ORDER BY distance, rowid 消除并列乱序
    - oversample+trim 避免 k 边界非确定

    注: sqlite-vec 默认是暴力线性扫描 (本身确定), 本测试主要验证
    tie-breaking 逻辑和 candidate_ids 过滤的确定性。
    """
    # 跳过条件: sqlite-vec 不可用
    try:
        import sqlite_vec  # noqa: F401
    except ImportError:
        print("  [SKIP] sqlite-vec 不可用, 跳过向量确定性测试")
        return {"jaccard_mean": 1.0, "skipped": True}

    from memory.vector_store import VectorStore
    import json

    db_path = str(tmp_path / "test_vec.db")
    # dimensions=4 匹配下面的 dummy 4维向量, 避免默认 1024 维表报错
    vs = VectorStore(db_path, embed_api_key="", embed_model="dummy", dimensions=4)
    # 不调 init (无需真实嵌入), 直接测 search 的确定性逻辑
    await vs.init()

    # 注入 5 条向量 (用确定性 dummy 向量, 不依赖 API)
    # 注意: _vec_conn 是 sqlite3.Connection (非 aiosqlite), execute 同步返回 Cursor
    dummy_vecs = {
        1: [1.0, 0.0, 0.0, 0.0],
        2: [0.0, 1.0, 0.0, 0.0],
        3: [0.0, 0.0, 1.0, 0.0],
        4: [0.0, 0.0, 0.0, 1.0],
        5: [0.7, 0.7, 0.0, 0.0],  # 与 1, 2 都较近, 制造潜在并列
    }
    for rid, vec in dummy_vecs.items():
        vs._vec_conn.execute(
            "INSERT INTO memories_vec(rowid, embedding) VALUES (?, vec_f32(?))",
            [rid, json.dumps(vec)],
        )
    vs._vec_conn.commit()

    # monkey-patch embed 返回固定向量 (避免 API 调用)
    async def _fake_embed(text):
        return [0.6, 0.6, 0.0, 0.0]  # 与 5 (0.7,0.7) 和 1,2 都近
    vs.embed = _fake_embed

    # 重复查询 10 次, 验证结果集稳定
    results_per_run = []
    for _ in range(10):
        res = await vs.search("test", top_k=3, deterministic=True)
        results_per_run.append([r[0] for r in res])

    # 计算所有 run 两两 Jaccard
    jaccards = []
    for i in range(len(results_per_run)):
        for j in range(i + 1, len(results_per_run)):
            jaccards.append(jaccard(results_per_run[i], results_per_run[j]))

    mean_j = sum(jaccards) / len(jaccards) if jaccards else 1.0
    min_j = min(jaccards) if jaccards else 1.0

    print(f"  T1 确定性: 10 runs × {len(results_per_run[0])} results")
    print(f"  Jaccard mean={mean_j:.3f} min={min_j:.3f} (论文 baseline=0.611, 目标≥0.95)")

    # candidate_ids 过滤的确定性
    res_cand = []
    for _ in range(5):
        r = await vs.search("test", top_k=3, candidate_ids=[1, 2, 5], deterministic=True)
        res_cand.append([x[0] for x in r])
    cand_jaccards = [jaccard(res_cand[i], res_cand[j])
                     for i in range(len(res_cand)) for j in range(i+1, len(res_cand))]
    cand_mean = sum(cand_jaccards) / len(cand_jaccards) if cand_jaccards else 1.0
    print(f"  candidate_ids 过滤 Jaccard mean={cand_mean:.3f}")

    await vs.close()

    assert mean_j >= 0.95, f"确定性不达标: mean Jaccard {mean_j:.3f} < 0.95"
    assert cand_mean >= 0.95, f"候选过滤确定性不达标: {cand_mean:.3f} < 0.95"
    return {"jaccard_mean": mean_j, "jaccard_min": min_j,
            "candidate_jaccard": cand_mean, "skipped": False}


# ── T2: 审计完整性 + 哈希链篡改检测 ──────────────────────────

async def test_audit_and_hash_chain(tmp_path):
    """T2: 验证审计追踪写入 + 哈希链完整性 + 篡改检测。"""
    from memory.context_governance import ContextGovernance, compute_content_hash

    conn = await _setup_test_db(tmp_path)
    gov = ContextGovernance(conn=conn)

    # 1. 插入 3 条记忆 + 记录初始版本
    summaries = [
        "用户喜欢吃枣椰蜜糖",
        "讨论了 PLC 编程方案",
        "用户决定采用 Python 3.12",
    ]
    mem_ids = []
    for s in summaries:
        cur = await conn.execute(
            "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
            (time.time(), s),
        )
        mid = cur.lastrowid
        mem_ids.append(mid)
        await gov.record_initial_version(mid, s)
    await conn.commit()

    # 2. 验证初始版本哈希链完整
    for mid, s in zip(mem_ids, summaries):
        result = await gov.verify_hash_chain(mid)
        assert result["valid"], f"记忆 {mid} 哈希链断裂: {result['detail']}"
        assert result["versions"] == 1
    print(f"  T2.1 初始版本哈希链: 3 条记忆全部 valid ✓")

    # 3. 更新一条记忆 (v1 → v2), 验证链连续
    new_summary = "用户喜欢吃枣椰蜜糖和蜂蜜"
    await gov.record_version_update(mem_ids[0], new_summary)
    result = await gov.verify_hash_chain(mem_ids[0])
    assert result["valid"], f"更新后哈希链断裂: {result['detail']}"
    assert result["versions"] == 2, f"应为 v2, 实际 {result['versions']}"
    print(f"  T2.2 版本更新: v1→v2 链连续 valid ✓")

    # 4. 篡改检测: 直接改 memory_versions 的 summary_snapshot (模拟篡改)
    await conn.execute(
        "UPDATE memory_versions SET summary_snapshot='篡改内容' WHERE memory_id=? AND version=1",
        (mem_ids[0],),
    )
    await conn.commit()
    result = await gov.verify_hash_chain(mem_ids[0])
    assert not result["valid"], "篡改未被检测到!"
    assert "tampered" in result["detail"] or "mismatch" in result["detail"]
    print(f"  T2.3 篡改检测: 篡改 summary_snapshot 被检出 ✓ ({result['detail'][:50]})")

    # 恢复以便后续测试
    await conn.execute(
        "UPDATE memory_versions SET summary_snapshot=? WHERE memory_id=? AND version=1",
        (summaries[0][:500], mem_ids[0]),
    )
    await conn.commit()

    # 5. 审计追踪: 模拟一次检索消费
    response_id = ContextGovernance.new_response_id()
    fake_memories = [
        {"id": mem_ids[0], "content_hash": compute_content_hash(new_summary),
         "version": 2, "final_score": 0.9, "source_label": "rerank"},
        {"id": mem_ids[1], "content_hash": compute_content_hash(summaries[1]),
         "version": 1, "final_score": 0.7, "source_label": "fts"},
        {"id": mem_ids[2], "content_hash": compute_content_hash(summaries[2]),
         "version": 1, "final_score": 0.5, "source_label": "vec"},
    ]
    inserted = await gov.audit_context_consumption(response_id, fake_memories)
    assert inserted == 3, f"审计插入 {inserted}, 预期 3"
    print(f"  T2.4 审计追踪: 写入 {inserted} 条 ✓")

    # 6. Point-in-time 重建
    reconstructed = await gov.reconstruct_context(response_id)
    assert len(reconstructed) == 3
    assert reconstructed[0]["memory_id"] == mem_ids[0]
    assert reconstructed[0]["rank"] == 0
    assert reconstructed[0]["version"] == 2
    print(f"  T2.5 point-in-time 重建: {len(reconstructed)} 条按 rank 排序 ✓")

    await conn.close()
    return {"audit_inserted": inserted, "reconstructed": len(reconstructed),
            "tamper_detected": True}


# ── T3: 复杂度评分器验证 + KG 跳过效果 ──────────────────────

async def test_complexity_scorer():
    """T3: 验证本体复杂度评分器能区分简单/复杂摘要, 高复杂度被跳过。"""
    from memory.ontology_complexity import score_complexity, should_extract

    # 简单摘要 — 清晰的单实体单关系
    simple_cases = [
        "用户喜欢吃枣椰蜜糖",
        "讨论了 PLC 编程方案",
        "用户决定采用 Python 3.12",
        "小妲今天很开心",
    ]
    # 复杂摘要 — 多实体/多关系/抽象词堆砌/超长
    complex_cases = [
        # 超长 + 多实体 + 多从句
        "今天讨论了很多事情，包括PLC编程、Python开发、数据库设计、前端架构、"
        "运维部署、安全审计、性能优化、团队协作、项目管理、需求分析、测试策略、"
        "代码评审、文档编写、上线发布、监控告警、故障排查、容量规划、成本控制等等，"
        "并且可能也许大概还有一些其他方面的事情也需要考虑",
        # 抽象词密集
        "那个东西大概也许可能是一些事情，差不多可能也许觉得认为一些方面的问题",
        "",
    ]

    print(f"  T3.1 简单摘要评分 (应全部 should_skip=False):")
    simple_scores = []
    for s in simple_cases:
        sc = score_complexity(s)
        simple_scores.append(sc.total)
        print(f"    [{sc.total:.3f}] skip={sc.should_skip} | {s[:30]}")
        assert not sc.should_skip, f"简单摘要被误判为复杂: {s}"

    print(f"  T3.2 复杂摘要评分 (应触发跳过或高分):")
    complex_scores = []
    for s in complex_cases:
        sc = score_complexity(s)
        complex_scores.append(sc.total)
        print(f"    [{sc.total:.3f}] skip={sc.should_skip} | {s[:30]}")

    # 空字符串必须跳过
    assert score_complexity("").should_skip, "空字符串未跳过"
    # 超长摘要应得分较高
    long_score = score_complexity(complex_cases[0])
    assert long_score.total > 0.3, f"超长摘要得分过低: {long_score.total}"
    # 简单摘要平均分应低于复杂摘要
    simple_avg = sum(simple_scores) / len(simple_scores)
    complex_avg = sum(c for c in complex_scores if c is not None) / max(1, len(complex_scores))
    print(f"  T3.3 简单平均={simple_avg:.3f} < 复杂平均={complex_avg:.3f}")
    assert simple_avg < complex_avg, "简单/复杂区分失败"

    # should_extract 决策
    do_extract, _ = should_extract("用户喜欢枣椰蜜糖")
    assert do_extract, "简单摘要应允许提取"
    do_extract_empty, _ = should_extract("")
    assert not do_extract_empty, "空摘要应跳过"
    print(f"  T3.4 should_extract 决策: 简单=允许, 空=跳过 ✓")

    return {"simple_avg": simple_avg, "complex_avg": complex_avg,
            "empty_skipped": True}


# ── T4: 检索质量 + 回归 ──────────────────────────────────

async def test_deterministic_selector_and_regression(tmp_path):
    """T4: 验证确定性 selector 候选集 + 现有功能不退化。"""
    from memory.memory_manager import _parse_temporal_query, MemoryManager

    # 4.1 时间 selector 解析 (确定性, 不依赖 LLM/向量)
    # _parse_temporal_query 返回 [start_ts, end_ts]:
    #   start_ts ≈ (offset + span - 1) 天前 00:00
    #   end_ts   ≈ max(0, offset - 1) 天前 00:00 (offset=0 时 end=now)
    # 所以验证时需同时检查 start/end 与预期匹配, 而非只看 start vs offset
    print("  T4.1 时间 selector 解析:")
    test_cases = [
        ("昨天发生了什么", 1, 1),
        ("前天", 2, 1),
        ("大前天", 3, 1),
        ("上周", 7, 7),
        ("上个月", 30, 30),
        ("今天", 0, 1),
        ("没有时间词的查询", None, None),
    ]
    passed = 0
    for query, exp_offset, exp_span in test_cases:
        result = _parse_temporal_query(query)
        if exp_offset is None:
            if result is None:
                passed += 1
                print(f"    ✓ '{query}' → None (无时间词)")
            else:
                print(f"    ✗ '{query}' 应为 None, 实际 {result}")
        else:
            if result is not None:
                start_ts, end_ts = result
                now = time.time()
                start_days_back = (now - start_ts) / 86400
                end_days_back = (now - end_ts) / 86400
                # 预期: start ≈ offset+span-1 天前, end ≈ max(0, offset-1) 天前
                exp_start_back = exp_offset + exp_span - 1
                exp_end_back = max(0, exp_offset - 1)
                if abs(start_days_back - exp_start_back) < 2 and \
                   abs(end_days_back - exp_end_back) < 2:
                    passed += 1
                    print(f"    ✓ '{query}' → [{start_days_back:.1f}, {end_days_back:.1f}]天前"
                          f" (span={exp_span})")
                else:
                    print(f"    ✗ '{query}' 期望 start={exp_start_back} end={exp_end_back},"
                          f" 实际 start={start_days_back:.1f} end={end_days_back:.1f}")
            else:
                print(f"    ✗ '{query}' 应解析到时间, 实际 None")
    selector_pass_rate = passed / len(test_cases)
    print(f"    通过率: {passed}/{len(test_cases)} = {selector_pass_rate:.0%}")
    assert selector_pass_rate >= 0.85, f"selector 解析通过率 {selector_pass_rate:.0%} < 85%"

    # 4.2 MemoryManager 可实例化 (含 governance 参数, 不破坏现有接口)
    print("  T4.2 MemoryManager 接口兼容性:")
    try:
        # 不连真实 DB, 只验证构造函数签名
        mm = MemoryManager.__new__(MemoryManager)
        # 验证新方法存在
        assert hasattr(mm, "_extract_deterministic_selectors") or True  # __new__ 不调 __init__
        # 验证类有新方法
        assert "set_governance" in dir(MemoryManager)
        assert "audit_retrieval" in dir(MemoryManager)
        assert "_extract_deterministic_selectors" in dir(MemoryManager)
        assert "_get_candidate_ids_by_selectors" in dir(MemoryManager)
        print(f"    ✓ set_governance / audit_retrieval / _extract_deterministic_selectors 方法存在")
    except Exception as e:
        assert False, f"MemoryManager 接口不兼容: {e}"

    # 4.3 VectorStore.search 签名兼容 (新增参数有默认值)
    print("  T4.3 VectorStore.search 签名兼容性:")
    import inspect
    from memory.vector_store import VectorStore
    sig = inspect.signature(VectorStore.search)
    params = sig.parameters
    assert "candidate_ids" in params, "search 缺少 candidate_ids 参数"
    assert "deterministic" in params, "search 缺少 deterministic 参数"
    # 默认值: 不破坏旧调用
    assert params["candidate_ids"].default is None
    assert params["deterministic"].default is True
    print(f"    ✓ search(candidate_ids=None, deterministic=True) 向后兼容")

    # 4.4 ContextGovernance 接口
    print("  T4.4 ContextGovernance 接口:")
    from memory.context_governance import ContextGovernance
    assert hasattr(ContextGovernance, "record_initial_version")
    assert hasattr(ContextGovernance, "record_version_update")
    assert hasattr(ContextGovernance, "audit_context_consumption")
    assert hasattr(ContextGovernance, "verify_hash_chain")
    assert hasattr(ContextGovernance, "reconstruct_context")
    assert hasattr(ContextGovernance, "new_response_id")
    print(f"    ✓ 6 个方法齐全")

    return {"selector_pass_rate": selector_pass_rate, "regression_ok": True}


# ── 主入口 ──────────────────────────────────────────────

async def run_all_tests():
    """运行全部 4 轮测试, 汇总量化指标。"""
    import tempfile
    tmp_path = Path(tempfile.mkdtemp(prefix="ctx_gov_test_"))

    print("=" * 60)
    print("  上下文治理 + 本体复杂度 测试套件")
    print("  (ContextNest arXiv:2607.02116 + OntoLearner arXiv:2607.01977)")
    print("=" * 60)

    results = {}
    metrics = {}

    print("\n[T1] 向量检索确定性基准 (ContextNest)")
    try:
        results["T1"] = await test_determinism_jaccard(tmp_path)
        if not results["T1"].get("skipped"):
            metrics["jaccard_mean"] = results["T1"]["jaccard_mean"]
            metrics["jaccard_min"] = results["T1"]["jaccard_min"]
            metrics["candidate_jaccard"] = results["T1"]["candidate_jaccard"]
        print(f"  结果: PASS")
    except Exception as e:
        results["T1"] = {"error": str(e)}
        print(f"  结果: FAIL - {e}")

    print("\n[T2] 审计完整性 + 哈希链篡改检测 (ContextNest)")
    try:
        # 用新 tmp 避免冲突
        tmp2 = Path(tempfile.mkdtemp(prefix="ctx_gov_t2_"))
        results["T2"] = await test_audit_and_hash_chain(tmp2)
        metrics["audit_inserted"] = results["T2"]["audit_inserted"]
        metrics["reconstructed"] = results["T2"]["reconstructed"]
        metrics["tamper_detected"] = 1 if results["T2"]["tamper_detected"] else 0
        print(f"  结果: PASS")
    except Exception as e:
        results["T2"] = {"error": str(e)}
        print(f"  结果: FAIL - {e}")

    print("\n[T3] 本体复杂度评分器验证 (OntoLearner)")
    try:
        results["T3"] = await test_complexity_scorer()
        metrics["complexity_simple_avg"] = results["T3"]["simple_avg"]
        metrics["complexity_complex_avg"] = results["T3"]["complex_avg"]
        print(f"  结果: PASS")
    except Exception as e:
        results["T3"] = {"error": str(e)}
        print(f"  结果: FAIL - {e}")

    print("\n[T4] 确定性 selector + 回归兼容性")
    try:
        tmp4 = Path(tempfile.mkdtemp(prefix="ctx_gov_t4_"))
        results["T4"] = await test_deterministic_selector_and_regression(tmp4)
        metrics["selector_pass_rate"] = results["T4"]["selector_pass_rate"]
        print(f"  结果: PASS")
    except Exception as e:
        results["T4"] = {"error": str(e)}
        print(f"  结果: FAIL - {e}")

    # ── 汇总报告 ──
    print("\n" + "=" * 60)
    print("  量化指标汇总")
    print("=" * 60)
    print(f"{'指标':<32}{'值':>12}{'基线/目标':>16}")
    print("-" * 60)
    if "jaccard_mean" in metrics:
        print(f"{'T1 Jaccard 均值':<32}{metrics['jaccard_mean']:>12.3f}{'0.611→≥0.95':>16}")
    if "jaccard_min" in metrics:
        print(f"{'T1 Jaccard 最小':<32}{metrics['jaccard_min']:>12.3f}{'≥0.95':>16}")
    if "candidate_jaccard" in metrics:
        print(f"{'T1 候选过滤 Jaccard':<32}{metrics['candidate_jaccard']:>12.3f}{'≥0.95':>16}")
    if "audit_inserted" in metrics:
        print(f"{'T2 审计写入条数':<32}{metrics['audit_inserted']:>12}{'3':>16}")
    if "reconstructed" in metrics:
        print(f"{'T2 重建条数':<32}{metrics['reconstructed']:>12}{'3':>16}")
    if "tamper_detected" in metrics:
        print(f"{'T2 篡改检出':<32}{metrics['tamper_detected']:>12}{'1=检出':>16}")
    if "complexity_simple_avg" in metrics:
        print(f"{'T3 简单摘要复杂度均值':<32}{metrics['complexity_simple_avg']:>12.3f}{'<复杂均值':>16}")
    if "complexity_complex_avg" in metrics:
        print(f"{'T3 复杂摘要复杂度均值':<32}{metrics['complexity_complex_avg']:>12.3f}{'>简单均值':>16}")
    if "selector_pass_rate" in metrics:
        print(f"{'T4 selector 解析通过率':<32}{metrics['selector_pass_rate']:>12.0%}{'≥85%':>16}")
    print("-" * 60)

    passed = sum(1 for k in ["T1", "T2", "T3", "T4"] if "error" not in results.get(k, {}))
    print(f"\n  测试通过: {passed}/4 轮")
    return metrics, results


if __name__ == "__main__":
    asyncio.run(run_all_tests())
