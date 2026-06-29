"""行为健康评分 + Zombie 进程检测 单元测试 — Dr2 P1 Doctor

测试覆盖:
- BehavioralHealthScorer: 5 级评分, 建议, 维度评分
- ZombieDetector: 心跳超时, 重复行为, 综合检测
"""
import time

import pytest

from core.behavioral_health import (
    BehavioralHealthScorer,
    HealthLevel,
    HealthScore,
)
from core.zombie_detector import ZombieDetector, ZombieProcess


# ── BehavioralHealthScorer ──


def test_excellent_score():
    """全优指标得 5 分 (EXCELLENT)"""
    scorer = BehavioralHealthScorer()
    metrics = {
        "p50_latency_ms": 500,
        "p99_latency_ms": 800,
        "success_rate": 0.97,
        "error_rate": 0.005,
        "memory_usage": 0.40,
        "tool_success_rate": 0.98,
    }
    score = scorer.calculate(metrics)
    assert score.score == 5
    assert score.level == HealthLevel.EXCELLENT
    # 所有维度都被采集
    assert len(score.factors) == 6
    assert score.factors["p99_latency_ms"] == 800


def test_poor_score():
    """差指标得低分 (POOR / CRITICAL)"""
    scorer = BehavioralHealthScorer()
    metrics = {
        "p50_latency_ms": 15000,
        "p99_latency_ms": 20000,
        "success_rate": 0.50,
        "error_rate": 0.50,
        "memory_usage": 0.97,
        "tool_success_rate": 0.50,
    }
    score = scorer.calculate(metrics)
    assert score.score <= 2
    assert score.level in (HealthLevel.POOR, HealthLevel.CRITICAL)


def test_recommendations():
    """建议正确生成 (CRITICAL 级别应包含可执行建议)"""
    scorer = BehavioralHealthScorer()
    metrics = {
        "p99_latency_ms": 12000,        # >10s → 1
        "success_rate": 0.50,           # <70% → 1
        "error_rate": 0.40,             # >20% → 1
        "memory_usage": 0.97,           # >95% → 1
        "tool_success_rate": 0.50,      # <70% → 1
    }
    score = scorer.calculate(metrics)
    recs = scorer.get_recommendations(score)
    assert isinstance(recs, list)
    assert len(recs) > 0
    # CRITICAL 级别应包含至少一条带 "立即" 或 "escalate" 的建议
    assert any("立即" in r or "escalate" in r.lower() or "介入" in r
                for r in recs), f"recommendations missing escalation hint: {recs}"
    # 各维度低分应被点名
    assert any("p99_latency_ms" in r for r in recs)


def test_health_level_is_intenum():
    """验证 HealthLevel 是 IntEnum, 支持数值比较"""
    assert HealthLevel.EXCELLENT == 5
    assert HealthLevel.CRITICAL == 1
    assert HealthLevel.EXCELLENT > HealthLevel.GOOD > HealthLevel.FAIR > HealthLevel.POOR > HealthLevel.CRITICAL
    # IntEnum 可与 int 比较
    assert HealthLevel.FAIR >= 3


# ── ZombieDetector ──


def test_zombie_heartbeat_timeout():
    """心跳超时检测: 超过 timeout 未收到心跳"""
    det = ZombieDetector()
    det.register_process(pid=99998, name="dead_worker", timeout=0.1)
    det.check_heartbeat(99998)
    # 等待超过 timeout
    time.sleep(0.25)
    zombies = det.detect_zombies()
    assert len(zombies) == 1
    z = zombies[0]
    assert z.pid == 99998
    assert z.name == "dead_worker"
    assert "心跳超时" in z.reason
    assert isinstance(z, ZombieProcess)
    assert z.duration > 0


def test_zombie_repetitive_activity():
    """重复行为检测: 连续 N 次相同活动视为死循环"""
    det = ZombieDetector(repetition_threshold=5)
    # 用大 timeout, 避免心跳超时干扰本测试
    det.register_process(pid=99999, name="loop_worker", timeout=100)
    det.check_heartbeat(99999)
    # 记录 5 次相同活动
    for _ in range(5):
        det.record_activity(99999, "loop_action")
    zombies = det.detect_zombies()
    assert len(zombies) == 1
    z = zombies[0]
    assert z.name == "loop_worker"
    assert "重复行为" in z.reason
    assert "loop_action" in z.reason
    assert z.last_activity == "loop_action"


def test_zombie_repetitive_activity_below_threshold():
    """重复行为阈值未达: 不应报告 (确保阈值边界正确)"""
    det = ZombieDetector(repetition_threshold=5)
    det.register_process(pid=99999, name="normal_worker", timeout=100)
    det.check_heartbeat(99999)
    # 只记录 4 次 (< 5)
    for _ in range(4):
        det.record_activity(99999, "ok_action")
    zombies = det.detect_zombies()
    # 心跳正常, 重复行为未达阈值 → 无 zombie
    assert len(zombies) == 0


def test_zombie_detection():
    """综合 zombie 检测: 同时具备心跳超时 + 重复行为"""
    det = ZombieDetector(repetition_threshold=5)
    det.register_process(pid=99997, name="bad_worker", timeout=0.1)
    det.check_heartbeat(99997)
    # 记录 5 次相同活动
    for _ in range(5):
        det.record_activity(99997, "stuck_op")
    # 等待超过 timeout
    time.sleep(0.25)
    zombies = det.detect_zombies()
    assert len(zombies) >= 1
    z = zombies[0]
    assert z.pid == 99997
    assert z.name == "bad_worker"
    # 综合检测: 心跳超时和重复行为至少有一个
    assert "心跳超时" in z.reason or "重复行为" in z.reason
    # 若两者都触发, reason 应包含两条
    if "心跳超时" in z.reason and "重复行为" in z.reason:
        assert "; " in z.reason


def test_zombie_detector_kill_unregistered():
    """kill_zombie 对未注册 pid 返回 False"""
    det = ZombieDetector()
    assert det.kill_zombie(88888) is False


def test_zombie_detector_get_status():
    """get_status 返回监控状态"""
    det = ZombieDetector(repetition_threshold=3)
    det.register_process(pid=10001, name="p1", timeout=30)
    det.register_process(pid=10002, name="p2", timeout=60)
    status = det.get_status()
    assert status["monitored_count"] == 2
    assert 10001 in status["pids"]
    assert 10002 in status["pids"]
    assert status["repetition_threshold"] == 3


def test_doctor_integration_includes_bhs_and_zombie_checks():
    """集成测试: doctor 默认检查包含 BHS 和 Zombie 检测项"""
    from core.doctor import _create_default_doctor
    doc = _create_default_doctor()
    names = [c["name"] for c in doc._checks]
    assert "Behavioral Health" in names
    assert "Zombie Processes" in names
    # 验证层级
    layers = {c["name"]: c["layer"] for c in doc._checks}
    assert layers["Behavioral Health"] == "L7-Behavior"
    assert layers["Zombie Processes"] == "L8-Zombie"
