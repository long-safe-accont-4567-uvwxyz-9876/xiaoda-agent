"""I7: 子 Agent 工作履历 — 单元测试

覆盖:
- AgentWorkRecord.record / get_stats / get_best_agent
- 冷启动保护 (样本 < 3 的 agent 给 0.5 基础分)
- 持久化 (JSON 文件读写)
- FIFO 上限 (500 条)
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_recorder(tmp_path):
    """用临时目录隔离持久化文件"""
    from core.agent_work_record import AgentWorkRecord
    return AgentWorkRecord(persist_path=tmp_path / "work_records.json")


# ============================================================
# record + get_stats
# ============================================================

def test_record_and_stats_success(tmp_recorder):
    """记录成功委派后统计应反映"""
    tmp_recorder.record("xiaoke", "frontend", success=True, duration=1.5)
    tmp_recorder.record("xiaoke", "frontend", success=True, duration=2.0)
    tmp_recorder.record("xiaoke", "frontend", success=False, duration=3.0)
    stats = tmp_recorder.get_stats("xiaoke", "frontend")
    assert stats["total"] == 3
    assert stats["success_rate"] == pytest.approx(2 / 3)
    assert stats["avg_duration"] == pytest.approx(2.167, abs=0.01)


def test_stats_no_records(tmp_recorder):
    """无记录时返回零值统计"""
    stats = tmp_recorder.get_stats("unknown_agent")
    assert stats["total"] == 0
    assert stats["success_rate"] == 0.0


def test_stats_filter_by_task_type(tmp_recorder):
    """按 task_type 过滤统计"""
    tmp_recorder.record("xiaoke", "frontend", success=True)
    tmp_recorder.record("xiaoke", "backend", success=False)
    frontend_stats = tmp_recorder.get_stats("xiaoke", "frontend")
    assert frontend_stats["total"] == 1
    assert frontend_stats["success_rate"] == 1.0
    backend_stats = tmp_recorder.get_stats("xiaoke", "backend")
    assert backend_stats["total"] == 1
    assert backend_stats["success_rate"] == 0.0
    all_stats = tmp_recorder.get_stats("xiaoke")
    assert all_stats["total"] == 2


# ============================================================
# get_best_agent + 冷启动保护
# ============================================================

def test_best_agent_picks_higher_success_rate(tmp_recorder):
    """应选成功率更高的 agent"""
    # xiaoke: 4/5 = 80%
    for _ in range(4):
        tmp_recorder.record("xiaoke", "frontend", success=True)
    tmp_recorder.record("xiaoke", "frontend", success=False)
    # xiaolang: 2/5 = 40%
    for _ in range(2):
        tmp_recorder.record("xiaolang", "frontend", success=True)
    for _ in range(3):
        tmp_recorder.record("xiaolang", "frontend", success=False)
    best = tmp_recorder.get_best_agent(["xiaoke", "xiaolang"], "frontend")
    assert best == "xiaoke"


def test_best_agent_cold_start_protection(tmp_recorder):
    """冷启动: 样本 < 3 的 agent 给 0.5 基础分, 不被饿死"""
    # xiaolang 有 5 次失败记录 (success_rate=0), xiaoke 无记录 (冷启动 0.5)
    for _ in range(5):
        tmp_recorder.record("xiaolang", "frontend", success=False)
    best = tmp_recorder.get_best_agent(["xiaolang", "xiaoke"], "frontend")
    # xiaoke 冷启动 0.5 > xiaolang 0.0, 应选 xiaoke
    assert best == "xiaoke"


def test_best_agent_empty_candidates(tmp_recorder):
    """空候选列表返回 None"""
    assert tmp_recorder.get_best_agent([], "frontend") is None


def test_best_agent_single_candidate(tmp_recorder):
    """单候选直接返回"""
    assert tmp_recorder.get_best_agent(["xiaoke"], "frontend") == "xiaoke"


# ============================================================
# 持久化
# ============================================================

def test_persist_and_reload(tmp_path):
    """记录应持久化到 JSON, 重启后可加载"""
    from core.agent_work_record import AgentWorkRecord
    path = tmp_path / "work_records.json"
    r1 = AgentWorkRecord(persist_path=path)
    r1.record("xiaoke", "frontend", success=True, duration=1.2)
    # 新实例应能加载之前的记录
    r2 = AgentWorkRecord(persist_path=path)
    stats = r2.get_stats("xiaoke", "frontend")
    assert stats["total"] == 1
    assert stats["success_rate"] == 1.0


def test_fifo_limit(tmp_path):
    """超过 500 条时 FIFO 淘汰最旧的"""
    from core.agent_work_record import AgentWorkRecord
    path = tmp_path / "work_records.json"
    r = AgentWorkRecord(persist_path=path)
    for i in range(550):
        r.record("xiaoke", "frontend", success=True, duration=float(i))
    stats = r.get_stats("xiaoke", "frontend")
    assert stats["total"] == 500  # 被截断到 500
    # 最旧的 (duration=0) 应已被淘汰, 最早的应是 duration=50
    assert r._records[0]["duration"] == 50.0


# ============================================================
# 单例
# ============================================================

def test_singleton():
    """get_work_recorder 应返回同一实例"""
    from core.agent_work_record import get_work_recorder
    r1 = get_work_recorder()
    r2 = get_work_recorder()
    assert r1 is r2
