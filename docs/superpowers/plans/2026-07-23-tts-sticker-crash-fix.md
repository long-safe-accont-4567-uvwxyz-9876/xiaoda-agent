# TTS 失控 + 表情包不发送 + 日志崩溃防御 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 TTS 否定误触发、表情包 fallback 缺失、USB 只读崩溃 3 个生产 bug

**Architecture:** Bug1 改 `_detect_voice_intent` 为三态 + 调用方适配；Bug2 加三级 fallback + 情绪别名；Bug7 补全 logger.add 防御

**Tech Stack:** Python 3.11, pytest, loguru

## Global Constraints
- 所有指令用中文
- 不硬编码系统功能
- prompt 模板用 str.replace 不用 str.format
- 后台慢任务需有超时保护

---

### Task 1: TTS 三态语音意图检测

**Files:**
- Modify: `agent_core/message_processor.py:1899-1910` (`_detect_voice_intent`) + `:350-351` (调用方)
- Test: `tests/test_voice_intent_tri_state.py`

**Interfaces:**
- Produces: `_detect_voice_intent(user_input: str) -> str` 返回 `"none"` / `"on"` / `"off"`

- [ ] **Step 1: 写失败测试**

```python
"""测试三态语音意图检测：none / on / off"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_detect_voice_intent_off_negation():
    """否定+语音词 → off"""
    from agent_core.message_processor import MessageProcessorMixin
    p = MessageProcessorMixin.__new__(MessageProcessorMixin)
    assert p._detect_voice_intent("不要发语音了") == "off"
    assert p._detect_voice_intent("不用语音了") == "off"
    assert p._detect_voice_intent("别发语音") == "off"
    assert p._detect_voice_intent("关掉语音") == "off"
    assert p._detect_voice_intent("关闭语音模式") == "off"


def test_detect_voice_intent_on_positive():
    """语音词无否定 → on"""
    from agent_core.message_processor import MessageProcessorMixin
    p = MessageProcessorMixin.__new__(MessageProcessorMixin)
    assert p._detect_voice_intent("发语音") == "on"
    assert p._detect_voice_intent("听你说") == "on"
    assert p._detect_voice_intent("用声音回答") == "on"
    assert p._detect_voice_intent("念给我听") == "on"


def test_detect_voice_intent_none_no_keyword():
    """无语音词 → none"""
    from agent_core.message_processor import MessageProcessorMixin
    p = MessageProcessorMixin.__new__(MessageProcessorMixin)
    assert p._detect_voice_intent("你好") == "none"
    assert p._detect_voice_intent("画一张图") == "none"
    assert p._detect_voice_intent("（静静地看着她）") == "none"


def test_detect_voice_intent_off_not_triggered_by_false_positive():
    """含"不"但非关语音 → 不误判 off（如"不太好"不含语音词→none）"""
    from agent_core.message_processor import MessageProcessorMixin
    p = MessageProcessorMixin.__new__(MessageProcessorMixin)
    assert p._detect_voice_intent("不太好吧") == "none"
    assert p._detect_voice_intent("不用了谢谢") == "none"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_voice_intent_tri_state.py -v`
Expected: FAIL (`_detect_voice_intent` 返回 bool 非 str)

- [ ] **Step 3: 实现三态检测**

`agent_core/message_processor.py:1899` 替换 `_detect_voice_intent`:

```python
def _detect_voice_intent(self, user_input: str) -> str:
    """检测语音意图：三态返回 'none' / 'on' / 'off'。

    - 'off': 含否定词 + 语音关键词（如"不要发语音了"）
    - 'on': 含语音关键词但不含否定词
    - 'none': 不含语音关键词
    """
    voice_keywords = [
        "语音", "声音", "说话", "朗读", "念给我", "读给我",
        "用声音", "听你", "听听你", "发语音", "生成语音",
        "语音回复", "语音消息", "说给我听", "念出来",
        "tts", "voice",
    ]
    negation_prefixes = ["不要", "不用", "别", "关掉", "关闭", "停止", "取消"]
    q = user_input.lower()
    has_voice_kw = any(kw in q for kw in voice_keywords)
    if not has_voice_kw:
        return "none"
    # 检测否定：否定词在语音词前 2~4 字符范围内
    for kw in voice_keywords:
        idx = q.find(kw)
        if idx == -1:
            continue
        prefix = q[max(0, idx - 4):idx]
        if any(neg in prefix for neg in negation_prefixes):
            return "off"
    return "on"
```

- [ ] **Step 4: 适配调用方**

`agent_core/message_processor.py:350-351` 替换:

```python
voice_intent = self._detect_voice_intent(clean_input)
if voice_intent == "off":
    self.set_voice_mode(False)
    force_voice = False
elif voice_intent == "on":
    force_voice = not self._voice_mode
else:
    force_voice = False
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_voice_intent_tri_state.py -v`
Expected: PASS (4/4)

- [ ] **Step 6: 提交**

```bash
git add agent_core/message_processor.py tests/test_voice_intent_tri_state.py
git commit -m "fix: _detect_voice_intent 改三态检测，支持否定关语音"
```

---

### Task 2: 表情包三级 fallback + crying 别名

**Files:**
- Modify: `agent_core/tool_executor.py:540-555` (`get_sticker_info` sticker 分支)
- Modify: `emotion/emotion_enum.py` (`EMOTION_ALIASES` 增加 crying)
- Test: `tests/test_sticker_fallback.py`

**Interfaces:**
- Consumes: `StickerManager.pick_by_name(filename)`, `StickerManager.pick(emotion)`, `resolve_emotion(label)`, `STICKER_FALLBACK`

- [ ] **Step 1: 写失败测试**

```python
"""测试表情包三级 fallback：文件名→情绪名→resolve_emotion→文本检测"""
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
import sys
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class _Stub:
    """最小化 ToolExecutorMixin stub"""
    pass


def _make_stub(sticker_dir: Path):
    from agent_core.tool_executor import ToolExecutorMixin
    from emotion.sticker_manager import StickerManager
    from core.degradation_strategy import get_degradation_strategy
    stub = _Stub()
    stub.sticker_manager = StickerManager(sticker_dir)
    # mock degradation strategy 允许 emotion
    with patch.object(get_degradation_strategy(), 'is_feature_available', return_value=True):
        pass
    return stub


def test_sticker_by_emotion_name_shy(tmp_path):
    """[sticker:shy] → pick_by_name 失败 → pick('shy') 成功"""
    (tmp_path / "shy").mkdir()
    (tmp_path / "shy" / "shy_脸红.jpg").write_bytes(b"x")
    (tmp_path / "neutral").mkdir()
    (tmp_path / "neutral" / "neutral_平静.jpg").write_bytes(b"x")
    from agent_core.tool_executor import ToolExecutorMixin
    stub = ToolExecutorMixin.__new__(ToolExecutorMixin)
    stub.sticker_manager = _make_stub(tmp_path).sticker_manager
    reply = "测试 [sticker:shy]"
    with patch("agent_core.tool_executor.get_degradation_strategy") as m_ds:
        m_ds.return_value.is_feature_available.return_value = True
        clean, path = stub.get_sticker_info(reply)
    assert path is not None
    assert "shy" in str(path)
    assert "[sticker:" not in clean


def test_sticker_crying_maps_to_sad(tmp_path):
    """[sticker:crying] → resolve_emotion('crying')→SAD → pick('sad')"""
    (tmp_path / "sad").mkdir()
    (tmp_path / "sad" / "sad_含泪.jpg").write_bytes(b"x")
    (tmp_path / "neutral").mkdir()
    (tmp_path / "neutral" / "neutral_平静.jpg").write_bytes(b"x")
    from agent_core.tool_executor import ToolExecutorMixin
    from emotion.sticker_manager import StickerManager
    stub = ToolExecutorMixin.__new__(ToolExecutorMixin)
    stub.sticker_manager = StickerManager(tmp_path)
    reply = "呜呜 [sticker:crying]"
    with patch("agent_core.tool_executor.get_degradation_strategy") as m_ds:
        m_ds.return_value.is_feature_available.return_value = True
        clean, path = stub.get_sticker_info(reply)
    assert path is not None
    assert "sad" in str(path)


def test_sticker_nonexistent_falls_back_to_text_emotion(tmp_path):
    """[sticker:不存在的] → 全部 fallback 失败 → 文本情绪检测"""
    (tmp_path / "neutral").mkdir()
    (tmp_path / "neutral" / "neutral_平静.jpg").write_bytes(b"x")
    from agent_core.tool_executor import ToolExecutorMixin
    from emotion.sticker_manager import StickerManager
    stub = ToolExecutorMixin.__new__(ToolExecutorMixin)
    stub.sticker_manager = StickerManager(tmp_path)
    reply = "不存在的标签 [sticker:xyz_fake]"
    with patch("agent_core.tool_executor.get_degradation_strategy") as m_ds:
        m_ds.return_value.is_feature_available.return_value = True
        clean, path = stub.get_sticker_info(reply)
    # 至少返回 neutral 表情包（不返回 None）
    assert path is not None


def test_resolve_emotion_crying_alias():
    """crying → Emotion.SAD 别名"""
    from emotion.emotion_enum import resolve_emotion, Emotion
    assert resolve_emotion("crying") == Emotion.SAD
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_sticker_fallback.py -v`
Expected: FAIL (crying 别名缺失 + fallback 缺失)

- [ ] **Step 3: 增加 crying 别名**

`emotion/emotion_enum.py` 在 `EMOTION_ALIASES` 的 SAD 区域增加:
```python
    "crying": Emotion.SAD,
```

- [ ] **Step 4: 实现三级 fallback**

`agent_core/tool_executor.py:540-555` 的 `[sticker:filename]` 分支替换:

```python
_sticker_match = _re.search(r'\[sticker:([^\]]+)\]', reply)
if _sticker_match:
    filename = _sticker_match.group(1).strip()
    clean_reply = _re.sub(r'\[sticker:[^\]]*\]', '', reply).rstrip()
    clean_reply = self.sticker_manager.strip_emotion_tag(clean_reply)
    if (self.sticker_manager.available
            and get_degradation_strategy().is_feature_available("emotion")):
        # 三级 fallback: 文件名 → 情绪名 → resolve_emotion 映射 → 文本检测
        path = self.sticker_manager.pick_by_name(filename)
        if path:
            return clean_reply, path
        path = self.sticker_manager.pick(filename)
        if path:
            return clean_reply, path
        from emotion.emotion_enum import resolve_emotion, STICKER_FALLBACK
        resolved = resolve_emotion(filename)
        mapped = STICKER_FALLBACK.get(resolved, "")
        if mapped:
            path = self.sticker_manager.pick(mapped)
            if path:
                return clean_reply, path
        detected = self.sticker_manager.detect_emotion(clean_reply) or "neutral"
        path = self.sticker_manager.pick(detected)
        if path:
            return clean_reply, path
        logger.warning(f"sticker.not_found name={filename}")
    return clean_reply, None
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_sticker_fallback.py -v`
Expected: PASS (4/4)

- [ ] **Step 6: 提交**

```bash
git add agent_core/tool_executor.py emotion/emotion_enum.py tests/test_sticker_fallback.py
git commit -m "fix: 表情包三级 fallback + crying→SAD 别名"
```

---

### Task 3: USB 只读日志崩溃防御

**Files:**
- Modify: `utils/logging_config.py` (所有 `logger.add` 加 try/except)
- Test: `tests/test_logging_usb_ro.py`

- [ ] **Step 1: 写失败测试**

```python
"""测试 setup_logging 在 USB 只读时不崩溃"""
import pytest
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_setup_logging_no_crash_on_readonly_fs(monkeypatch):
    """logger.add 文件 sink 在只读 FS 上不崩溃"""
    from utils.logging_config import setup_logging
    # mock 文件 sink 抛 OSError
    with patch("loguru.logger.add", side_effect=[OSError("Read-only"), OSError("Read-only"), MagicMock(), MagicMock(), MagicMock()]):
        try:
            setup_logging()
        except (OSError, PermissionError):
            pytest.fail("setup_logging should not raise on read-only FS")


def test_setup_logging_falls_back_gracefully():
    """setup_logging 任何 sink 失败都不应传播异常"""
    from utils.logging_config import setup_logging
    try:
        setup_logging()
    except Exception as e:
        pytest.fail(f"setup_logging raised: {e}")
```

- [ ] **Step 2: 运行测试确认失败/通过**

Run: `pytest tests/test_logging_usb_ro.py -v`

- [ ] **Step 3: 补全 try/except**

扫描 `utils/logging_config.py` 所有 `logger.add` 调用，对文件 sink 补全 try/except `(OSError, PermissionError)`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_logging_usb_ro.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add utils/logging_config.py tests/test_logging_usb_ro.py
git commit -m "fix: setup_logging 所有 logger.add 加 USB 只读防御"
```

---

### Task 4: 全量回归 + security review

- [ ] Run: `pytest -q`
- [ ] Fix any failures
- [ ] TRAE-security-review on changed files
- [ ] Final commit if needed
