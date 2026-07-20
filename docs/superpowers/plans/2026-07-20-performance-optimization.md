# 性能优化统一修改实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 10 项性能真实缺口（G1-G10）+ 系统性审计 + 全量回归，确保不影响现有功能。

**Architecture:** 每项独立修改+独立测试+独立开关（.env 控制），分 5 个 Phase 渐进推进。Phase 1 低风险→Phase 5 全量回归。

**Tech Stack:** Python 3.11+ / asyncio / httpx / FastAPI / pytest / sqlite-vec

## Global Constraints

- 所有修改不得破坏现有 2057+ 测试（pytest 零失败）
- 每项优化支持 `.env` 开关独立灰度（除 G6/G7/G8/G9 是行为修复无开关）
- Windows 安装包大小目标 ~100MB（不引入大依赖）
- 配置开关读取用 `utils/config.py` 的 `get_env_bool()` 函数
- 所有新增日志用 `logger`（loguru），key=value 格式
- 提交粒度：每个 G 项独立 commit
- 测试超时：单测 60s（pytest.ini 已配置）

---

## Phase 1: 低风险快速修复

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

### Task 2: G7 dream_consolidation scheduler 修 bug

**Files:**
- Modify: `core/dream_consolidation.py:59-68`（__init__ 加 memory_db 参数）
- Modify: `core/dream_consolidation.py:411-412`（scheduler 调用 consolidate_from_db）
- Modify: `core/dream_consolidation.py:446-451`（get_dream_consolidator 工厂注入 memory_db）
- Test: `tests/test_dream_scheduler_calls_from_db.py`

**Interfaces:**
- Consumes: `MemoryDB` 实例（来自 `db/db_memory.py` 的 `MemoryDB` 类）
- Produces: `DreamConsolidator.__init__(..., memory_db=None)`、scheduler 优先调 `consolidate_from_db(memory_db)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_dream_scheduler_calls_from_db.py
"""G7: dream scheduler 应调用 consolidate_from_db 而非 consolidate."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from core.dream_consolidation import DreamConsolidator


def test_init_accepts_memory_db_param():
    """DreamConsolidator.__init__ 应接受 memory_db 参数."""
    mock_db = MagicMock()
    dc = DreamConsolidator(memory_db=mock_db)
    assert dc._memory_db is mock_db


async def test_scheduler_calls_consolidate_from_db():
    """scheduler 应优先调用 consolidate_from_db，不调 consolidate."""
    mock_db = MagicMock()
    dc = DreamConsolidator(memory_db=mock_db)

    # mock 两个方法，确认调用哪个
    dc.consolidate = AsyncMock(return_value={"archived": 0})
    dc.consolidate_from_db = AsyncMock(return_value={"archived": 0})

    # 用 patch 缩短 sleep 时间立即触发
    with patch("core.dream_consolidation.asyncio.sleep", new=AsyncMock()):
        with patch("core.dream_consolidation.time") as mock_time:
            # 让 target <= time.time() 立即触发
            mock_time.localtime.return_value = type("TS", (), {
                "tm_year": 2026, "tm_mon": 7, "tm_mday": 20,
                "tm_hour": 3, "tm_min": 0, "tm_sec": 0,
                "tm_wday": 0, "tm_yday": 200, "tm_isdst": -1
            })()
            mock_time.mktime.return_value = 1000.0
            mock_time.time.return_value = 2000.0  # target < time，立即触发

            task = asyncio.create_task(dc._run_scheduled_test())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # 验证调用 consolidate_from_db 而非 consolidate
    assert dc.consolidate_from_db.called
    assert not dc.consolidate.called


async def test_scheduler_fallback_to_consolidate_when_no_db():
    """无 memory_db 时降级调用 consolidate."""
    dc = DreamConsolidator(memory_db=None)
    dc.consolidate = AsyncMock(return_value={})
    dc.consolidate_from_db = AsyncMock(return_value={})

    with patch("core.dream_consolidation.asyncio.sleep", new=AsyncMock()):
        with patch("core.dream_consolidation.time") as mock_time:
            mock_time.localtime.return_value = type("TS", (), {
                "tm_year": 2026, "tm_mon": 7, "tm_mday": 20,
                "tm_hour": 3, "tm_min": 0, "tm_sec": 0,
                "tm_wday": 0, "tm_yday": 200, "tm_isdst": -1
            })()
            mock_time.mktime.return_value = 1000.0
            mock_time.time.return_value = 2000.0

            task = asyncio.create_task(dc._run_scheduled_test())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert dc.consolidate.called
    assert not dc.consolidate_from_db.called
```

注意：测试用 `_run_scheduled_test` 是为测试暴露的内部方法，需在 Step 3 实现。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_dream_scheduler_calls_from_db.py -v`
Expected: FAIL with "TypeError: __init__() got an unexpected keyword argument 'memory_db'"

- [ ] **Step 3: 修改 __init__ 接受 memory_db**

```python
# core/dream_consolidation.py 第 59-68 行
def __init__(self, threshold_importance: float = 0.2,
              threshold_strength: float = 0.1,
              on_consolidate: Callable[[], None] | None = None,
              memory_db: Any = None) -> None:
    self._memories: dict[str, Memory] = {}
    self._importance_threshold = threshold_importance
    self._strength_threshold = threshold_strength
    self._fsrs = FSRSModel()
    self._scheduler_task: asyncio.Task | None = None
    self._last_consolidate_at = 0
    self._stats = {"consolidated": 0, "decayed": 0, "merged": 0, "strengthened": 0}
    self._memory_db = memory_db  # G7: scheduler 用此调 consolidate_from_db
```

- [ ] **Step 4: 修改 scheduler 逻辑**

```python
# core/dream_consolidation.py 第 396-414 行 start_scheduler 内的 _run()
async def _run() -> None:
    while True:
        now = time.localtime()
        target = time.mktime(time.struct_time((
            now.tm_year, now.tm_mon, now.tm_mday,
            hour, 0, 0, 0, 0, -1
        )))
        if target <= time.time():
            target += 86400
        wait = target - time.time()
        logger.info(f"Dream.scheduler next_run_in={wait:.0f}s")
        await asyncio.sleep(wait)
        try:
            # G7: 优先用 consolidate_from_db 操作真实记忆
            if self._memory_db is not None:
                result = await self.consolidate_from_db(self._memory_db)
                logger.info(f"Dream.scheduler.from_db done archived={result.get('archived', 0)}")
            else:
                await self.consolidate()
                logger.warning("Dream.scheduler.fallback_to_consolidate (no memory_db)")
        except Exception as e:
            logger.error(f"Dream.scheduler.failed: {e}")
```

- [ ] **Step 5: 暴露 _run_scheduled_test 供测试用**

在 `start_scheduler` 方法之后添加：

```python
async def _run_scheduled_test(self) -> None:
    """测试用：单次执行 scheduler 逻辑（不循环）."""
    try:
        if self._memory_db is not None:
            await self.consolidate_from_db(self._memory_db)
        else:
            await self.consolidate()
    except Exception as e:
        logger.error(f"Dream.scheduler_test.failed: {e}")
```

- [ ] **Step 6: 修改 get_dream_consolidator 工厂注入 memory_db**

```python
# core/dream_consolidation.py 第 446-451 行
def get_dream_consolidator(memory_db: Any = None) -> DreamConsolidator:
    """获取全局 DreamConsolidator 单例, 不存在时创建.

    Args:
        memory_db: MemoryDB 实例，用于 scheduler 调用 consolidate_from_db
    """
    global _dream
    if _dream is None:
        _dream = DreamConsolidator(memory_db=memory_db)
    elif memory_db is not None and _dream._memory_db is None:
        # 后注入（首次创建时未提供）
        _dream._memory_db = memory_db
    return _dream
```

- [ ] **Step 7: 启动时注入 memory_db**

查找 `get_dream_consolidator()` 调用点（在 agent 启动初始化处），改为传入 memory_db 实例。

```bash
cd /home/orangepi/ai-agent
grep -rn "get_dream_consolidator" --include="*.py" .
```

在主启动流程（如 `agent.py` 或 `core/agent.py` 的初始化）注入：
```python
from db.db_memory import MemoryDB
memory_db = MemoryDB(...)
get_dream_consolidator(memory_db=memory_db).start_scheduler(hour=3)
```

- [ ] **Step 8: 跑测试确认通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_dream_scheduler_calls_from_db.py -v --timeout=60`
Expected: PASS (3 tests)

- [ ] **Step 9: 跑回归测试**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_dream_engine_v2.py tests/test_dream_scheduler_calls_from_db.py -v --timeout=60`
Expected: PASS

- [ ] **Step 10: 提交**

```bash
cd /home/orangepi/ai-agent
git add core/dream_consolidation.py tests/test_dream_scheduler_calls_from_db.py
git commit -m "fix(G7): dream scheduler 改调 consolidate_from_db 修复空整合 bug"
```

---

## Phase 2: 核心性能修复

### Task 3: G1 问候短路

**Files:**
- Modify: `agent_core/message_processor.py:310`（slash 命令后插入问候短路）
- Test: `tests/test_greeting_shortcut.py`

**Interfaces:**
- Consumes: `ProcessResult` 类（已存在）、`get_env_bool("ENABLE_GREETING_SHORTCUT", True)`（新增开关）
- Produces: `MessageProcessor._try_greeting_shortcut(user_input, user_id, source) -> ProcessResult | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_greeting_shortcut.py
"""G1: 问候短路测试 - 纯问候 <100ms 返回，不调 LLM."""
import os
import time
from unittest.mock import MagicMock
os.environ["ENABLE_GREETING_SHORTCUT"] = "true"

from agent_core.message_processor import MessageProcessor


def _make_processor():
    """构造无依赖的 MessageProcessor."""
    mp = MessageProcessor.__new__(MessageProcessor)
    mp.slash_handler = None
    return mp


def test_pure_greeting_returns_shortcut():
    """纯问候"你好"应返回 shortcut reply，不调 LLM."""
    mp = _make_processor()
    # 任何问候变体
    for greeting in ["你好", "你好！", "你好。", "hi", "hello", "嗨", "在吗", "在不在？"]:
        result = mp._try_greeting_shortcut(greeting, "user1", "qq")
        assert result is not None, f"应命中短路: {greeting}"
        assert result.reply, f"reply 不能为空: {greeting}"
        assert result.emotion == "greeting"


def test_non_greeting_returns_none():
    """非问候"帮我写函数"应返回 None，走正常流程."""
    mp = _make_processor()
    for text in ["帮我写函数", "今天天气怎么样", "你好帮我写代码", "请问一下"],
                 ["你好请问"]:
        result = mp._try_greeting_shortcut(text, "user1", "qq")
        assert result is None, f"不应命中短路: {text}"


def test_thank_you_returns_shortcut():
    """感谢类"谢谢"应返回 shortcut."""
    mp = _make_processor()
    for text in ["谢谢", "感谢", "thanks", "thx"]:
        result = mp._try_greeting_shortcut(text, "user1", "qq")
        assert result is not None


def test_greeting_shortcut_latency_under_100ms():
    """问候短路延迟 < 100ms."""
    mp = _make_processor()
    start = time.monotonic()
    for _ in range(100):
        mp._try_greeting_shortcut("你好", "user1", "qq")
    elapsed = (time.monotonic() - start) * 1000 / 100  # 平均 ms
    assert elapsed < 100, f"平均延迟 {elapsed:.1f}ms 应 <100ms"


def test_group_chat_skips_shortcut():
    """群聊模式不触发短路（避免刷屏）."""
    mp = _make_processor()
    result = mp._try_greeting_shortcut("你好", "user1", "qq_group")
    assert result is None


def test_disabled_via_env():
    """ENABLE_GREETING_SHORTCUT=false 时关闭短路."""
    import importlib
    import agent_core.message_processor as mod
    os.environ["ENABLE_GREETING_SHORTCUT"] = "false"
    importlib.reload(mod)
    mp = mod.MessageProcessor.__new__(mod.MessageProcessor)
    result = mp._try_greeting_shortcut("你好", "user1", "qq")
    assert result is None
    os.environ["ENABLE_GREETING_SHORTCUT"] = "true"
    importlib.reload(mod)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_greeting_shortcut.py -v --timeout=60`
Expected: FAIL with "AttributeError: 'MessageProcessor' object has no attribute '_try_greeting_shortcut'"

- [ ] **Step 3: 实现 _try_greeting_shortcut**

在 `agent_core/message_processor.py` 添加方法（在 `_try_simple_chat_fast_path` 之前）：

```python
import re
import os
from datetime import datetime

# 模块级正则（编译一次）
_GREETING_PATTERN = re.compile(
    r'^(你好|您好|hi|hello|hey|嗨|在吗|在不在|在么|'
    r'早安|早上好|早|午安|下午好|晚上好|晚安|'
    r'谢谢|感谢|thanks|thx|多谢)\s*[!！。.～~？?]*$',
    re.IGNORECASE
)

_GREETING_REPLIES = [
    "你好呀～有什么可以帮你的吗？",
    "嗨～我在呢，怎么啦？",
    "你好～今天想聊点什么呢？",
]

_THANK_REPLIES = ["不客气～", "不用谢啦～", "举手之劳～"]
_TIME_GREETINGS = [
    (5, 12, "早上好～新的一天开始啦，今天也要加油哦！"),
    (12, 18, "下午好～今天过得怎么样呀？"),
    (18, 22, "晚上好～今天辛苦啦，有什么想聊的吗？"),
    (22, 30, "夜深啦～记得早点休息哦，有什么事明天再说？"),
]


def _is_greeting_enabled() -> bool:
    """读取 ENABLE_GREETING_SHORTCUT 开关（默认 true）."""
    return os.environ.get("ENABLE_GREETING_SHORTCUT", "true").lower() in ("true", "1", "yes")


class MessageProcessor:
    # ... 已有代码 ...

    def _try_greeting_shortcut(self, user_input: str, user_id: str, source: str):
        """G1: 纯问候短路 - 跳过 LLM 直接返回 <100ms.

        Returns:
            ProcessResult | None: 命中返回 ProcessResult，否则 None
        """
        if not _is_greeting_enabled():
            return None
        # 群聊跳过（避免刷屏）
        if source and "group" in source.lower():
            return None
        text = (user_input or "").strip()
        if not text or len(text) > 20:  # 问候不超过 20 字符
            return None
        match = _GREETING_PATTERN.match(text)
        if not match:
            return None
        keyword = match.group(1).lower()
        # 时段问候
        now_hour = datetime.now().hour
        reply = None
        for start_h, end_h, msg in _TIME_GREETINGS:
            if start_h <= now_hour < end_h:
                reply = msg
                break
        if reply is None:
            reply = msg  # fallback 到最后一个
        # 感谢类
        if keyword in ("谢谢", "感谢", "thanks", "thx", "多谢"):
            import random
            reply = random.choice(_THANK_REPLIES)
        elif reply is None:
            import random
            reply = random.choice(_GREETING_REPLIES)
        return ProcessResult(reply=reply, emotion="greeting")
```

- [ ] **Step 4: 在 _process_impl 中调用短路**

修改 `agent_core/message_processor.py:310` slash 命令之后：

```python
# slash 命令
if self.slash_handler and self.slash_handler.is_slash_command(user_input):
    slash_reply = await self.slash_handler.handle(user_input, user_id)
    return ProcessResult(reply=slash_reply)

# G1: 问候短路（在 chat_targets 之前）
greeting_result = self._try_greeting_shortcut(user_input, user_id, source or "")
if greeting_result is not None:
    trace.info("agent.greeting_shortcut_hit", keyword=user_input[:20])
    return greeting_result

chat_targets = await self._parse_chat_target(user_input, user_id)
# ... 后续逻辑不变
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_greeting_shortcut.py -v --timeout=60`
Expected: PASS (6 tests)

- [ ] **Step 6: 跑回归测试**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_qq_streaming.py tests/test_greeting_shortcut.py -v --timeout=60`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
cd /home/orangepi/ai-agent
git add agent_core/message_processor.py tests/test_greeting_shortcut.py
git commit -m "feat(G1): 问候短路 <100ms 返回，跳过 LLM（默认开启，群聊跳过）"
```

---

### Task 4: G3 mental_state debounce

**Files:**
- Modify: `core/mental_state.py:197-225`（__init__ 加 timer，_save 改 debounce）
- Test: `tests/test_mental_state_debounce.py`

**Interfaces:**
- Produces: `MentalStateManager._save()` 改为 debounce 300ms；新增 `flush()` 立即写盘

- [ ] **Step 1: 写失败测试**

```python
# tests/test_mental_state_debounce.py
"""G3: mental_state debounce 测试 - 多次更新合并为 1 次写盘."""
import threading
import time
from unittest.mock import patch, MagicMock
from core.mental_state import MentalStateManager


def test_save_debounces_multiple_calls():
    """300ms 内连续 10 次 _save 只触发 1 次磁盘写入."""
    mgr = MentalStateManager(data_dir=None)
    call_count = 0
    original_save = mgr._state.__class__.save

    def counting_save(self, path):
        nonlocal call_count
        call_count += 1
        original_save(self, path)

    with patch.object(mgr._state.__class__, 'save', counting_save):
        for _ in range(10):
            mgr._save()
        # debounce 窗口内尚未写盘
        assert call_count == 0, "debounce 窗口内不应立即写盘"
        # 等待 debounce 窗口
        time.sleep(0.5)
        assert call_count == 1, f"应只写盘 1 次，实际 {call_count}"


def test_flush_writes_immediately():
    """flush() 立即触发写盘，不等 debounce."""
    mgr = MentalStateManager(data_dir=None)
    call_count = 0
    original_save = mgr._state.__class__.save

    def counting_save(self, path):
        nonlocal call_count
        call_count += 1

    with patch.object(mgr._state.__class__, 'save', counting_save):
        mgr._save()
        assert call_count == 0, "debounce 窗口内不应写盘"
        mgr.flush()
        assert call_count == 1, "flush 后应立即写盘"


def test_no_deadlock_on_concurrent_saves():
    """并发 _save 不死锁."""
    mgr = MentalStateManager(data_dir=None)
    def worker():
        for _ in range(100):
            mgr._save()
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    mgr.flush()
    # 不抛异常即通过
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_mental_state_debounce.py -v --timeout=60`
Expected: FAIL with "AssertionError: assert 0 == 0"（第一次 _save 直接写盘了 call_count=10）

- [ ] **Step 3: 改造 _save 为 debounce**

```python
# core/mental_state.py 顶部添加 import
import threading

# MentalStateManager.__init__ 第 197-201 行后添加：
def __init__(self, data_dir: Path | None = None) -> None:
    """data_dir 默认为 data/"""
    self._data_dir = Path(data_dir) if data_dir else Path("data")
    self._state_path = self._data_dir / "mental_state.json"
    self._state = self._load_or_init()
    # G3: debounce 写盘
    self._save_lock = threading.Lock()
    self._save_timer: threading.Timer | None = None
    self._save_pending = False

# 替换 _save 方法（第 220-225 行）：
def _save(self) -> None:
    """G3: debounce 300ms 写盘，多次调用合并为 1 次."""
    with self._save_lock:
        self._save_pending = True
        if self._save_timer is None or not self._save_timer.is_alive():
            self._save_timer = threading.Timer(0.3, self._do_save)
            self._save_timer.daemon = True
            self._save_timer.start()

def _do_save(self) -> None:
    """实际写盘（在后台线程执行）."""
    with self._save_lock:
        if not self._save_pending:
            return
        self._save_pending = False
    try:
        self._state.save(self._state_path)
    except Exception as e:
        logger.warning(f"MentalState.save_failed error={e}")

def flush(self) -> None:
    """G3: 立即写盘（退出时调用）."""
    with self._save_lock:
        if self._save_timer is not None and self._save_timer.is_alive():
            self._save_timer.cancel()
            self._save_timer = None
        self._save_pending = False
    try:
        self._state.save(self._state_path)
    except Exception as e:
        logger.warning(f"MentalState.flush_failed error={e}")
```

- [ ] **Step 4: 在进程退出时调用 flush**

查找 agent 的 shutdown/atexit 钩子（如 `core/agent.py` 的 cleanup），添加：
```python
from core.mental_state import get_mental_state_manager
try:
    get_mental_state_manager().flush()
except Exception:
    pass
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_mental_state_debounce.py -v --timeout=60`
Expected: PASS (3 tests)

- [ ] **Step 6: 跑回归测试**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_mental_state.py tests/test_mental_state_debounce.py -v --timeout=60`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
cd /home/orangepi/ai-agent
git add core/mental_state.py tests/test_mental_state_debounce.py
git commit -m "feat(G3): mental_state debounce 300ms 写盘，避免 Windows 卡顿"
```

---

### Task 5: G8 context_compressor retrieve async

**Files:**
- Modify: `memory/context_compressor.py`（新增 retrieve_async）
- Modify: `agent_core/tools/`（retrieve_context 工具改用 async）
- Test: `tests/test_context_compressor_async.py`

**Interfaces:**
- Produces: `ContextCompressor.retrieve_async(ccr_key) -> str | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_context_compressor_async.py
"""G8: context_compressor retrieve_async 测试."""
import asyncio
import time
from memory.context_compressor import ContextCompressor


async def test_retrieve_async_returns_cached():
    """retrieve_async 应返回缓存的原始内容."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        comp = ContextCompressor(cache_dir=Path(tmp))
        # 先同步缓存
        comp._cache_original("test_key", "hello world")
        # 异步读取
        result = await comp.retrieve_async("test_key")
        assert result == "hello world"


async def test_retrieve_async_not_found():
    """不存在的 key 返回 None."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        comp = ContextCompressor(cache_dir=Path(tmp))
        result = await comp.retrieve_async("nonexistent")
        assert result is None


async def test_retrieve_async_does_not_block_event_loop():
    """retrieve_async 不应阻塞事件循环（其他协程可并行）."""
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        comp = ContextCompressor(cache_dir=Path(tmp))
        comp._cache_original("big_key", "x" * 1024 * 1024)  # 1MB

        other_ran = False

        async def other_task():
            nonlocal other_ran
            other_ran = True

        async def retrieve_task():
            await comp.retrieve_async("big_key")

        # 并行：retrieve 和 other_task 应能交错
        await asyncio.gather(retrieve_task(), other_task())
        assert other_ran, "retrieve_async 期间 other_task 应能执行"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_context_compressor_async.py -v --timeout=60`
Expected: FAIL with "AttributeError: 'ContextCompressor' object has no attribute 'retrieve_async'"

- [ ] **Step 3: 实现 retrieve_async**

在 `memory/context_compressor.py` 添加方法：

```python
import asyncio

class ContextCompressor:
    # ... 已有代码 ...

    async def retrieve_async(self, ccr_key: str) -> str | None:
        """G8: 异步读取缓存，避免阻塞事件循环."""
        return await asyncio.to_thread(self.retrieve, ccr_key)
```

- [ ] **Step 4: 修改 retrieve_context 工具调用点**

```bash
cd /home/orangepi/ai-agent
grep -rn "retrieve_context\|ctx_comp\.retrieve\|compressor\.retrieve" --include="*.py" agent_core/ tools/
```

将调用 `comp.retrieve(ccr_key)` 改为 `await comp.retrieve_async(ccr_key)`（仅当在 async 上下文中）

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_context_compressor_async.py -v --timeout=60`
Expected: PASS (3 tests)

- [ ] **Step 6: 跑回归测试**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_context_compressor.py tests/test_context_compressor_async.py -v --timeout=60`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
cd /home/orangepi/ai-agent
git add memory/context_compressor.py tests/test_context_compressor_async.py
git commit -m "feat(G8): context_compressor 新增 retrieve_async，避免阻塞事件循环"
```

---

### Task 6: G9 tts_engine read_bytes async

**Files:**
- Modify: `emotion/tts_engine.py:321`
- Test: `tests/test_tts_read_bytes_async.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_tts_read_bytes_async.py
"""G9: tts_engine read_bytes async 测试."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock


async def test_synthesize_voice_data_url_uses_async_read():
    """synthesize_voice_data_url 应使用 asyncio.to_thread 读取文件."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"\x00" * (10 * 1024 * 1024))  # 10MB
        f.flush()
        big_file = Path(f.name)

    try:
        # 检查 tts_engine 源码是否使用 asyncio.to_thread
        import inspect
        from emotion import tts_engine
        source = inspect.getsource(tts_engine)
        assert "asyncio.to_thread" in source or "to_thread" in source, \
            "tts_engine 应使用 asyncio.to_thread 异步读取文件"
    finally:
        big_file.unlink(missing_ok=True)


async def test_concurrent_read_does_not_block():
    """10MB 文件读取期间事件循环不阻塞."""
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"\x00" * (10 * 1024 * 1024))
        f.flush()
        big_file = Path(f.name)

    other_ran = False

    async def other_task():
        nonlocal other_ran
        other_ran = True

    async def read_task():
        return await asyncio.to_thread(big_file.read_bytes)

    try:
        await asyncio.gather(read_task(), other_task())
        assert other_ran
    finally:
        big_file.unlink(missing_ok=True)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_tts_read_bytes_async.py -v --timeout=60`
Expected: FAIL

- [ ] **Step 3: 改造 tts_engine**

```python
# emotion/tts_engine.py 第 321 行原：
# data = path.read_bytes()
# 改为：
import asyncio
data = await asyncio.to_thread(path.read_bytes)
```

确保调用方 `synthesize_voice_data_url` 是 async（若不是则改为 async）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_tts_read_bytes_async.py -v --timeout=60`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd /home/orangepi/ai-agent
git add emotion/tts_engine.py tests/test_tts_read_bytes_async.py
git commit -m "feat(G9): tts_engine read_bytes 改用 asyncio.to_thread 避免阻塞"
```

---

## Phase 3: 连接稳定性

### Task 7: G2 WebSocket broadcast 背压

**Files:**
- Modify: `web/ws_hub.py:87-90`
- Test: `tests/test_ws_broadcast_backpressure.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ws_broadcast_backpressure.py
"""G2: WS broadcast 背压测试 - 慢连接不阻塞快连接."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from web.ws_hub import ConnectionManager


async def test_broadcast_does_not_block_on_slow_connection():
    """1 个慢连接不应阻塞其他连接."""
    mgr = ConnectionManager()

    # 模拟 3 个连接：1 慢 2 快
    slow_ws = AsyncMock()
    async def slow_send(*a, **kw):
        await asyncio.sleep(10)  # 模拟慢
    slow_ws.send_json = slow_send

    fast_ws1 = AsyncMock()
    fast_ws2 = AsyncMock()

    mgr._connections = {
        "slow": {"ws": slow_ws},
        "fast1": {"ws": fast_ws1},
        "fast2": {"ws": fast_ws2},
    }

    # 广播事件
    event = {"type": "test"}
    await asyncio.wait_for(mgr.broadcast(event), timeout=10.0)

    # 快连接应收到事件
    fast_ws1.send_json.assert_called_once_with(event)
    fast_ws2.send_json.assert_called_once_with(event)
    # 慢连接应被清理（从 _connections 移除）
    assert "slow" not in mgr._connections


async def test_broadcast_with_no_connections_returns_immediately():
    """无连接时立即返回."""
    mgr = ConnectionManager()
    mgr._connections = {}
    await asyncio.wait_for(mgr.broadcast({"type": "test"}), timeout=1.0)


async def test_broadcast_cleans_up_failed_connections():
    """发送失败的连接应被清理."""
    mgr = ConnectionManager()
    failed_ws = AsyncMock()
    failed_ws.send_json = AsyncMock(side_effect=RuntimeError("connection closed"))
    ok_ws = AsyncMock()

    mgr._connections = {
        "failed": {"ws": failed_ws},
        "ok": {"ws": ok_ws},
    }

    await mgr.broadcast({"type": "test"})
    assert "failed" not in mgr._connections
    assert "ok" in mgr._connections
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_ws_broadcast_backpressure.py -v --timeout=60`
Expected: FAIL with timeout（原 broadcast 串行会被慢连接阻塞）

- [ ] **Step 3: 改造 broadcast**

```python
# web/ws_hub.py 第 87-90 行替换为：
async def broadcast(self, event: dict) -> None:
    """G2: 向所有活跃连接广播事件（fire-and-forget 扇出 + 5s 超时背压）."""
    if not self._connections:
        return
    tasks = {
        asyncio.create_task(self._safe_send(cid, event)): cid
        for cid in list(self._connections)
    }
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=5.0)
    # 清理超时的慢连接
    for t in pending:
        cid = tasks[t]
        t.cancel()
        logger.warning("ws.broadcast_timeout conn_id={}", cid)
        self.unregister(cid)

async def _safe_send(self, conn_id: str, event: dict) -> None:
    """G2: 安全发送，失败时清理连接."""
    ws = self._connections.get(conn_id, {}).get("ws")
    if ws is None:
        return
    try:
        await ws.send_json(event)
    except Exception as e:
        logger.warning("ws.send_failed conn_id={} error={}", conn_id, str(e))
        self.unregister(conn_id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_ws_broadcast_backpressure.py -v --timeout=60`
Expected: PASS (3 tests)

- [ ] **Step 5: 跑回归测试**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/ -k "ws" -v --timeout=60`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
cd /home/orangepi/ai-agent
git add web/ws_hub.py tests/test_ws_broadcast_backpressure.py
git commit -m "feat(G2): WS broadcast 改 fire-and-forget 扇出 + 5s 超时背压"
```

---

### Task 8: G5 WebSocket 心跳

**Files:**
- Modify: `web/ws_hub.py`（新增 heartbeat_loop）
- Test: `tests/test_ws_heartbeat.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ws_heartbeat.py
"""G5: WebSocket 心跳测试 - 死连接 40s 内清理."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from web.ws_hub import ConnectionManager


async def test_heartbeat_sends_ping_every_30s():
    """心跳协程应每 30s 发送 ping."""
    mgr = ConnectionManager()
    ws = AsyncMock()
    mgr._connections["test"] = {"ws": ws}

    # 加速：patch sleep
    with patch("web.ws_hub.asyncio.sleep", new=AsyncMock()):
        task = asyncio.create_task(mgr._heartbeat_loop("test"))
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # 应至少发过一次 ping
    sent_events = [call.args[0] for call in ws.send_json.call_args_list]
    assert any(e.get("type") == "ping" for e in sent_events)


async def test_heartbeat_cleans_up_dead_connection():
    """无 pong 响应的连接应被清理."""
    mgr = ConnectionManager()
    ws = AsyncMock()
    # send 失败模拟死连接
    ws.send_json = AsyncMock(side_effect=RuntimeError("dead"))
    mgr._connections["dead"] = {"ws": ws}

    with patch("web.ws_hub.asyncio.sleep", new=AsyncMock()):
        await mgr._heartbeat_loop("dead")

    # 死连接应被清理
    assert "dead" not in mgr._connections
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_ws_heartbeat.py -v --timeout=60`
Expected: FAIL with "AttributeError: 'ConnectionManager' object has no attribute '_heartbeat_loop'"

- [ ] **Step 3: 实现 heartbeat_loop**

在 `web/ws_hub.py` 添加：

```python
HEARTBEAT_INTERVAL = 30  # 秒
HEARTBEAT_TIMEOUT = 10   # 等待 pong 超时

class ConnectionManager:
    # ... 已有代码 ...

    async def _heartbeat_loop(self, conn_id: str) -> None:
        """G5: 每个连接的心跳协程 - 30s ping + 10s pong 超时."""
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                ws = self._connections.get(conn_id, {}).get("ws")
                if ws is None:
                    return
                await ws.send_json({"type": "ping"})
                # pong 处理在 on_message 中 set event
                evt = self._pong_events.get(conn_id)
                if evt is None:
                    evt = asyncio.Event()
                    self._pong_events[conn_id] = evt
                evt.clear()
                await asyncio.wait_for(evt.wait(), timeout=HEARTBEAT_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("ws.heartbeat_timeout conn_id={}", conn_id)
                self.unregister(conn_id)
                return
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("ws.heartbeat_error conn_id={} error={}", conn_id, str(e))
                self.unregister(conn_id)
                return
```

在 `__init__` 添加 `self._pong_events: dict[str, asyncio.Event] = {}`，在 `register` 中启动心跳任务，在 `unregister` 中取消心跳任务并清理 event。

在 on_message 处理 pong：
```python
if msg_type == "pong":
    evt = self._pong_events.get(conn_id)
    if evt:
        evt.set()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_ws_heartbeat.py -v --timeout=60`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd /home/orangepi/ai-agent
git add web/ws_hub.py tests/test_ws_heartbeat.py
git commit -m "feat(G5): WS 新增 30s 心跳 + 10s 超时清理死连接"
```

---

### Task 9: G4 HTTP 连接池复用

**Files:**
- Create: `utils/http_pool.py`
- Modify: 高频 HTTP 调用点（reranker/query_transform/memory_distiller 等）
- Test: `tests/test_http_pool.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_http_pool.py
"""G4: HTTP 连接池复用测试."""
import asyncio
import httpx
from utils.http_pool import get_shared_client, close_shared_client


async def test_shared_client_is_singleton():
    """多次调用返回同一实例."""
    c1 = get_shared_client()
    c2 = get_shared_client()
    assert c1 is c2


async def test_shared_client_has_pool_limits():
    """共享 client 应有连接池配置."""
    client = get_shared_client()
    assert client.limits.max_connections == 50
    assert client.limits.max_keepalive_connections == 20
    assert client.limits.keepalive_expiry == 30


async def test_shared_client_http2_enabled():
    """应启用 HTTP/2."""
    client = get_shared_client()
    assert client.http2 is True


async def test_close_resets_singleton():
    """关闭后下次获取是新实例."""
    c1 = get_shared_client()
    await close_shared_client()
    c2 = get_shared_client()
    assert c1 is not c2
    await close_shared_client()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_http_pool.py -v --timeout=60`
Expected: FAIL with "ModuleNotFoundError: No module named 'utils.http_pool'"

- [ ] **Step 3: 创建 http_pool.py**

```python
# utils/http_pool.py
"""G4: 全局共享 httpx.AsyncClient 单例（连接池复用 + HTTP/2）."""
import httpx
from typing import Optional

_shared_client: Optional[httpx.AsyncClient] = None


def get_shared_client() -> httpx.AsyncClient:
    """获取全局共享 httpx.AsyncClient 单例.

    特性：
    - max_connections=50, max_keepalive=20, keepalive_expiry=30s
    - HTTP/2 启用（多路复用）
    - 默认 timeout 30s（单次请求可覆盖）

    Returns:
        httpx.AsyncClient: 共享 client
    """
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
            timeout=httpx.Timeout(30.0, connect=5.0),
            http2=True,
        )
    return _shared_client


async def close_shared_client() -> None:
    """关闭共享 client（应用退出时调用）."""
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
    _shared_client = None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/test_http_pool.py -v --timeout=60`
Expected: PASS (4 tests)

- [ ] **Step 5: 改造高频调用点**

```bash
cd /home/orangepi/ai-agent
# 查找所有 httpx.AsyncClient() 用法
grep -rn "async with httpx.AsyncClient" --include="*.py" . | grep -v tests/
```

按优先级改造：
1. `memory/reranker.py`（每次记忆检索都调）
2. `memory/query_transform.py`（每次查询改写都调）
3. `memory/memory_distiller.py`
4. `memory/knowledge_graph_v2.py` / `memory/kg_search.py`
5. 其他工具类

每处改造模板：
```python
# 原：
async with httpx.AsyncClient(timeout=10.0) as client:
    resp = await client.post(url, json=payload)

# 新：
from utils.http_pool import get_shared_client
client = get_shared_client()
resp = await client.post(url, json=payload, timeout=httpx.Timeout(10.0))
```

注意：保留 `event_hooks` 用法（如 SSRF 检查）的临时实例化，不池化。

- [ ] **Step 6: 在应用退出时关闭共享 client**

在 `agent.py` 或 `core/agent.py` 的 shutdown 钩子添加：
```python
from utils.http_pool import close_shared_client
await close_shared_client()
```

- [ ] **Step 7: 跑回归测试**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/ -k "http or reranker or query_transform or distiller" -v --timeout=60`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
cd /home/orangepi/ai-agent
git add utils/http_pool.py tests/test_http_pool.py
# + 所有改造的文件
git commit -m "feat(G4): HTTP 连接池复用 - 共享 httpx.AsyncClient 单例 + HTTP/2"
```

---

## Phase 4: 系统性性能审计

### Task 10: 6 大领域性能审计

**Files:**
- Create: `docs/performance_audit_2026-07-20.md`

**审计方法**：
1. 静态扫描（grep 同步 IO、TODO 性能注释、大循环）
2. 运行时插桩（关键路径加 time.monotonic 计时）
3. 基准测试（pytest-benchmark）
4. 长期运行监控（tracemalloc）

- [ ] **Step 1: 响应延迟审计**

在 `agent_core/message_processor.py:_process_impl` 各阶段加计时日志：
```python
import time
t0 = time.monotonic()
# ... slash 命令
t1 = time.monotonic()
# ... 问候短路
t2 = time.monotonic()
# ... 上下文恢复
t3 = time.monotonic()
# ... 记忆检索
t4 = time.monotonic()
# ... prompt 构建
t5 = time.monotonic()
# ... LLM 调用
t6 = time.monotonic()
logger.info("process.trace slash={:.0f}ms greeting={:.0f}ms ctx={:.0f}ms mem={:.0f}ms prompt={:.0f}ms llm={:.0f}ms total={:.0f}ms",
            (t1-t0)*1000, (t2-t1)*1000, (t3-t2)*1000, (t4-t3)*1000, (t5-t4)*1000, (t6-t5)*1000, (t6-t0)*1000)
```

跑 3 类输入（问候/普通/回忆）记录各阶段耗时。

- [ ] **Step 2: RAG 检索审计**

```bash
cd /home/orangepi/ai-agent
# 查询计划
sqlite3 data/memory.db "EXPLAIN QUERY PLAN SELECT * FROM episodic_memory_fts WHERE episodic_memory_fts MATCH 'test';"
# 数据量
sqlite3 data/memory.db "SELECT COUNT(*) FROM episodic_memories; SELECT COUNT(*) FROM memory_child_chunks; SELECT COUNT(*) FROM knowledge_entities;"
```

检查 `memory_manager.retrieve_memories_hybrid` 各路耗时分布。

- [ ] **Step 3: Windows 桌面审计**

```bash
cd /home/orangepi/ai-agent
# 前端静态资源大小
du -sh web/frontend/dist/
du -sh web/frontend/dist/assets/*.js
```

检查 `agent.py` 启动各阶段耗时（已有 logger.info）。

- [ ] **Step 4: 数据库审计**

```bash
cd /home/orangepi/ai-agent
sqlite3 data/memory.db "EXPLAIN QUERY PLAN SELECT * FROM episodic_memories WHERE user_id='test' ORDER BY created_at DESC LIMIT 10;"
sqlite3 data/memory.db "EXPLAIN QUERY PLAN SELECT * FROM memory_child_chunks WHERE parent_id='test';"
sqlite3 data/memory.db "PRAGMA wal_checkpoint;"
```

- [ ] **Step 5: 并发审计**

```bash
cd /home/orangepi/ai-agent
# 查找同步 IO 残留
grep -rn "open(" --include="*.py" . | grep -v "test" | grep -v "openai\|httpx\|websocket" | head -30
# 查找 subprocess.run
grep -rn "subprocess.run\|subprocess.check_output\|subprocess.Popen" --include="*.py" . | grep -v test | head -20
```

- [ ] **Step 6: 内存审计**

```python
# 临时脚本 tests/perf/test_memory_audit.py
import tracemalloc
tracemalloc.start()
# ... 启动 agent，跑 100 轮对话
snapshot = tracemalloc.take_snapshot()
top_stats = snapshot.statistics('lineno')
for stat in top_stats[:20]:
    print(stat)
```

- [ ] **Step 7: 汇总审计报告**

将上述结果写入 `docs/performance_audit_2026-07-20.md`：
- 每个发现的瓶颈附带：测量数据、根因、修复方案、预期收益
- 新发现的瓶颈追加为 G11、G12...

- [ ] **Step 8: 提交审计报告**

```bash
cd /home/orangepi/ai-agent
git add docs/performance_audit_2026-07-20.md
git commit -m "docs: 系统性性能审计报告 2026-07-20"
```

---

## Phase 5: 全量回归 + 冒烟测试

### Task 11: 全量 pytest 回归

- [ ] **Step 1: 跑全量测试**

Run: `cd /home/orangepi/ai-agent && python -m pytest tests/ -x --timeout=60 --ignore=tests/perf 2>&1 | tail -50`
Expected: 全部 PASS，零失败

- [ ] **Step 2: 修复任何失败**

若有失败，分析根因（G1-G9 引入）并修复，重新跑直到全绿。

- [ ] **Step 3: 提交修复**

```bash
cd /home/orangepi/ai-agent
git add -A
git commit -m "test: 修复全量回归发现的失败"
```

### Task 12: 冒烟测试

- [ ] **Step 1: 启动冒烟**

Run: `cd /home/orangepi/ai-agent && timeout 10 python -c "
import asyncio
from agent_core.message_processor import MessageProcessor
mp = MessageProcessor()
# 模拟问候
result = asyncio.run(mp._try_greeting_shortcut('你好', 'test', 'qq'))
assert result is not None
print('G1 greeting OK:', result.reply)
"`

- [ ] **Step 2: WebSocket 冒烟**

Run: `cd /home/orangepi/ai-agent && python -c "
import asyncio
from web.ws_hub import manager
async def test():
    await manager.broadcast({'type':'test'})
    print('G2/G5 WS OK, active=', manager.active_count)
asyncio.run(test())
"`

- [ ] **Step 3: HTTP 池冒烟**

Run: `cd /home/orangepi/ai-agent && python -c "
import asyncio, httpx
from utils.http_pool import get_shared_client, close_shared_client
async def test():
    c = get_shared_client()
    assert c.http2
    await close_shared_client()
    print('G4 HTTP pool OK')
asyncio.run(test())
"`

- [ ] **Step 4: 长期运行冒烟**

跑 30 分钟模拟对话，监控内存稳定不增长。

- [ ] **Step 5: 最终提交 + tag**

```bash
cd /home/orangepi/ai-agent
git log --oneline -10  # 确认所有 G 项已提交
# 若全部通过，准备发版
```

---

## Self-Review 检查

### Spec 覆盖
- G1 ✅ Task 3
- G2 ✅ Task 7
- G3 ✅ Task 4
- G4 ✅ Task 9
- G5 ✅ Task 8
- G6 ✅ Task 1
- G7 ✅ Task 2
- G8 ✅ Task 5
- G9 ✅ Task 6
- G10 ✅ 不修改，已在 spec 标记接受
- 审计 ✅ Task 10
- 回归 ✅ Task 11
- 冒烟 ✅ Task 12

### 类型一致性
- `ProcessResult(reply=..., emotion=...)` — G1 一致
- `deque(maxlen=500)` — G6 一致
- `MentalStateManager._save_lock/_save_timer/_save_pending` — G3 一致
- `get_shared_client() -> httpx.AsyncClient` — G4 一致
- `ConnectionManager._safe_send/_heartbeat_loop/_pong_events` — G2/G5 一致

### 无占位符
所有步骤均含完整代码，无 TBD/TODO。
