# xiaoda-agent 情感系统 v2.0 设计文档

> 日期：2026-07-10
> 状态：Approved
> 基于 SPEC: https://www.coze.cn/s/S1Heabqx3rg/

## 1. 架构总览

**方案 A：PAD 为核心 + 双层调度 + 渐进式集成**

```
Layer 3 (消费层):  TTS | Sticker | Nudge | MentalState | XP
Layer 2 (调度层):  emotion_llm (异步) + emotion_simple (即时) → 双层
Layer 1 (模型层):  PAD模型 ← emotion_enum (映射) + emotion_state (状态)
Layer 0 (基础层):  现有 emotion_enum + emotion_simple 不变
```

核心原则：
- PAD 模型作为情绪系统的底层表示层
- 所有现有模块通过适配层接入 PAD，不破坏已有接口
- 双层调度：关键词层即时返回(0ms)，LLM 层后台执行(300-800ms)后异步更新

## 2. 新增模块

### 2.1 N2: PAD 三维情绪模型 (`emotion/pad_model.py`)

**目标**：将硬分类(9类关键词)升级为 Pleasure-Arousal-Dominance 连续空间

**数据结构**：
```python
@dataclass
class PADEmotion:
    P: float  # Pleasure: -1(不悦) ~ +1(愉悦)
    A: float  # Arousal: 0(平静) ~ 1(激动)
    D: float  # Dominance: 0(受控) ~ 1(掌控)
```

**9类中文标签 → PAD 参考值映射**：
```python
EMOTION_PAD_REFERENCE = {
    "喜悦": PADEmotion(0.8, 0.5, 0.6),
    "兴奋": PADEmotion(0.9, 0.9, 0.7),
    "悲伤": PADEmotion(-0.7, 0.3, 0.2),
    "愤怒": PADEmotion(-0.6, 0.8, 0.8),
    "焦虑": PADEmotion(-0.5, 0.7, 0.3),
    "害羞": PADEmotion(0.2, 0.4, 0.2),
    "好奇": PADEmotion(0.3, 0.5, 0.5),
    "思考": PADEmotion(0.0, 0.2, 0.5),
    "恐惧": PADEmotion(-0.8, 0.8, 0.1),
}
```

**方法**：
- `from_emotion(label: str, intensity: float) -> PADEmotion`: 标签+强度 → PAD 值
- `blend(pad1, pad2, weight) -> PADEmotion`: 混合两个 PAD 值
- `to_dict() / from_dict()`: 序列化

**集成点**：
- `emotion_simple.detect_emotion()` 返回值增加 `pad` 字段
- `emotion_state` 新增 `_pad` 字段，衰减时各维度独立衰减
- TTS 可用 P 值微调语速/音高

### 2.2 N3: LLM 深度情绪分析 (`emotion/emotion_llm.py`)

**目标**：关键词层(0延迟但粗糙) + LLM层(300-800ms后台，语义精准)

**接口**：
```python
async def detect_emotion_llm(text: str, context: str = "") -> dict:
    """返回 {"primary", "P", "A", "D", "needs", "style"}"""
```

**双层调度逻辑**（在 message_processor 中）：
1. 关键词结果立即用于 emotion_state 更新和 sticker 选择
2. LLM 结果异步完成后更新 emotion_state（更精确）
3. 超时 500ms 回退到关键词结果

**集成点**：
- 在 `message_processor.py` 的消息处理流水线中调用
- `emotional_memory.anchor()` 的 emotion 参数可选用 LLM 分析结果

### 2.3 N4: 语音情绪提取 (修改 `web/routers/chat.py`)

**目标**：ASR 不仅出文本，还出情感标签

**方案**：文本推断情绪（用 ASR 转写文本 → emotion_simple/PAD 推断）

**修改**：
```python
# speech_to_text() 返回值从 {"text": "..."}
# 改为 {"text": "...", "emotion": "happy", "intensity": 0.6}
```

**向下兼容**：旧消费方忽略 emotion 字段即可

### 2.4 N5: 情绪记忆 ↔ 情绪状态联动

**目标**：打通 emotional_memory 和 emotion_state，形成闭环

**联动逻辑**：
1. `emotional_memory.anchor()` 末尾调用 `emotion_state.update()`
2. `emotional_memory.recall()` 结果 → `emotion_state.shift_pad()` 微调(10%)
3. `emotion_state` 新增 `shift_pad(pad, weight)` 方法

### 2.5 N6: 重聚反思 (`emotion/reunion_reflection.py`)

**目标**：用户回来时回顾离开期间的事，不是简单"你回来啦"

**接口**：
```python
async def generate_reunion_message(
    idle_seconds: float,
    last_emotion: tuple[str, float],
    emotional_memories: list,
    portrait: dict,
) -> str:
```

**三档逻辑**：
- 短离(<30min)：简单"回来啦～"
- 中离(30min-4h)：提及离开前的话题
- 长离(>4h)：关心+重聚反思
- 离开前情绪低落：优先关心

**集成点**：在 `nudge_engine._check_greeting()` 中替换简单问候

### 2.6 N7: 微信桥接骨架 (`wechat_bot_adapter.py`)

**目标**：仅预留接口骨架，不实现完整 iLink 协议

**内容**：类定义 + 方法签名 + TODO 注释，结构对齐 `qq_bot_adapter.py`

## 3. 已有代码改进

### 3.1 I1: emotion_simple PAD 扩展

**现状**：`detect_emotion()` 返回 `{primary, valence, intensity}`

**改进**：返回值增加 `pad: PADEmotion`，由 `EMOTION_PAD_REFERENCE[primary] * intensity` 计算

**向下兼容**：旧字段保留

### 3.2 I2: emotion_state 多情绪共存

**现状**：单情绪 `_current`，新情绪需超过旧情绪才替换

**改进**：
- 新增 `_active_emotions: dict[str, float]`（最多3个，按 intensity 排序）
- `get_current()` 仍返回主情绪（兼容）
- 新增 `get_active_emotions() -> list[tuple[str, float]]` 返回列表
- 各情绪独立衰减

### 3.3 I3: nudge_engine 情感记忆注入

**改进**：`_generate_idle_greeting()` 生成前先 `emotional_memory.recall()` 近期事件，注入 prompt

### 3.4 I4: XP → 行为映射

**新增** `XP_BEHAVIOR_MAP` 常量：
```python
XP_BEHAVIOR_MAP = {
    XPLevel.LV1_STRANGER: {"proximity": "far", "initiate": False, "special": []},
    XPLevel.LV2_ACQUAINTANCE: {"proximity": "mid", "initiate": False, "special": []},
    XPLevel.LV3_FRIEND: {"proximity": "near", "initiate": True, "special": ["wave"]},
    XPLevel.LV4_CLOSE_FRIEND: {"proximity": "close", "initiate": True, "special": ["wave", "hug"]},
    XPLevel.LV5_SOULMATE: {"proximity": "intimate", "initiate": True, "special": ["wave", "hug", "cuddle"]},
    XPLevel.LV6_ETERNAL: {"proximity": "intimate", "initiate": True, "special": ["wave", "hug", "cuddle", "kiss"]},
}
```

### 3.5 I5: TTS 风格细化

**修改** `emotion_enum.py` 的 `TTS_STYLE_MAP`：
- MOVED → "caring"（温柔关切，不是 sad）
- PLAYFUL → "playful"（保留调皮，不是 happy）
- POUT → "coquettish"（撒娇用独立风格，不是 shy）

## 4. 实施路线图

```
P0 (Phase 1) ─┬─ N2: PAD三维情绪模型 ← emotion_simple扩展(I1)
              ├─ I5: TTS风格细化 ← 纯配置改动
              └─ I4: XP→行为映射 ← 纯常量新增

P1 (Phase 2) ─┬─ N3: LLM深度情绪分析 ← message_processor集成
              ├─ I2: 多情绪共存 ← emotion_state改进
              └─ N5: 情绪记忆↔状态联动 ← emotional_memory+emotion_state

P2 (Phase 3) ─┬─ N4: 语音情绪提取 ← chat.py扩展
              ├─ N6: 重聚反思 ← nudge_engine扩展
              └─ N7: 微信桥接骨架 ← 仅骨架
```

## 5. 测试策略

每个模块采用 TDD：先写测试再写实现。

**测试覆盖**：
- `tests/test_pad_model.py`: PAD 数据结构、映射、混合
- `tests/test_emotion_llm.py`: LLM 情绪分析（mock LLM 调用）
- `tests/test_emotion_state_multi.py`: 多情绪共存、衰减、shift_pad
- `tests/test_emotion_memory_linkage.py`: 记忆↔状态联动
- `tests/test_reunion_reflection.py`: 三档重聚反思
- `tests/test_speech_emotion.py`: 语音情绪提取
- `tests/test_xp_behavior_map.py`: XP→行为映射
- `tests/test_tts_style_refinement.py`: TTS 风格细化

**量化评分标准**：
- 所有新测试通过
- 现有 955 个测试全通过
- 情绪检测覆盖度：16种情绪 × PAD 三维 → 评分
- 代码覆盖率不低于 85%

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| emotion_state 多情绪改动破坏现有测试 | get_current() 保持兼容，新增 get_active_emotions() |
| LLM 情绪分析延迟过高 | 500ms 超时回退到关键词结果 |
| TTS 风格名不被 MiMo 支持 | 保留旧映射作为 fallback |
| emotional_memory 联动产生循环依赖 | 用延迟导入 + fire-and-forget |
