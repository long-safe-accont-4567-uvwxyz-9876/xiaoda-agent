#!/usr/bin/env python3
"""本能管理器端到端性能基准测试

量化考核：
- 每个操作的 p50/p99/max 耗时
- 事件循环心跳延迟（>50ms = 阻塞，>100ms = 严重阻塞）
- 用真实 aiosqlite + rapidfuzz，非 mock 计算

用法: .venv/bin/python scripts/bench_instinct.py
"""
import asyncio
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
from instinct_manager import InstinctManager

# 6 条 active 本能（模拟生产 top 6）
TEST_INSTINCTS = [
    ("用户偏好浓香入味的菜品", 0.85),
    ("用户倾向于直接处理问题", 0.75),
    ("用户喜欢被打断时继续说", 0.80),
    ("用户偏好简洁的回复风格", 0.70),
    ("用户习惯深夜活跃", 0.65),
    ("用户喜欢被称呼为爸爸", 0.90),
]

# 200 条用于 merge_duplicates 压测
BULK_INSTINCTS = [
    (f"用户偏好类型{i}号行为模式变体{i % 5}", 0.5 + (i % 10) * 0.04)
    for i in range(200)
]


class FakeDB:
    """模拟 DatabaseManager：_conn 指向 aiosqlite.Connection（供 InstinctManager 用），
    自身也代理 execute/commit/executemany（供 setup_db 用）"""
    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql, params=()):
        return await self._conn.execute(sql, params)

    async def commit(self):
        await self._conn.commit()

    async def executemany(self, sql, params):
        return await self._conn.executemany(sql, params)

    async def close(self):
        await self._conn.close()


async def setup_db(seed_bulk=False):
    """创建内存数据库 + instincts 表 + 测试数据"""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    db = FakeDB(conn)
    await db.execute("""CREATE TABLE IF NOT EXISTS instincts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        confidence REAL DEFAULT 0.5,
        source_session TEXT,
        status TEXT DEFAULT 'active',
        created_at REAL,
        last_used_at REAL,
        use_count INTEGER DEFAULT 0
    )""")
    now = time.time()
    for content, conf in TEST_INSTINCTS:
        await db.execute(
            "INSERT INTO instincts (content, confidence, status, created_at, last_used_at, use_count) "
            "VALUES (?, ?, 'active', ?, ?, 0)",
            (content, conf, now, now),
        )
    if seed_bulk:
        for content, conf in BULK_INSTINCTS:
            await db.execute(
                "INSERT INTO instincts (content, confidence, status, created_at, last_used_at, use_count) "
                "VALUES (?, ?, 'active', ?, ?, 0)",
                (content, conf, now, now),
            )
    await db.commit()
    return db


class MockRouter:
    async def route(self, **kwargs):
        return None


async def heartbeat_monitor(stop_event):
    """事件循环心跳监测：每 5ms 醒一次，测量实际延迟"""
    latencies = []
    while not stop_event.is_set():
        start = time.monotonic()
        await asyncio.sleep(0.005)
        elapsed = time.monotonic() - start
        latency_ms = max(0.0, (elapsed - 0.005) * 1000)
        latencies.append(latency_ms)
    return latencies


async def bench(name, func, iterations=50):
    """测量单个操作耗时 + 事件循环阻塞"""
    durations = []
    max_hb_list = []
    for _ in range(iterations):
        stop_event = asyncio.Event()
        hb_task = asyncio.create_task(heartbeat_monitor(stop_event))
        start = time.monotonic()
        await func()
        duration = time.monotonic() - start
        stop_event.set()
        hb_latencies = await hb_task
        durations.append(duration * 1000)
        if hb_latencies:
            max_hb_list.append(max(hb_latencies))
    durations.sort()
    max_hb_list.sort()
    p50 = statistics.median(durations)
    p99 = durations[int(len(durations) * 0.99)] if len(durations) > 1 else durations[0]
    max_dur = max(durations)
    max_hb = max(max_hb_list) if max_hb_list else 0.0
    p99_hb = statistics.median(max_hb_list) if max_hb_list else 0.0
    verdict = "⚠️ 阻塞" if max_hb > 50 else ("⚠️ 轻微" if max_hb > 20 else "✓ 流畅")
    print(f"  {name}:")
    print(f"    耗时  p50={p50:7.2f}ms  p99={p99:7.2f}ms  max={max_dur:7.2f}ms")
    print(f"    心跳  p50={p99_hb:7.2f}ms  max={max_hb:7.2f}ms  {verdict}")
    return {"name": name, "p50": p50, "p99": p99, "max": max_dur, "max_hb": max_hb}


async def main():
    print("=" * 64)
    print("  本能管理器 端到端性能基准（真实 aiosqlite + rapidfuzz）")
    print("  心跳 >50ms = 事件循环阻塞；>100ms = 严重阻塞")
    print("=" * 64)

    # ── 场景 1: correct_instinct（定位 + 降权，6 条本能）──
    db = await setup_db()
    mgr = InstinctManager(db=db, router=MockRouter())
    mgr._available = True

    async def op_correct():
        # 每轮重置 confidence，避免累积降权
        await db.execute("UPDATE instincts SET confidence=0.8, status='active'")
        await db.commit()
        await mgr.correct_instinct("用户喜欢被打断时继续说", "demote")

    print("\n[场景1] correct_instinct 定位+降权（6条 rapidfuzz）")
    await bench("correct_instinct", op_correct, iterations=50)
    await db.close()

    # ── 场景 2: get_active_instincts（查询 top 6）──
    db = await setup_db()
    mgr = InstinctManager(db=db, router=MockRouter())
    mgr._available = True

    async def op_query():
        await mgr.get_active_instincts(limit=6, min_confidence=0.0)

    print("\n[场景2] get_active_instincts 查询 top 6")
    await bench("get_active_instincts", op_query, iterations=50)
    await db.close()

    # ── 场景 3: merge_duplicates（200 条，分批 MAX_MERGE_BATCH=200）──
    db = await setup_db(seed_bulk=True)
    mgr = InstinctManager(db=db, router=MockRouter())
    mgr._available = True

    async def op_merge():
        await mgr.merge_duplicates()

    print("\n[场景3] merge_duplicates 206条去重（MAX_MERGE_BATCH=200）")
    await bench("merge_duplicates", op_merge, iterations=10)
    await db.close()

    # ── 场景 4: extract_instincts 解析+去重+插入+CORRECT ──
    db = await setup_db()
    mgr = InstinctManager(db=db, router=MockRouter())
    mgr._available = True

    async def mock_llm(messages, **kwargs):
        return (
            "NEW | 用户偏好深夜编程 | 0.8\n"
            "NEW | 用户喜欢简洁回复 | 0.7\n"
            "CORRECT | 用户喜欢被打断时继续说 | archive\n"
            "NEW | 用户偏好浓香入味的菜品 | 0.85\n"
        )

    mgr._call_free_model = mock_llm

    async def op_extract():
        await mgr.extract_instincts("用户说了一些话", "助手回复了", "bench_session")

    print("\n[场景4] extract_instincts 解析+去重+插入+CORRECT修正")
    await bench("extract_instincts", op_extract, iterations=20)
    await db.close()

    # ── 场景 5: archive_stale 归档 ──
    db = await setup_db(seed_bulk=True)
    mgr = InstinctManager(db=db, router=MockRouter())
    mgr._available = True

    async def op_archive():
        await mgr.archive_stale(max_age_days=7)

    print("\n[场景5] archive_stale 归档过期本能（206条）")
    await bench("archive_stale", op_archive, iterations=10)
    await db.close()

    # ── 场景 6: 端到端单轮对话（build_prompt + correct + extract）──
    db = await setup_db()
    mgr = InstinctManager(db=db, router=MockRouter())
    mgr._available = True

    async def mock_llm2(messages, **kwargs):
        return "NEW | 用户偏好深夜编程 | 0.8\nCORRECT | 用户喜欢被打断时继续说 | demote\n"

    mgr._call_free_model = mock_llm2

    async def op_e2e():
        await mgr.get_active_instincts(limit=6, min_confidence=0.0)
        await mgr.correct_instinct("用户喜欢被打断时继续说", "demote")
        await mgr.extract_instincts("用户说了一些话", "助手回复了", "e2e_session")

    print("\n[场景6] 端到端单轮（查询+修正+提取，模拟一轮后台任务）")
    await bench("e2e_single_turn", op_e2e, iterations=20)
    await db.close()

    # ── 场景 7: 4714 条生产规模压测（之前 difflib 要 313 秒的规模）──
    db = await setup_db()
    now = time.time()
    bulk_data = [
        (f"用户偏好类型{i}号行为模式变体{i % 10}", 0.3 + (i % 7) * 0.05, now, now)
        for i in range(4708)  # +6 初始 = 4714
    ]
    await db.executemany(
        "INSERT INTO instincts (content, confidence, status, created_at, last_used_at, use_count) "
        "VALUES (?, ?, 'active', ?, ?, 0)",
        bulk_data,
    )
    await db.commit()
    mgr = InstinctManager(db=db, router=MockRouter())
    mgr._available = True

    async def op_production():
        await mgr.get_active_instincts(limit=6, min_confidence=0.0)
        await mgr.archive_stale(max_age_days=7)
        await mgr.merge_duplicates()

    print("\n[场景7] 生产规模 4714条（查询+归档+去重，之前 difflib 要 313s）")
    await bench("production_4714", op_production, iterations=5)
    await db.close()

    # ── 场景 8: learning_feedback.record（同步方法在 async 调用，_lessons 空）──
    from core.learning_feedback import LearningFeedbackLoop, LearningEvent, EventType, Lesson
    lf_loop = LearningFeedbackLoop(persist_path=None)

    async def op_lf_record_empty():
        lf_loop.record(LearningEvent(
            event_type=EventType.SUCCESS,
            task_description="test task",
            approach_used="test approach",
            outcome="ok",
        ))

    print("\n[场景8] learning_feedback.record（_lessons 空，同步方法在 async 调用）")
    await bench("lf_record_empty", op_lf_record_empty, iterations=50)

    # ── 场景 9: learning_feedback.record（_lessons 200 条，O(n)+rapidfuzz）──
    lf_loop2 = LearningFeedbackLoop(persist_path=None)
    lf_loop2._lessons = [
        Lesson(content=f"教训{i}号: 某操作失败的处理方式变体{i % 5}",
               event_type=EventType.FAILURE)
        for i in range(200)
    ]

    async def op_lf_record_200():
        lf_loop2.record(LearningEvent(
            event_type=EventType.FAILURE,
            task_description="test task",
            approach_used="test approach",
            outcome="failed: 某操作失败的处理方式",
        ))

    print("\n[场景9] learning_feedback.record（_lessons 200条，O(n)+rapidfuzz）")
    await bench("lf_record_200", op_lf_record_200, iterations=20)

    # ── 场景 10: spreading_activation._semantic_rerank（15次 rapidfuzz）──
    from memory.spreading_activation import SpreadingActivationEngine

    class MockConceptDB:
        async def get_node(self, nid):
            return {"id": nid, "text": f"节点{nid}的文本内容描述"}

    sa = SpreadingActivationEngine(concept_db=MockConceptDB(), vector_store=None,
                                    key_extractor=None)
    fused = {i: 1.0 / (60 + i) for i in range(15)}

    async def op_sa_rerank():
        await sa._semantic_rerank("查询文本", fused, top_k=5)

    print("\n[场景10] spreading_activation._semantic_rerank（15次 rapidfuzz）")
    await bench("sa_rerank", op_sa_rerank, iterations=50)

    # ── 场景 11: jieba 分词 to_thread 包裹（验证不阻塞）──
    import jieba
    jieba.initialize()

    async def op_jieba_threaded():
        text = "用户喜欢被打断时继续说的偏好行为模式描述" * 5
        await asyncio.to_thread(lambda: list(jieba.cut_for_search(text)))

    print("\n[场景11] jieba 分词 to_thread 包裹（不阻塞事件循环）")
    await bench("jieba_to_thread", op_jieba_threaded, iterations=30)

    # ── 场景 12: jieba 分词直接调用（对比：会阻塞）──
    async def op_jieba_blocking():
        text = "用户喜欢被打断时继续说的偏好行为模式描述" * 5
        list(jieba.cut_for_search(text))

    print("\n[场景12] jieba 分词直接调用（对比：会阻塞事件循环）")
    await bench("jieba_blocking", op_jieba_blocking, iterations=30)

    # ── 场景 13: xp_system.add_chat_xp（to_thread 隔离，写 JSON 不阻塞）──
    from pathlib import Path
    from core.xp_system import XPSystem
    import tempfile
    with tempfile.TemporaryDirectory() as _xp_tmp:
        _xp_bench = XPSystem(data_dir=Path(_xp_tmp))

        async def op_xp_threaded():
            await asyncio.to_thread(_xp_bench.add_chat_xp, "bench_user", 50)

        print("\n[场景13] xp_system.add_chat_xp（to_thread 隔离，写 JSON）")
        await bench("xp_to_thread", op_xp_threaded, iterations=30)

    # ── 场景 14: learning_loop.process_correction（to_thread 隔离 _persist）──
    from core.learning_loop import LearningLoop
    with tempfile.TemporaryDirectory() as _ll_tmp:
        _ll = LearningLoop(persist_path=Path(_ll_tmp) / "constraints.json")

        async def op_ll_correct():
            await _ll.process_correction("不要这样做", "好的我改")

        print("\n[场景14] learning_loop.process_correction（to_thread 隔离 _persist）")
        await bench("ll_correct_to_thread", op_ll_correct, iterations=30)

    print("\n" + "=" * 64)
    print("  评判标准：心跳 max < 20ms 流畅 | 20-50ms 轻微 | >50ms 阻塞")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
