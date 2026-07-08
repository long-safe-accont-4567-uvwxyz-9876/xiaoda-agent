# Bugfix Round 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 10 bugs found in round 3 scanning — concurrency, security, and resource leak issues

**Architecture:** Add thread-safety locks to global mutable state, fix command injection, encrypt API keys at rest, fix eval sandbox bypass, clean up temp files

**Tech Stack:** Python 3.11+, threading, asyncio, security.credential_vault

## Global Constraints

- All lock additions must use `threading.Lock()` for sync code, `asyncio.Lock()` for async
- API key encryption must use existing `security.credential_vault.encrypt()` 
- No new dependencies
- Each fix must be independently verifiable via `py_compile`

---

### Task 1: _pty_sessions dict thread safety (Bug #20)

**Files:**
- Modify: `web/ws_hub.py`

**Severity:** HIGH — concurrent WebSocket connections can corrupt dict

- [ ] **Step 1: Add threading.Lock for _pty_sessions**

At the top of ws_hub.py near `_pty_sessions` definition, add:
```python
import threading
_pty_sessions_lock = threading.Lock()
```

- [ ] **Step 2: Wrap _pty_sessions mutations with lock**

For all `_pty_sessions[...] = ...` and `_pty_sessions.pop(...)` calls, wrap with:
```python
with _pty_sessions_lock:
    _pty_sessions[term_sid] = {...}
```

For reads like `_pty_sessions.get(...)` and `list(_pty_sessions.keys())`, also wrap with lock.

- [ ] **Step 3: Verify compilation**

Run: `python -m py_compile web/ws_hub.py`

---

### Task 2: hardware_tools cache thread safety (Bug #21)

**Files:**
- Modify: `tools/hardware_tools.py`

**Severity:** MEDIUM — concurrent tool calls can corrupt cache

- [ ] **Step 1: Add threading.Lock for _hw_cache**

Near the `_hw_cache` definition, add:
```python
import threading
_hw_cache_lock = threading.Lock()
```

- [ ] **Step 2: Wrap cache reads/writes with lock**

In `hardware_status()`:
```python
with _hw_cache_lock:
    if _hw_cache is not None and (now - cache_ts) < _HW_CACHE_TTL:
        cached = _hw_cache.get(target)
        if cached is not None:
            return cached

# ... compute result ...

with _hw_cache_lock:
    if _hw_cache is None:
        _hw_cache = {}
    _hw_cache[target] = result
    _hw_cache_ts[target] = now
```

- [ ] **Step 3: Verify compilation**

Run: `python -m py_compile tools/hardware_tools.py`

---

### Task 3: mail_tools agently-cli resolution thread safety (Bug #22)

**Files:**
- Modify: `tools/mail_tools.py`

**Severity:** LOW — resolution happens once at startup typically

- [ ] **Step 1: Add threading.Lock**

Near `_AGENTLY_CACHE` definition:
```python
import threading
_agently_lock = threading.Lock()
```

- [ ] **Step 2: Wrap _resolve_agently_cli with lock**

```python
def _resolve_agently_cli() -> str | None:
    global _AGENTLY_CACHE, _RESOLVED
    with _agently_lock:
        if _RESOLVED:
            return _AGENTLY_CACHE
        _RESOLVED = True
    # ... rest of resolution logic ...
    # At each return point, cache is set before returning
```

- [ ] **Step 3: Verify compilation**

Run: `python -m py_compile tools/mail_tools.py`

---

### Task 4: tool_registry schema cache thread safety (Bug #23)

**Files:**
- Modify: `tool_engine/tool_registry.py`

**Severity:** MEDIUM — plugin registration can race with schema reads

- [ ] **Step 1: Add threading.Lock**

Near `_schema_cache` definition:
```python
import threading
_schema_lock = threading.Lock()
```

- [ ] **Step 2: Wrap all _schema_cache/_schema_version mutations with lock**

In every function that does `global _schema_cache, _schema_version`:
```python
with _schema_lock:
    _schema_version += 1
    _schema_cache = None
```

And in `get_tool_schema()`:
```python
with _schema_lock:
    if _schema_cache is not None:
        return _schema_cache
# ... build result ...
with _schema_lock:
    _schema_cache = result
```

- [ ] **Step 3: Verify compilation**

Run: `python -m py_compile tool_engine/tool_registry.py`

---

### Task 5: doctor.py os.system command injection (Bug #24)

**Files:**
- Modify: `core/doctor.py`

**Severity:** HIGH — port from env var could be manipulated

- [ ] **Step 1: Validate port is numeric before using in os.system**

In `_fix_port_conflict()`:
```python
def _fix_port_conflict() -> None:
    port_str = os.getenv("WEBUI_PORT", "8082")
    if not port_str.isdigit() or not (1 <= int(port_str) <= 65535):
        logger.warning("doctor.invalid_port value={}", port_str)
        return
    port = int(port_str)
    # ... rest of function with validated port ...
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile core/doctor.py`

---

### Task 6: setup.py API Key plaintext write (Bug #25)

**Files:**
- Modify: `web/routers/setup.py`

**Severity:** HIGH — API keys stored in plaintext on disk

- [ ] **Step 1: Replace plaintext write with encrypted write**

At line ~780, change:
```python
fp.write_text(api_key, encoding="utf-8")
```
To:
```python
from web._provider_keys import _encode_key
fp.write_text(_encode_key(api_key) + "\n", encoding="utf-8")
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile web/routers/setup.py`

---

### Task 7: models.py API Key plaintext write (Bug #26)

**Files:**
- Modify: `web/routers/models.py`

**Severity:** HIGH — API keys stored in plaintext on disk

- [ ] **Step 1: Replace plaintext write with encrypted write**

At line ~180, change:
```python
fp.write_text(api_key, encoding="utf-8")
```
To:
```python
from web._provider_keys import _encode_key
fp.write_text(_encode_key(api_key) + "\n", encoding="utf-8")
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile web/routers/models.py`

---

### Task 8: health.py _all_running race condition (Bug #27)

**Files:**
- Modify: `web/routers/health.py`

**Severity:** MEDIUM — flag checked outside lock

- [ ] **Step 1: Move _all_running check inside lock**

In `test_all()`:
```python
async def test_all(request: Request) -> Any:
    if _all_running_lock.locked():
        raise HTTPException(409, "全量自检已在进行中")
    # ... acquire lock, set flag ...
```
This is actually already correct — `_all_running_lock.locked()` is checked before acquire.
But `_all_running = True` is set after acquire without protection. Fix:
Move `_all_running = True` inside the lock acquisition flow (it already is after acquire).

The real issue: `_all_running = False` in finally block should be atomic with release.
Current code is fine since finally runs in same coroutine. No fix needed — false alarm.

- [ ] **Step 2: Mark as no-fix-needed after review**

---

### Task 9: system.py temp bat file cleanup (Bug #28)

**Files:**
- Modify: `web/routers/system.py`

**Severity:** LOW — temp bat files accumulate in temp dir

- [ ] **Step 1: Add bat file self-deletion**

Change the bat content to self-delete:
```python
bat.write(f'@echo off\ntimeout /t 2 /nobreak >nul\n"{python}" "{script}" {arg_str}\ndel "%~f0"\n')
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile web/routers/system.py`

---

### Task 10: calculator eval sandbox bypass (Bug #29)

**Files:**
- Modify: `tools/code_tools_v2.py`

**Severity:** HIGH — `__import__` can bypass sandbox via string concatenation

- [ ] **Step 1: Add AST-based validation before eval**

The current string-based check for `__` can be bypassed with techniques like:
`''.__class__.__mro__[1].__subclasses__()` — but `__` is already blocked.

However, the check `if pattern in expression` for `__` does catch double underscores.
The real risk: Unicode tricks or encoding bypasses. Add a secondary AST validation:

```python
import ast
try:
    tree = ast.parse(expression, mode='eval')
except SyntaxError:
    return ToolResult.fail("表达式语法错误")

for node in ast.walk(tree):
    if isinstance(node, (ast.Import, ast.ImportFrom, ast.Attribute)):
        if isinstance(node, ast.Attribute) and node.attr.startswith('_'):
            return ToolResult.fail("表达式包含不允许的属性访问")
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in ('__import__', 'exec', 'eval', 'compile', 'open', 'getattr', 'setattr', 'delattr', 'type', 'vars', 'dir', 'globals', 'locals', 'input'):
            return ToolResult.fail(f"表达式包含不允许的函数: {node.func.id}")
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile tools/code_tools_v2.py`