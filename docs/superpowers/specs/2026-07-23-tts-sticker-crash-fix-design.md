# TTS 失控 + 表情包不发送 + 日志崩溃防御修复设计

> 日期: 2026-07-23
> 状态: 设计中
> 来源: 生产日志排查（USB DB agent.db id=1992~2001 + agent_2026-07-23.json + crash.log）

## 1. 问题概述

翻查生产日志发现 3 类需就地修复的 bug：

### Bug 1: TTS 失控（用户明确要关，还在生成）
- **证据**: id=1993 (06:48) 用户说"不要发语音了"，但 06:49:20 / 06:50:40 / 06:58:05 仍出现 `tts.synthesized`
- **影响**: 用户明确拒绝语音后仍被强制发语音，体验极差 + 浪费 TTS 资源

### Bug 2: 表情包不发送
- **证据**: 今日日志反复 `sticker.not_found name=crying` / `name=shy`（6 次+）
- **影响**: LLM 输出 `[sticker:情绪名]` 但系统按文件名查找失败，表情包完全不发送

### Bug 7: USB 只读导致启动崩溃
- **证据**: nahida-agent / xiaoda-agent crash.log 各一次 `PermissionError` / `OSError: Read-only file system`
- **影响**: USB 盘只读/权限异常时 agent 直接崩溃无法启动

## 2. 根因分析

### Bug 1 根因: `_detect_voice_intent` 不处理否定

`agent_core/message_processor.py:1899`:
```python
def _detect_voice_intent(self, user_input: str) -> bool:
    voice_keywords = ["语音", "声音", "说话", "发语音", ...]
    q = user_input.lower()
    return any(kw in q for kw in voice_keywords)
```

"不要发语音了"含"发语音"→返回 True→`force_voice=True`→TTS 照生成。

且无自然语言关语音机制：`set_voice_mode(False)` 只能通过 `/voice off` 斜杠命令触发，用户说"不要发语音了"不会关闭 voice_mode。

### Bug 2 根因: `pick_by_name` 失败后无 fallback

`agent_core/tool_executor.py:540`:
```python
_sticker_match = _re.search(r'\[sticker:([^\]]+)\]', reply)
if _sticker_match:
    filename = _sticker_match.group(1).strip()  # "crying" / "shy" / "surprised"
    path = self.sticker_manager.pick_by_name(filename)  # 按文件名查
    if path:
        return clean_reply, path
    logger.warning(f"sticker.not_found name={filename}")  # 失败就放弃
    return clean_reply, None  # 不发表情包
```

LLM 输出的是**情绪名**（crying/shy/surprised）不是**文件名**（sad_含泪委屈.jpg）。`pick_by_name("shy")` 找文件名为"shy"的文件→失败。且：
- "crying" 不是合法情绪目录（USB 有 sad/shy/surprised 等 16 个目录，无 crying/）
- `pick_by_name` 失败后直接返回 None，无 fallback 到按情绪查找

### Bug 7 根因: 部分 `logger.add` 未加 try/except

`utils/logging_config.py` line 104/113 的 `logger.add`（stderr/JSON sink）在 L6 修复的 try/except 块（line 128-159）之外。虽然这两行写的是 stderr/JSON 不是文件，但 nahida crash.log 显示 line 20 的 `logger.add` 也曾崩溃——说明存在多个未被保护的 `logger.add` 调用路径。

## 3. 修复设计

### Bug 1 修复: 三态语音意图检测 + 自然语言关语音

**方案**: `_detect_voice_intent` 改为返回三态枚举 `VoiceIntent`（NONE / TURN_ON / TURN_OFF），检测否定前缀。

```
VoiceIntent = Literal["none", "on", "off"]
```

**否定检测规则**:
- 关闭意图: 用户输入含"不要"/"不用"/"别"/"关掉"/"关闭" + 语音关键词 → TURN_OFF
- 开启意图: 含语音关键词但不含否定词 → TURN_ON
- 无意图: 不含语音关键词 → NONE

**调用方修改** (`message_processor.py:350-351`):
```python
voice_intent = self._detect_voice_intent(clean_input)  # 三态
if voice_intent == "off":
    self.set_voice_mode(False)  # 自然语言关语音
    force_voice = False
elif voice_intent == "on":
    force_voice = not self._voice_mode  # 已开则不强制
else:
    force_voice = False
```

**TTS 引擎防御**: `emotion/tts_engine.py` 已有 `[sticker:]` 标签清理（line 495），同时清理 `[emotion:]`——无需改。

### Bug 2 修复: 三级 fallback 表情包选取

**方案**: `get_sticker_info` 中 `pick_by_name` 失败后，三级 fallback：

```python
# 1. 精确文件名匹配
path = self.sticker_manager.pick_by_name(filename)
if path:
    return clean_reply, path

# 2. 按情绪名匹配（filename 当情绪名用，如 "shy" → pick("shy")）
path = self.sticker_manager.pick(filename)
if path:
    return clean_reply, path

# 3. resolve_emotion 映射（如 "crying" → SAD → "sad" 目录）
from emotion.emotion_enum import resolve_emotion, STICKER_FALLBACK
resolved = resolve_emotion(filename)
mapped = STICKER_FALLBACK.get(resolved, "")
if mapped:
    path = self.sticker_manager.pick(mapped)
    if path:
        return clean_reply, path

# 4. 最终 fallback: 文本情绪检测
detected = self.sticker_manager.detect_emotion(clean_reply) or "neutral"
path = self.sticker_manager.pick(detected)
if path:
    return clean_reply, path

logger.warning(f"sticker.not_found name={filename}")
return clean_reply, None
```

**情绪名别名扩展**: `resolve_emotion` 增加 "crying" → SAD 映射（当前缺失）。

### Bug 7 修复: 补全所有 `logger.add` 的 try/except

**方案**: 扫描 `utils/logging_config.py` 所有 `logger.add` 调用，对涉及文件 sink 的全部加 try/except `(OSError, PermissionError)` 保护。stderr/JSON sink 本身不涉及文件，但为防御性也加保护。

## 4. 不修复（推迟到单独 spec）

- **Bug 3 model_used 空**: 代码链路正确（`get_current_chat_model()` 返回 `"mimo-v2.5"`），但 1872/1873 条生产记录空。L5 修复 07-22 提交，生产 agent 可能未重启。需运行时验证，非代码 bug。
- **Bug 4 后台任务慢** (`_encode_task` 367s 等): 性能优化，需系统性重构，单独 spec。
- **Bug 5 memory.retrieve_global_timeout**: 同 Bug 4，性能类。
- **Bug 6 kg.merge_entity_failed**: 已有降级路径（FTS5 触发器失败降级），非阻塞。

## 5. 验收标准

- **AC1**: `_detect_voice_intent("不要发语音了")` 返回 TURN_OFF，不触发 TTS
- **AC2**: `_detect_voice_intent("发语音")` / `_detect_voice_intent("听你说")` 返回 TURN_ON
- **AC3**: `get_sticker_info` 对 `[sticker:crying]` / `[sticker:shy]` / `[sticker:surprised]` 均返回非 None 路径
- **AC4**: `pick_by_name("不存在")` 失败后 fallback 到情绪匹配，不返回 None（除非真无表情包）
- **AC5**: `setup_logging` 在 USB 只读时不崩溃，降级到 stderr-only
- **AC6**: 新增测试全绿 + 全量回归通过

## 6. 文件变更

| 文件 | 变更 |
|------|------|
| `agent_core/message_processor.py` | `_detect_voice_intent` 改三态 + 调用方适配 |
| `agent_core/tool_executor.py` | `get_sticker_info` 三级 fallback |
| `emotion/emotion_enum.py` | `resolve_emotion` 增加 crying→SAD 别名 |
| `utils/logging_config.py` | 所有 `logger.add` 加 try/except |
| `tests/test_voice_intent_tri_state.py` | 新增: 三态语音意图测试 |
| `tests/test_sticker_fallback.py` | 新增: 表情包 fallback 测试 |
| `tests/test_logging_usb_ro.py` | 新增: USB 只读不崩溃测试 |
