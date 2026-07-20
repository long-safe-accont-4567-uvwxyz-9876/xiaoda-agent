### Task 1: G6 recovery_orchestrator audit_log 上限

**Files:**
- Modify: `core/recovery_orchestrator.py:92`
- Test: `tests/test_recovery_audit_log_maxlen.py`

**Interfaces:**
- Produces: `self._audit_log: deque[dict]` (maxlen=500)，与原 `list` 接口兼容（append/len/[-n:]切片均支持）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_recovery_audit_log_maxlen.py
"""G6: recovery_orchestrator audit_log 上限测试."""
import asyncio
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
    # 最近 500 条保留（i=100..599）
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_recovery_audit_log_maxlen.py -v`
Expected: FAIL with "AssertionError: list is not deque" 或类似

- [ ] **Step 3: 修改实现**

```python
# core/recovery_orchestrator.py
# 第 92 行原：
#     self._audit_log: list[dict] = []
# 改为：
from collections import deque
self._audit_log: deque[dict] = deque(maxlen=500)
```

注意：文件顶部若未 import deque，需添加 `from collections import deque`

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_recovery_audit_log_maxlen.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: 跑回归测试**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_recovery_orchestrator.py tests/test_recovery_audit_log_maxlen.py -v --timeout=60`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
cd /home/orangepi/ai-agent
git add core/recovery_orchestrator.py tests/test_recovery_audit_log_maxlen.py
git commit -m "fix(G6): recovery_orchestrator audit_log 改用 deque(maxlen=500) 防内存泄漏"
```

---

