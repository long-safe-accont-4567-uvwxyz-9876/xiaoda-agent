# Task 1 报告：G6 recovery_orchestrator audit_log 改 deque(maxlen=500)

## 状态

**DONE_WITH_CONCERNS**

任务核心目标（list→deque(maxlen=500) 防 600+ 事件内存泄漏）已实现，3 个新测试全绿、回归测试全绿、提交完成。
存在两处需要 reviewer 知晓的偏离原 brief 的细节（详见"自审发现"），均属必要修复，未引入额外功能。

## 提交 hash

```
e60c59baf5b1d7de080168e41c687ed412cf8d46
```

提交信息：
```
fix(G6): recovery_orchestrator audit_log 改用 deque(maxlen=500) 防内存泄漏
```

涉及文件：
- `core/recovery_orchestrator.py`（修改）
- `tests/test_recovery_audit_log_maxlen.py`（新增）

diff 摘要：
```
2 files changed, 35 insertions(+), 2 deletions(-)
```

## 实现变更

### 变更 1（brief 要求）
`core/recovery_orchestrator.py:26`
```python
+from collections import deque
```

`core/recovery_orchestrator.py:93`
```python
-        self._audit_log: list[dict] = []
+        self._audit_log: deque[dict] = deque(maxlen=500)
```

### 变更 2（brief 未提及，但属必要修复）
`core/recovery_orchestrator.py:358` — `get_audit_log()` 切片改写
```python
-        return self._audit_log[-limit:]
+        return list(self._audit_log)[-limit:]
```
原因：`deque` 不支持切片语法 `deque[-limit:]`，会抛 `TypeError: sequence index must be integer, not 'slice'`。
需先 `list(...)` 转回 list 再切片，以维持原有 `list[dict]` 返回类型契约与切片语义。

## 测试结果

### Step 2 — 失败测试（TDD 红灯）

命令：
```bash
.venv/bin/python -m pytest tests/test_recovery_audit_log_maxlen.py -v --tb=no
```

输出摘要：
```
tests/test_recovery_audit_log_maxlen.py::test_audit_log_has_maxlen_500 FAILED [ 33%]
tests/test_recovery_audit_log_maxlen.py::test_audit_log_evicts_old_after_600_events FAILED [ 66%]
tests/test_recovery_audit_log_maxlen.py::test_get_audit_log_still_works_with_deque PASSED [100%]
========================= 2 failed, 1 passed in 0.18s =========================
```
（前两个测试如期失败，第三个 `get_audit_log` 切片在 list 上也能通过，符合 TDD 预期）

### Step 4 — 修复后单元测试全绿

命令：
```bash
.venv/bin/python -m pytest tests/test_recovery_audit_log_maxlen.py -v --tb=short
```

输出摘要：
```
tests/test_recovery_audit_log_maxlen.py::test_audit_log_has_maxlen_500 PASSED [ 33%]
tests/test_recovery_audit_log_maxlen.py::test_audit_log_evicts_old_after_600_events PASSED [ 66%]
tests/test_recovery_audit_log_maxlen.py::test_get_audit_log_still_works_with_deque PASSED [100%]
============================== 3 passed in 0.23s ===============================
```

### Step 5 — 回归测试

命令（实际执行版本，见"自审发现 #1"说明）：
```bash
.venv/bin/python -m pytest tests/test_phase6_modules.py tests/test_tnr_protocol.py tests/test_recovery_audit_log_maxlen.py -v --timeout=60
```

输出摘要：
```
...
tests/test_phase6_modules.py::test_recovery_retry_success PASSED
tests/test_phase6_modules.py::test_recovery_fallback PASSED
tests/test_phase6_modules.py::test_recovery_escalate_to_human PASSED
...
tests/test_tnr_protocol.py::TestTNRFullFlow::test_tnr_full_flow PASSED
tests/test_tnr_protocol.py::TestFailedRecoveryAlerts::test_failed_recovery_alerts PASSED
tests/test_tnr_protocol.py::TestFailedRecoveryAlerts::test_failed_recovery_alert_history PASSED
...
tests/test_recovery_audit_log_maxlen.py::test_audit_log_has_maxlen_500 PASSED
tests/test_recovery_audit_log_maxlen.py::test_audit_log_evicts_old_after_600_events PASSED
tests/test_recovery_audit_log_maxlen.py::test_get_audit_log_still_works_with_deque PASSED

============================== 56 passed in 2.29s ==============================
```

回归全绿，未破坏任何已有 `RecoveryOrchestrator` 测试。

## 自审发现

### #1 brief 中的回归命令引用了不存在的文件

**问题**：brief Step 5 指定命令为
```bash
python -m pytest tests/test_recovery_orchestrator.py tests/test_recovery_audit_log_maxlen.py -v --timeout=60
```
但 `tests/test_recovery_orchestrator.py` 在仓库中并不存在（确认：`ls tests/test_recovery_orchestrator.py` → `No such file or directory`）。原命令执行结果：
```
ERROR: file or directory not found: tests/test_recovery_orchestrator.py
============================ no tests ran in 0.01s =============================
```

**应对**：以实际包含 `RecoveryOrchestrator` 测试的文件替代，覆盖范围与 brief 意图等价或更广：
- `tests/test_phase6_modules.py`：直接测试 `RecoveryOrchestrator`（`test_recovery_retry_success`、`test_recovery_fallback`、`test_recovery_escalate_to_human`，对应 brief 行 422-472）
- `tests/test_tnr_protocol.py`：通过 `RecoveryOrchestrator` 实例化使用（`test_failed_recovery_alerts` 等 10 个测试）

### #2 brief 的设计假设"`deque` 兼容 `[-limit:]` 切片"是错误的

**问题**：brief 与对应设计文档（`docs/superpowers/specs/2026-07-20-performance-optimization-design.md:251`）均断言：
> `get_audit_log(limit)` 已用 `[-limit:]` 切片，deque 兼容。

但 Python `deque` 不支持切片语法：
```python
>>> from collections import deque
>>> deque([1,2,3])[-2:]
TypeError: sequence index must be integer, not 'slice'
```

TDD 流程中 Step 4 首次跑修复后测试时第三个测试 `test_get_audit_log_still_works_with_deque` 因此报：
```
FAILED ... TypeError: sequence index must be integer, not 'slice'
```

**应对**：将 `get_audit_log` 改为 `return list(self._audit_log)[-limit:]`，先转 list 再切片。此修改：
- 仅在 brief 列出的 `core/recovery_orchestrator.py` 文件内
- 维持原 `list[dict]` 返回类型契约不变
- 维持原 `[-limit:]` 语义不变（取最近 N 条，按时间升序）
- 不引入新功能，仅修复 deque 类型替换的副作用
- 不影响 YAGNI 约束

如 reviewer 认为该修改超出 brief 范围，可考虑：将 `get_audit_log` 的返回类型注解改为 `deque[dict]` 并要求所有调用方适配——但当前调用方均按 list 接口使用，且 brief 强调"与原 `list` 接口兼容"，故推荐保留当前实现。

## 一致性确认

- ✅ Base commit `7e4045e` 之后基于 main 分支前进，最终单 commit `e60c59b`
- ✅ 仅修改 brief 列出的两个文件，未触碰其它文件（其它 modified 文件如 `web/frontend/src/App.vue` 属并行其它任务，未纳入本次提交）
- ✅ TDD 流程：写失败测试 → 跑失败 → 改实现 → 跑通过 → 跑回归
- ✅ 未添加 docstring / 注释 / 额外功能（YAGNI）
- ✅ 单元测试 3 个全绿；回归 56 个全绿
