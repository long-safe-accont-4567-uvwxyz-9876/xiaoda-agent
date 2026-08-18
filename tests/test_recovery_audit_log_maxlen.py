"""G6: recovery_orchestrator audit_log 上限测试."""
from collections import deque

from core.recovery_orchestrator import RecoveryOrchestrator


def test_audit_log_has_maxlen_500():
    """audit_log 应为 deque(maxlen=500)，防止内存泄漏."""
    orch = RecoveryOrchestrator()
    assert isinstance(orch._audit_log, deque)
    assert orch._audit_log.maxlen == 500


def test_audit_log_evicts_old_after_600_events():
    """触发 600 次事件后，audit_log 仅保留最近 500 条."""
    orch = RecoveryOrchestrator()
    for i in range(600):
        orch._audit_log.append({"i": i, "event": "test"})
    assert len(orch._audit_log) == 500
    # 最近 500 条保留 (i=100..599)
    assert orch._audit_log[0]["i"] == 100
    assert orch._audit_log[-1]["i"] == 599


def test_get_audit_log_still_works_with_deque():
    """get_audit_log(limit) 切片语法与 deque 兼容."""
    orch = RecoveryOrchestrator()
    for i in range(10):
        orch._audit_log.append({"i": i})
    result = orch.get_audit_log(limit=3)
    assert len(result) == 3
    assert result[-1]["i"] == 9
