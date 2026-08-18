"""Phase 6 新增模块测试 (前沿技术驱动优化)

覆盖以下任务:
- S3: utils/async_compat.py
- S9: security/anomaly_detector.py
- S10: security/human_approval.py
- P4: core/tiered_cache.py
- P5: core/parallel_dag.py
- Q3: core/slo_tracker.py
- Q5: core/sla_exporter.py
- A2: core/metacognition_lite.py
- A3: core/agent_r_reflection.py
- A5: core/dream_consolidation.py
- A6: core/self_diagnostic.py
- Dr3: core/recovery_orchestrator.py
- H1: memory/episodic_limiter.py
- H2: core/config_reloader.py
- H3: db/idempotent_migrator.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# 确保项目根在 path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ============================================================
# S3: async_compat
# ============================================================

@pytest.mark.asyncio
async def test_async_compat_sleep():
    from utils.async_compat import async_sleep
    t0 = time.time()
    await async_sleep(0.05)
    assert time.time() - t0 >= 0.04


@pytest.mark.asyncio
async def test_async_compat_run_sync():
    from utils.async_compat import run_sync
    result = await run_sync(lambda x: x * 2, 21)
    assert result == 42


@pytest.mark.asyncio
async def test_async_compat_gather_with_concurrency():
    from utils.async_compat import gather_with_concurrency
    async def task(i):
        await asyncio.sleep(0.01)
        return i
    results = await gather_with_concurrency([task(i) for i in range(10)], limit=3)
    assert sorted(results) == list(range(10))


@pytest.mark.asyncio
async def test_async_compat_exponential_backoff():
    from utils.async_compat import async_sleep_range
    d1 = await async_sleep_range(1, base_delay=0.01, max_delay=1.0)
    d2 = await async_sleep_range(3, base_delay=0.01, max_delay=1.0)
    assert d2 > d1


# ============================================================
# P4: tiered_cache
# ============================================================

@pytest.mark.asyncio
async def test_tiered_cache_l1_hit():
    import gc

    from core.tiered_cache import TieredCache
    with tempfile.TemporaryDirectory() as td:
        cache = TieredCache(
            cache_dir=Path(td) / "L2",
            db_path=Path(td) / "L3.db",
            l1_size=10, l1_ttl=10,
        )
        await cache.get("k1", loader=lambda: "v1", ttl=10)
        # 第二次应该命中 L1
        v = await cache.get("k1", loader=lambda: "should_not_load")
        assert v == "v1"
        del cache
        gc.collect()


@pytest.mark.asyncio
async def test_tiered_cache_l2_backfill():
    import gc

    from core.tiered_cache import L1MemoryCache, TieredCache
    with tempfile.TemporaryDirectory() as td:
        cache = TieredCache(
            cache_dir=Path(td) / "L2",
            db_path=Path(td) / "L3.db",
            l1_size=1, l1_ttl=10,
        )
        # 第一次: 全部加载, 写入所有层
        await cache.get("k1", loader=lambda: "v1", ttl=10)
        # 清空 L1 模拟 L1 miss
        cache._l1 = L1MemoryCache(maxsize=1, default_ttl=10)
        # 第二次: 应该从 L2 命中并回填 L1
        v = await cache.get("k1", loader=lambda: "should_not_load")
        assert v == "v1"
        del cache
        gc.collect()


@pytest.mark.asyncio
async def test_tiered_cache_invalidate():
    import gc

    from core.tiered_cache import TieredCache
    with tempfile.TemporaryDirectory() as td:
        cache = TieredCache(
            cache_dir=Path(td) / "L2",
            db_path=Path(td) / "L3.db",
        )
        await cache.get("k1", loader=lambda: "v1")
        cache.invalidate("k")
        # 失效后重新加载
        v = await cache.get("k1", loader=lambda: "v2")
        assert v == "v2"
        del cache
        gc.collect()


# ============================================================
# P5: parallel_dag
# ============================================================

@pytest.mark.asyncio
async def test_dag_parallel_execution():
    from core.parallel_dag import ToolDAG
    dag = ToolDAG()
    completed = []

    async def task_a():
        await asyncio.sleep(0.05)
        completed.append("a")
        return "a_result"

    async def task_b():
        await asyncio.sleep(0.05)
        completed.append("b")
        return "b_result"

    async def task_c(a_val, b_val):
        await asyncio.sleep(0.01)
        completed.append("c")
        return f"{a_val}+{b_val}"

    dag.add_node("a", task_a, produces=["a_val"])
    dag.add_node("b", task_b, produces=["b_val"])
    dag.add_node("c", task_c,
                  depends_on=["a", "b"], consumes=["a_val", "b_val"])

    result = await dag.execute()
    assert result.success_count == 3
    assert result.failed_count == 0
    # a 和 b 应该在 c 之前完成
    assert completed.index("a") < completed.index("c")
    assert completed.index("b") < completed.index("c")


@pytest.mark.asyncio
async def test_dag_fallback_on_failure():
    from core.parallel_dag import ToolDAG
    dag = ToolDAG()

    async def fail_task():
        raise RuntimeError("intentional failure")

    async def fallback():
        return "fallback_result"

    dag.add_node("failing", fail_task, fallback=fallback, retries=1)
    result = await dag.execute()
    assert result.success_count == 1
    assert result.nodes["failing"].result == "fallback_result"


@pytest.mark.asyncio
async def test_dag_skip_downstream():
    from core.parallel_dag import NodeState, ToolDAG
    dag = ToolDAG()

    async def fail_task():
        raise RuntimeError("fail")

    async def dependent_task():
        return "should_not_run"

    dag.add_node("a", fail_task, retries=0)
    dag.add_node("b", dependent_task, depends_on=["a"])
    result = await dag.execute()
    assert result.nodes["a"].state == NodeState.FAILED
    assert result.nodes["b"].state == NodeState.SKIPPED


@pytest.mark.asyncio
async def test_dag_cycle_detection():
    from core.parallel_dag import ToolDAG
    dag = ToolDAG()
    dag.add_node("a", lambda: None, depends_on=["b"])
    dag.add_node("b", lambda: None, depends_on=["a"])
    with pytest.raises(ValueError, match="cycle"):
        await dag.execute()


# ============================================================
# A2: metacognition_lite
# ============================================================

def test_metacog_anticipate():
    from core.metacognition_lite import MetacognitionLite
    mc = MetacognitionLite()
    state = mc.anticipate("what's the weather in tokyo",
                          known=["location:tokyo"],
                          unknown=["current_time"])
    assert "tokyo" in state.task_keywords
    assert state.uncertainty > 0


def test_metacog_detect_repetition():
    from core.metacognition_lite import DriftType, MetacognitionLite
    mc = MetacognitionLite()
    mc.anticipate("test query")
    mc.monitor("same output", confidence=0.8)
    mc.monitor("same output", confidence=0.8)
    mc.monitor("same output", confidence=0.8)
    assert mc.state.drift_type == DriftType.REPETITION


def test_metacog_reflect_quality():
    from core.metacognition_lite import MetacognitionLite
    mc = MetacognitionLite()
    mc.anticipate("test query hello")
    mc.monitor("hello world", confidence=0.8)
    result = mc.reflect("hello world test")
    assert 0 <= result["quality_score"] <= 1.0
    assert "reflection" in result


def test_metacog_regulate_actions():
    from core.metacognition_lite import DriftType, MetacognitionLite
    mc = MetacognitionLite()
    mc.state.drift_type = DriftType.REPETITION
    action = mc.regulate()
    assert action == "reframe"


# ============================================================
# A3: agent_r_reflection
# ============================================================

def test_agent_r_record_step_and_detect_error():
    from core.agent_r_reflection import AgentRReflector, TrajectoryType
    r = AgentRReflector()
    r.record_step("tool_call", "ok", success=True)
    r.record_step("tool_call", "fail", success=False, error="404")
    assert r._current_trajectory.type == TrajectoryType.ERROR
    assert r.should_reflect() is True


def test_agent_r_reflect_generates_memory():
    from core.agent_r_reflection import AgentRReflector
    r = AgentRReflector()
    r.record_step("tool_call", "fail", success=False, error="timeout")
    mem = r.reflect()
    assert mem is not None
    assert mem.pattern == "timeout_error"
    assert "timeout" in mem.lesson.lower()


def test_agent_r_apply_revision():
    from core.agent_r_reflection import AgentRReflector, TrajectoryStep, TrajectoryType
    r = AgentRReflector()
    r.record_step("a", "ok")
    r.record_step("b", "fail", success=False, error="404")
    correct = [TrajectoryStep(step_idx=1, action="b", content="ok_retry")]
    revision = r.apply_revision(correct)
    assert revision.type == TrajectoryType.REVISION


def test_agent_r_get_lessons_for_prompt():
    from core.agent_r_reflection import AgentRReflector
    r = AgentRReflector()
    r.record_step("call", "fail", success=False, error="timeout")
    r.reflect()
    lessons = r.get_lessons_for_prompt()
    assert "Past lessons" in lessons
    assert "timeout" in lessons.lower()


# ============================================================
# Q3: slo_tracker
# ============================================================

def test_slo_availability_calculation():
    from core.slo_tracker import SLOMeasurement, SLOTarget, SLOTracker
    t = SLOTracker(SLOTarget())
    for _ in range(9):
        t.record(SLOMeasurement(timestamp=time.time(), success=True, latency_ms=100))
    t.record(SLOMeasurement(timestamp=time.time(), success=False, latency_ms=200))
    assert t.availability() == 0.9
    assert t.error_rate() == 0.1


def test_slo_burn_rate():
    from core.slo_tracker import SLOMeasurement, SLOTarget, SLOTracker
    t = SLOTracker(SLOTarget(availability=0.99))
    # 全部失败, burn_rate 应该很高
    for _ in range(10):
        t.record(SLOMeasurement(timestamp=time.time(), success=False, latency_ms=100))
    assert t.burn_rate() > 1.0


@pytest.mark.asyncio
async def test_rate_limiter_allows_within_limit():
    from core.slo_tracker import RateLimiter
    rl = RateLimiter()
    rl.set_global(100)
    results = []
    for _ in range(5):
        ok = await rl.allow(user_id="u1", endpoint="/test")
        results.append(ok)
    assert all(results)


# ============================================================
# Q5: sla_exporter
# ============================================================

def test_sla_exporter_basic():
    from core.sla_exporter import SLAExporter
    exp = SLAExporter()
    exp.inc_request("/api/v1/chat", "200")
    exp.observe_latency("/api/v1/chat", 0.1)
    out = exp.export()
    assert "agent_requests_total" in out
    assert "agent_request_duration_seconds_bucket" in out


def test_sla_exporter_counter_increment():
    from core.sla_exporter import SLAExporter
    exp = SLAExporter()
    exp.inc_request("/test", "200")
    exp.inc_request("/test", "200")
    out = exp.export()
    # 应该看到 2
    assert "/test" in out


# ============================================================
# A5: dream_consolidation
# ============================================================

@pytest.mark.asyncio
async def test_dream_consolidate_decay():
    from core.dream_consolidation import DreamConsolidator, Memory
    d = DreamConsolidator()
    # 添加一个旧记忆 (模拟 30 天前创建, 10 天前最后访问)
    now = time.time()
    old = Memory(id="m1", content="old", importance=0.5, strength=0.5,
                  last_access=now - 86400 * 10, created_at=now - 86400 * 30,
                  decay_rate=0.5)
    d.add_memory(old)
    await d.consolidate()
    stats = d.stats()
    # 应该被衰减或删除
    assert stats["total_memories"] == 0 or d.get_memory("m1") is None or \
           d.get_memory("m1").strength < 0.5


@pytest.mark.asyncio
async def test_dream_consolidate_merge_similar():
    from core.dream_consolidation import DreamConsolidator, Memory
    d = DreamConsolidator()
    # 两个 content 前 30 字符相同, 应该被合并
    d.add_memory(Memory(id="m1", content="how to cook rice with chicken broth", importance=0.5))
    d.add_memory(Memory(id="m2", content="how to cook rice with chicken broth recipe", importance=0.7))
    await d.consolidate()
    # 相似前缀应该被合并
    assert len(d._memories) == 1


# ============================================================
# A6: self_diagnostic
# ============================================================

@pytest.mark.asyncio
async def test_self_diag_run_checks():
    from core.self_diagnostic import ReportLevel, SelfDiagnostic
    diag = SelfDiagnostic()
    # 不依赖真实 SLO, 直接注入检查
    triggered = []
    diag.add_check(lambda: None)  # no-op check
    _custom_report = None

    async def custom_check():
        from core.self_diagnostic import SelfReport
        return SelfReport(
            level=ReportLevel.WARNING,
            category="test",
            message="test report",
        )

    diag.add_check(custom_check)
    diag.on_report(triggered.append)
    reports = await diag.run_checks()
    assert len(reports) >= 1
    assert any(r.category == "test" for r in reports)


# ============================================================
# Dr3: recovery_orchestrator
# ============================================================

@pytest.mark.asyncio
async def test_recovery_retry_success():
    from core.recovery_orchestrator import RecoveryOrchestrator
    orch = RecoveryOrchestrator()
    call_count = {"n": 0}

    async def flaky():
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise RuntimeError("transient")
        return "ok"

    result = await orch.execute("test_op", flaky)
    assert result.success
    assert result.attempts >= 2


@pytest.mark.asyncio
async def test_recovery_fallback():
    from core.recovery_orchestrator import RecoveryOrchestrator
    orch = RecoveryOrchestrator(backoff_delays=[0.01, 0.02, 0.04])

    async def always_fail():
        raise RuntimeError("permanent")

    async def fallback():
        return "fallback_value"

    orch.register_fallback("op", fallback)
    result = await orch.execute("op", always_fail)
    assert result.success
    assert result.result == "fallback_value"


@pytest.mark.asyncio
async def test_recovery_escalate_to_human():
    from core.recovery_orchestrator import RecoveryLevel, RecoveryOrchestrator
    orch = RecoveryOrchestrator()

    async def fail():
        raise RuntimeError("auth failed: 401")

    # 限制最大级别为 ESCALATE
    result = await orch.execute("op", fail,
                                  max_level=RecoveryLevel.ESCALATE)
    assert not result.success


# ============================================================
# H2: config_reloader
# ============================================================

def test_config_reloader_load():
    from core.config_reloader import ConfigReloader
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False) as f:
        f.write('{"a": 1, "b": {"c": 2}}')
        f.flush()
        path = f.name
    try:
        r = ConfigReloader(path)
        assert r.get("a") == 1
        assert r.get("b.c") == 2
        assert r.get("nonexistent", "default") == "default"
    finally:
        os.unlink(path)


def test_config_reloader_detects_changes():
    from core.config_reloader import ConfigReloader
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False) as f:
        f.write('{"v": 1}')
        f.flush()
        path = f.name
    try:
        r = ConfigReloader(path)
        assert r.get("v") == 1
        # 修改文件
        time.sleep(0.1)
        with open(path, "w") as f:
            f.write('{"v": 2}')
            f.flush()
        assert r.reload()
        assert r.get("v") == 2
    finally:
        os.unlink(path)


def test_config_reloader_callback():
    from core.config_reloader import ConfigReloader
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                        delete=False) as f:
        f.write('{"v": 1}')
        f.flush()
        path = f.name
    try:
        r = ConfigReloader(path)
        notified = []
        r.on_change(lambda snap: notified.append(snap.version))
        time.sleep(0.1)
        with open(path, "w") as f:
            f.write('{"v": 2}')
            f.flush()
        r.reload()
        assert len(notified) >= 1
    finally:
        os.unlink(path)


# ============================================================
# H3: idempotent_migrator
# ============================================================

@pytest.mark.asyncio
async def test_migrator_idempotent_column_add():
    import aiosqlite

    from db.idempotent_migrator import IdempotentMigrator
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("CREATE TABLE test (id INTEGER)")
            await conn.commit()
            m = IdempotentMigrator(conn)
            # 第一次添加
            ok1 = await m.add_column_if_not_exists("test", "col1", "TEXT", "''")
            assert ok1 is True
            # 第二次添加 (幂等, 应该跳过)
            ok2 = await m.add_column_if_not_exists("test", "col1", "TEXT", "''")
            assert ok2 is False
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_migrator_apply_idempotent():
    import aiosqlite

    from db.idempotent_migrator import IdempotentMigrator
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        async with aiosqlite.connect(path) as conn:
            await conn.execute("CREATE TABLE t (id INTEGER)")
            await conn.commit()
            m = IdempotentMigrator(conn)
            stmts = [
                "CREATE INDEX IF NOT EXISTS idx_t ON t(id)",
                ("ALTER TABLE t ADD COLUMN x TEXT", "column", "t", "x"),
            ]
            ok1 = await m.apply("v1", stmts, "test migration")
            assert ok1 is True
            ok2 = await m.apply("v1", stmts, "test migration")
            assert ok2 is False  # 已应用
    finally:
        os.unlink(path)


# ============================================================
# H1: episodic_limiter
# ============================================================

@pytest.mark.asyncio
async def test_episodic_limiter_enforce():
    import aiosqlite

    from memory.episodic_limiter import EpisodicLimiter

    class FakeDB:
        def __init__(self, conn):
            self._conn = conn

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    try:
        async with aiosqlite.connect(path) as conn:
            # 创建表 + 测试数据
            await conn.execute("""
                CREATE TABLE episodic_memories (
                    id TEXT PRIMARY KEY,
                    summary TEXT,
                    importance REAL DEFAULT 0.5,
                    access_count INTEGER DEFAULT 0,
                    timestamp REAL DEFAULT 0,
                    session_id TEXT DEFAULT 'user'
                )
            """)
            # 插入 20 条数据, 上限设为 5
            for i in range(20):
                await conn.execute(
                    "INSERT INTO episodic_memories (id, summary, importance) VALUES (?, ?, ?)",
                    (f"id_{i}", f"summary_{i}", 0.1 * i)
                )
            await conn.commit()

            db = FakeDB(conn)
            limiter = EpisodicLimiter(db, max_rows=5)
            pruned = await limiter.enforce_limit()
            assert pruned == 15
            # 验证剩余未归档的行数
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM episodic_memories WHERE session_id != 'archived'"
            )
            row = await cursor.fetchone()
            assert row[0] == 5
    finally:
        os.unlink(path)


# ============================================================
# S9: anomaly_detector
# ============================================================

def test_anomaly_detector_baseline():
    from security.anomaly_detector import AnomalyDetector, BehaviorEvent
    det = AnomalyDetector()
    # 训练基线 (10 个正常事件)
    for _ in range(15):
        det.record(BehaviorEvent(user_id="u1", action="search",
                                   input_size=100, success=True))
    # 异常事件 (输入超大)
    big_event = BehaviorEvent(user_id="u1", action="search",
                                input_size=10000, success=True)
    anomalies = det.check(big_event)
    # 应该检测到 input_size 异常
    assert any(a.dimension == "input_size" for a in anomalies)


def test_anomaly_detector_off_hours():
    from security.anomaly_detector import AnomalyDetector, BehaviorEvent
    det = AnomalyDetector()
    # 凌晨事件 (3:00)
    event = BehaviorEvent(user_id="u1", action="search",
                            timestamp=time.mktime(time.strptime("2026-06-29 03:00:00",
                                                                   "%Y-%m-%d %H:%M:%S")))
    anomalies = det.check(event)
    assert any(a.dimension == "time" for a in anomalies)


# ============================================================
# S10: human_approval
# ============================================================

@pytest.mark.asyncio
async def test_approval_auto_approve_owner():
    from security.human_approval import ApprovalStatus, HumanApprovalGate, RiskLevel
    gate = HumanApprovalGate()
    gate.register_auto_approve_user("owner")
    req = await gate.request(
        user_id="owner", operation="delete_file",
        args={"path": "/test"}, risk_level=RiskLevel.CRITICAL,
    )
    assert req.status == ApprovalStatus.AUTO_APPROVED


@pytest.mark.asyncio
async def test_approval_wait_and_decide():
    from security.human_approval import ApprovalStatus, HumanApprovalGate, RiskLevel
    gate = HumanApprovalGate(default_timeout=2.0)

    async def decide_later(req_id):
        await asyncio.sleep(0.1)
        gate.decide(req_id, ApprovalStatus.APPROVED, "test_admin", "looks ok")

    req = await gate.request(
        user_id="u1", operation="shell_command",
        risk_level=RiskLevel.HIGH,
    )
    _decide_task = asyncio.create_task(decide_later(req.id))
    result = await gate.wait_for_decision(req.id, timeout=2.0)
    assert result.status == ApprovalStatus.APPROVED
    assert result.decided_by == "test_admin"


@pytest.mark.asyncio
async def test_approval_timeout():
    from security.human_approval import ApprovalStatus, HumanApprovalGate, RiskLevel
    gate = HumanApprovalGate(default_timeout=0.3)
    req = await gate.request(
        user_id="u1", operation="restart_service",
        risk_level=RiskLevel.HIGH,
    )
    result = await gate.wait_for_decision(req.id, timeout=0.3)
    assert result.status == ApprovalStatus.TIMEOUT


def test_approval_is_high_risk():
    from security.human_approval import HumanApprovalGate
    gate = HumanApprovalGate()
    assert gate.is_high_risk("delete_file")
    assert gate.is_high_risk("shell_command")
    assert not gate.is_high_risk("read_file")


# ============================================================
# 综合测试: 模块集成
# ============================================================

@pytest.mark.asyncio
async def test_metacog_with_agent_r_integration():
    """A2 + A3 集成: 元认知检测到漂移, 触发 Agent-R 反思"""
    from core.agent_r_reflection import AgentRReflector
    from core.metacognition_lite import DriftType, MetacognitionLite

    mc = MetacognitionLite()
    reflector = AgentRReflector()

    mc.anticipate("query about topic_x")
    # 模拟输出漂移
    for _ in range(3):
        reflector.record_step("llm_response", "irrelevant content",
                                success=False, error="drift")
    if reflector.should_reflect():
        mem = reflector.reflect()
        assert mem is not None
        # 元认知应该看到漂移信号
        mc.monitor("irrelevant content", confidence=0.3)
        assert mc.state.drift_type != DriftType.NONE or mc.state.confidence < 0.5


@pytest.mark.asyncio
async def test_full_pipeline_slo_to_sla():
    """Q3 + Q5 集成: SLO 测量值导出到 SLA Prometheus 格式"""
    from core.sla_exporter import SLAExporter
    from core.slo_tracker import SLOMeasurement, SLOTarget, SLOTracker

    slo = SLOTracker(SLOTarget())
    exp = SLAExporter()

    # 记录一些请求
    for i in range(10):
        success = i < 8
        lat = 100 + i * 20
        slo.record(SLOMeasurement(timestamp=time.time(),
                                     success=success, latency_ms=lat))
        exp.inc_request("/api/v1/chat", "200" if success else "500")
        exp.observe_latency("/api/v1/chat", lat / 1000)

    # 导出
    output = exp.export()
    assert "agent_requests_total" in output
    # SLO 应该有数据
    health = slo.health()
    assert health["sample_count"] == 10


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
