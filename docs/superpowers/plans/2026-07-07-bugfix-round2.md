# Bug Fix Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 10 bugs found in the second round of code review, covering operator precedence, thread safety, cache safety, and security hardening.

**Architecture:** Each fix is isolated to a single file with minimal changes. All concurrency fixes use existing patterns (threading.Lock for sync, asyncio.Lock for async). No new dependencies.

**Tech Stack:** Python 3.11+, threading, asyncio

## Global Constraints

- All fixes must be backward-compatible (no API changes)
- Use existing lock patterns already in the codebase
- No new third-party dependencies
- Each fix must compile without errors (`python -m py_compile`)
- Fixes must not change behavior in single-threaded scenarios

---

### Task 1: Fix Docker detection operator precedence (Bug #10)

**Files:**
- Modify: `agent.py:122`

**Interfaces:**
- Consumes: None
- Produces: Correct `_is_running_in_docker()` return value

**Bug:** `os.path.isfile("/proc/1/cgroup") and "docker" in open(...).read()` — due to Python operator precedence, `or` binds looser than `and`, so the expression evaluates as `A or (B and C)`. When `/.dockerenv` doesn't exist but `/proc/1/cgroup` does, the `and` short-circuits correctly. However, when `/.dockerenv` exists, the `and` part is never evaluated (correct). The real bug is that `open("/proc/1/cgroup")` is never closed (resource leak), and the logical grouping is unclear.

- [ ] **Step 1: Fix operator precedence and resource leak**

```python
def _is_running_in_docker() -> bool:
    """检测当前是否在 Docker 容器内运行。"""
    import os
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", errors="ignore") as f:
            return "docker" in f.read()
    except OSError:
        return False
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile agent.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add agent.py
git commit -m "fix: Docker检测运算符优先级+文件句柄泄漏 (Bug #10)"
```

---

### Task 2: Add lock to _load_or_create_secret (Bug #11)

**Files:**
- Modify: `web/routers/auth.py:42-58`

**Interfaces:**
- Consumes: None
- Produces: Thread-safe `_load_or_create_secret()`

**Bug:** Multiple workers/threads calling `_load_or_create_secret()` concurrently can race on file write, potentially corrupting the secret file or reading a partially-written secret.

- [ ] **Step 1: Add threading.Lock to protect secret initialization**

Add a module-level lock and use it in `_load_or_create_secret`:

```python
_secret_lock = Lock()

def _load_or_create_secret() -> str:
    global _SECRET
    with _secret_lock:
        if _SECRET:
            return _SECRET
        env_secret = os.getenv("WEBUI_SECRET", "")
        if env_secret:
            _SECRET = env_secret
            return _SECRET
        secret_path = _get_secret_path()
        if secret_path.exists():
            _SECRET = secret_path.read_text(encoding="utf-8").strip()
        else:
            _SECRET = secrets.token_hex(32)
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_text(_SECRET, encoding="utf-8")
            try:
                secret_path.chmod(0o600)
            except OSError:
                pass
        return _SECRET
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile web/routers/auth.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add web/routers/auth.py
git commit -m "fix: auth secret初始化加线程锁防竞态 (Bug #11)"
```

---

### Task 3: Add thread safety to pty_executor _pending_cmd (Bug #12)

**Files:**
- Modify: `web/pty_executor.py`

**Interfaces:**
- Consumes: None
- Produces: Thread-safe `feed_output()` and `execute_on_pty()`

**Bug:** `_pending_cmd` is a module-level global written from async context (`execute_on_pty`) and read from background thread (`feed_output` via `_reader_thread`). No synchronization.

- [ ] **Step 1: Add threading.Lock for _pending_cmd**

At the top of the file, add:
```python
import threading
_pending_lock = threading.Lock()
```

In `execute_on_pty`, wrap all reads/writes of `_pending_cmd`:
```python
    with _pending_lock:
        if _pending_cmd and not _pending_cmd.event.is_set():
            pending_event = _pending_cmd.event
        else:
            pending_event = None
    if pending_event:
        try:
            await asyncio.wait_for(pending_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            with _pending_lock:
                _pending_cmd = None
```

And:
```python
    with _pending_lock:
        _pending_cmd = state
```

And:
```python
    except (OSError, BrokenPipeError) as e:
        with _pending_lock:
            _pending_cmd = None
```

And:
```python
    except asyncio.TimeoutError:
        with _pending_lock:
            _pending_cmd = None
```

And:
```python
    _pending_cmd = None  # after event.wait succeeds
```
becomes:
```python
    with _pending_lock:
        _pending_cmd = None
```

In `feed_output`:
```python
def feed_output(text: str) -> None:
    with _pending_lock:
        state = _pending_cmd
    if not state:
        return
    # ... rest of the function uses local 'state' only
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile web/pty_executor.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add web/pty_executor.py
git commit -m "fix: pty_executor _pending_cmd加线程锁防竞态 (Bug #12)"
```

---

### Task 4: Add lock to _SYSTEM_PROMPT_CACHE (Bug #13)

**Files:**
- Modify: `prompt_builder.py:1130-1170`

**Interfaces:**
- Consumes: `_cache_lock` (already exists from Bug #2 fix)
- Produces: Thread-safe `build_system_prompt()`

- [ ] **Step 1: Wrap _SYSTEM_PROMPT_CACHE reads/writes with _cache_lock**

In the `build_system_prompt` function, wrap the global cache access:

```python
        global _SYSTEM_PROMPT_CACHE, _SYSTEM_PROMPT_CACHE_TS, _SYSTEM_PROMPT_CACHE_MTIMES, _SYSTEM_PROMPT_CACHE_ADDR_TERM
        from config import DATA_DIR

        now = time.time()
        with _cache_lock:
            current_mtimes = _get_workspace_mtimes()
            mtime_changed = current_mtimes != _SYSTEM_PROMPT_CACHE_MTIMES
            addr_changed = address_term != _SYSTEM_PROMPT_CACHE_ADDR_TERM

            if _SYSTEM_PROMPT_CACHE and (now - _SYSTEM_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL and not mtime_changed and not addr_changed:
                system_prompt = _SYSTEM_PROMPT_CACHE
            else:
                system_prompt = None

        if system_prompt is None:
            sections = _build_workspace_sections(address_term)
            sections.append(_build_hardware_context(DATA_DIR))
            system_prompt = "\n\n---\n\n".join(sections)

            with _cache_lock:
                _SYSTEM_PROMPT_CACHE = system_prompt
                _SYSTEM_PROMPT_CACHE_TS = now
                _SYSTEM_PROMPT_CACHE_MTIMES = current_mtimes
                _SYSTEM_PROMPT_CACHE_ADDR_TERM = address_term
```

Note: Need to handle the `system_prompt is None` case properly — the original code had an if/else that set `system_prompt` in both branches.

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile prompt_builder.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add prompt_builder.py
git commit -m "fix: _SYSTEM_PROMPT_CACHE加锁防竞态 (Bug #13)"
```

---

### Task 5: Add lock to _SAFE_PROMPT_CACHE (Bug #14)

**Files:**
- Modify: `prompt_builder.py:1345-1370`

**Interfaces:**
- Consumes: `_cache_lock` (already exists)
- Produces: Thread-safe `build_safe_system_prompt()`

- [ ] **Step 1: Wrap _SAFE_PROMPT_CACHE reads/writes with _cache_lock**

```python
    global _SAFE_PROMPT_CACHE, _SAFE_PROMPT_CACHE_TS, _SAFE_PROMPT_CACHE_NAME

    from config import get_agent_display_name
    xiaoda_name = get_agent_display_name('xiaoda')

    now = time.time()
    with _cache_lock:
        cache_hit = (_SAFE_PROMPT_CACHE
                and (now - _SAFE_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL
                and _SAFE_PROMPT_CACHE_NAME == xiaoda_name)
        if cache_hit:
            safe_prompt = _SAFE_PROMPT_CACHE
        else:
            safe_prompt = None

    if safe_prompt is None:
        sections = []
        # ... (existing section building code unchanged) ...
        safe_prompt = "\n\n---\n\n".join(sections)
        with _cache_lock:
            _SAFE_PROMPT_CACHE = safe_prompt
            _SAFE_PROMPT_CACHE_TS = now
            _SAFE_PROMPT_CACHE_NAME = xiaoda_name
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile prompt_builder.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add prompt_builder.py
git commit -m "fix: _SAFE_PROMPT_CACHE加锁防竞态 (Bug #14)"
```

---

### Task 6: Add lock to _is_revoked cache (Bug #15)

**Files:**
- Modify: `web/routers/auth.py:107-120`

**Interfaces:**
- Consumes: `_revoked_lock` (already exists)
- Produces: Thread-safe `_is_revoked()`

- [ ] **Step 1: Use existing _revoked_lock in _is_revoked**

```python
def _is_revoked(token: str) -> bool:
    global _revoked_cache, _revoked_cache_mtime
    path = _get_revoked_path()
    if not path.exists():
        return False
    try:
        mtime = path.stat().st_mtime
        with _revoked_lock:
            if mtime != _revoked_cache_mtime:
                data = json.loads(path.read_text(encoding="utf-8"))
                _revoked_cache = set(data.get("revoked", []))
                _revoked_cache_mtime = mtime
            return token in _revoked_cache
    except Exception as exc:
        logger.debug("auth.is_revoked_json_parse_failed: {}", exc, exc_info=True)
        return False
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile web/routers/auth.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add web/routers/auth.py
git commit -m "fix: _is_revoked缓存加锁防竞态 (Bug #15)"
```

---

### Task 7: Restrict write whitelist for home directory (Bug #16)

**Files:**
- Modify: `tools/file_tools_v2.py:86`

**Interfaces:**
- Consumes: None
- Produces: More restrictive write sandbox

**Bug:** `os.path.expanduser("~")` in the write whitelist allows writing to any file in the user's home directory, including `~/.ssh/authorized_keys`, `~/.bashrc`, etc.

- [ ] **Step 1: Remove home directory from write whitelist, keep read whitelist**

Change the write_allowed list to remove `os.path.expanduser("~")`:

```python
            if mode == "write":
                write_allowed = [_PROJECT_DIR, os.path.join(_PROJECT_DIR, "tts_cache"), "/tmp", "/var/tmp", tempfile.gettempdir()]
```

This keeps home directory in the READ whitelist (line 34) but removes it from WRITE.

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile tools/file_tools_v2.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add tools/file_tools_v2.py
git commit -m "fix: 文件写入白名单移除用户主目录，防止任意文件写入 (Bug #16)"
```

---

### Task 8: Add lock to get_config_service singleton (Bug #17)

**Files:**
- Modify: `web/config_service.py:154-157`

**Interfaces:**
- Consumes: None
- Produces: Thread-safe `get_config_service()`

- [ ] **Step 1: Add threading.Lock to singleton creation**

```python
import threading

_config_service_lock = threading.Lock()

def get_config_service() -> ConfigService:
    global _instance
    if _instance is None:
        with _config_service_lock:
            if _instance is None:
                _instance = ConfigService()
    return _instance
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile web/config_service.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add web/config_service.py
git commit -m "fix: ConfigService单例加双重检查锁 (Bug #17)"
```

---

### Task 9: Atomic .env file write (Bug #18)

**Files:**
- Modify: `agent.py:88`

**Interfaces:**
- Consumes: None
- Produces: Atomic .env file creation

- [ ] **Step 1: Use atomic write pattern for .env file**

```python
                import tempfile
                tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(ENV_PATH), prefix=".env.tmp")
                try:
                    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                        f.write("")
                    os.replace(tmp_path, ENV_PATH)
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile agent.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add agent.py
git commit -m "fix: .env文件写入改为原子操作 (Bug #18)"
```

---

### Task 10: Add lock to _sf_pricing_cache (Bug #19)

**Files:**
- Modify: `web/routers/model_discovery.py:185`

**Interfaces:**
- Consumes: None
- Produces: Thread-safe `_get_siliconflow_pricing()`

- [ ] **Step 1: Add threading.Lock for pricing cache**

```python
import threading
_sf_pricing_lock = threading.Lock()

def _get_siliconflow_pricing() -> dict[str, dict] | None:
    global _sf_pricing_cache, _sf_pricing_ts
    with _sf_pricing_lock:
        if _sf_pricing_cache and time.time() - _sf_pricing_ts < _SF_PRICING_TTL:
            return _sf_pricing_cache

    # ... (existing HTTP fetch code unchanged) ...

    with _sf_pricing_lock:
        _sf_pricing_cache = pricing_map
        _sf_pricing_ts = time.time()

    return pricing_map
```

- [ ] **Step 2: Verify compilation**

Run: `python -m py_compile web/routers/model_discovery.py`
Expected: No errors

- [ ] **Step 3: Commit**

```bash
git add web/routers/model_discovery.py
git commit -m "fix: SiliconFlow定价缓存加锁防竞态 (Bug #19)"
```

---

## Verification

After all tasks are complete:

- [ ] **Run full compilation check**

```bash
python -m py_compile agent.py; python -m py_compile web/routers/auth.py; python -m py_compile web/pty_executor.py; python -m py_compile prompt_builder.py; python -m py_compile tools/file_tools_v2.py; python -m py_compile web/config_service.py; python -m py_compile web/routers/model_discovery.py
```

- [ ] **Final commit with tag**

```bash
git tag -a v0.4.99-bugfix2 -m "Bug fix round 2: 10 bugs fixed"
```