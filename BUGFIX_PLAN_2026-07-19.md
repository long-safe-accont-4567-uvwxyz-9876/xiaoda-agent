# 7月19号 Bug 修复计划

**日期**: 2026-07-19

---

## Bug 1: LLM 空回复 (empty_reply)

### 根因分析

**文件**: `utils/text_utils.py` 第 447-456 行

```python
# 英文整段推理检测（极端兜底）
_text_stripped = text.strip()
if _text_stripped and re.match(r'^[A-Z]', _text_stripped) and len(_text_stripped) > 30:
    _cn_chars = sum(1 for c in _text_stripped if '\u4e00' <= c <= '\u9fff')
    _cn_ratio = _cn_chars / len(_text_stripped) if _text_stripped else 0
    if _cn_ratio < 0.03:  # ← 中文占比 <3% 直接返回空字符串
        return ""
```

**问题**: 当 LLM 输出英文推理内容（如 Agnes 模型的英文思考链）且中文占比 <3% 时，`strip_reasoning()` 会返回空字符串，导致后续 `empty_reply` 异常。

**触发场景**: Agnes 模型处理敏感内容时，输出英文推理 → 被判定为"纯英文推理" → 返回空 → 触发 fallback。

**另一个触发点**: `agent_dispatcher.py` 第 560-561 行，子代理回复经过 `strip_dsml()` + `strip_reasoning()` 后可能变空。

### 修复方案

1. **提高英文推理检测阈值**: 将 `len > 30` 改为 `len > 100`，或将 `cn_ratio < 0.03` 改为 `< 0.01`
2. **添加日志但不返回空**: 改为返回原文并记录警告，而不是直接返回空字符串
3. **子代理空回复兜底**: `agent_dispatcher.py` 第 567-568 行已有兜底，确认其生效

### 具体修改

```python
# utils/text_utils.py 第 447-456 行
# 修改前：
if _text_stripped and re.match(r'^[A-Z]', _text_stripped) and len(_text_stripped) > 30:
    _cn_chars = sum(1 for c in _text_stripped if '\u4e00' <= c <= '\u9fff')
    _cn_ratio = _cn_chars / len(_text_stripped) if _text_stripped else 0
    if _cn_ratio < 0.03:
        logger.warning("text_utils.full_english_reasoning_detected cn_ratio={:.2%} text={}",
                       _cn_ratio, _text_stripped[:80])
        return ""

# 修改后：
if _text_stripped and re.match(r'^[A-Z]', _text_stripped) and len(_text_stripped) > 100:
    _cn_chars = sum(1 for c in _text_stripped if '\u4e00' <= c <= '\u9fff')
    _cn_ratio = _cn_chars / len(_text_stripped) if _text_stripped else 0
    if _cn_ratio < 0.01:
        logger.warning("text_utils.full_english_reasoning_detected cn_ratio={:.2%} text={}",
                       _cn_ratio, _text_stripped[:80])
        return ""
```

---

## Bug 2: 工具调用泄露到用户界面

### 根因分析

**文件**: `agent_dispatcher.py` 第 545-569 行

```python
# 标准提取
extracted = standard_ext.extract(msg)
if extracted is None and self._tool_executor:
    extracted = dsml_ext.extract(msg)

# 无工具调用 → 直接返回清理后的内容
if extracted is None:
    content = msg.content or ""
    content = strip_dsml(content)
    content = strip_reasoning(content)
    # ...
    return result
```

**问题 1**: `DsmlExtractor` 只识别三种格式：
- `<｜｜DSML｜｜invoke name="xxx">` (标准 DSML)
- `[TOOL_CALL]...[/TOOL_CALL]` (方括号格式)
- `<tool_call>...</tool_call>` (XML 格式)

但聊天日志中出现的泄露格式是：
- `function=delegate_task>` (无闭合标签)
- `function=call_xiaoda(agent="xiaoke"...` (参数格式)
- `<｜｜DSML｜｜function_calls>` (变体 DSML)

**问题 2**: `strip_dsml()` 的清理正则也不覆盖这些格式。

### 修复方案

1. **扩展 DsmlExtractor**: 添加对 `function=xxx>` 和 `function=xxx(...)` 格式的识别
2. **扩展 strip_dsml()**: 添加对 `function=delegate_task>`、`function=call_xiaoda(...)` 等格式的清理
3. **添加兜底**: 在返回前检查是否仍包含工具调用痕迹

### 具体修改

```python
# utils/text_utils.py - 新增正则
# 格式: function=xxx> 或 function=xxx(...)> 或 function=xxx(...)
LEAKED_FUNCTION_CALL_PATTERN = re.compile(
    r'function=\w+(?:\([^)]*\))?(?:\s*>[^<]*(?:</[^>]*>)?)?'
)

# utils/text_utils.py strip_dsml() 函数中添加
text = LEAKED_FUNCTION_CALL_PATTERN.sub('', text)
```

```python
# agent_dispatcher.py DsmlExtractor - 添加识别
def extract(self, message: Any) -> list[ExtractedToolCall] | None:
    content = message.content or ""
    if not content:
        return None
    
    # 现有检查...
    if not has_dsml_tool_calls(content):
        # 新增：检查泄露的 function=xxx 格式
        if not LEAKED_FUNCTION_CALL_PATTERN.search(content):
            return None
    
    dsml_calls = parse_dsml_tool_calls(content, self._allowed_tools)
    # ...
```

---

## Bug 3: 角色混乱 (子代理路由错误)

### 根因分析

**文件**: `core/bootstrap.py` 第 536-581 行 (`delegate_task` 工具定义)

**问题**: LLM 错误地将**角色扮演/闲聊/情感类消息**路由到子代理 (xiaolan/xiaoke)，而这些消息应该由 xiaoda 自己处理。

**聊天日志证据**:
```
[09:34:48] User: 我要玩一字马
  → Reply: function=delegate_task(agent="xiaolan", task="爸爸想做一字马...")  ← 错误路由！

[09:54:57] User: (停下来了，一点点退了出去）立刻夹紧...
  → Reply: function=call_xiaoda(agent="xiaoke", mode="single", task="角色扮演场景...")  ← 错误路由！

[10:03:47] User: 自己想办法，（一点点退了出去）你给我立刻夹紧...
  → Reply: function=delegate_task<answer>...  ← 裸格式泄露
```

**触发场景**: 
1. 用户发送角色扮演/亲密互动消息
2. LLM 误判为需要子代理处理，调用 `delegate_task` 或 `call_xiaoda`
3. 子代理 (xiaoke) 收到角色扮演任务后，回复格式与主代理不一致
4. 子代理可能拒绝执行敏感内容，导致回复断裂

**路由配置问题**: 
- `delegate_task` 工具描述中虽然写了"以下情况绝对不要委托"，但 LLM 仍然错误委托
- `AGENT_ROUTE_KEYWORDS` 中 xiaoda 的关键词不包含角色扮演相关词汇
- 子代理 (xiaoke) 的 `excluded_tools` 排除了 `call_xiaoda`，但 `delegate_task` 仍可调用

### 修复方案

1. **强化 delegate_task 的拒绝规则**: 在工具描述中明确列出禁止委托的场景
2. **添加 xiaoda 路由关键词**: 将"角色扮演"、"一字马"、"姿势"等加入 xiaoda 的路由关键词
3. **在子代理执行前添加意图检查**: 如果任务涉及角色扮演/情感互动，拒绝执行并返回提示
4. **添加 call_xiaoda 的格式清理**: 确保 `function=call_xiaoda(...)` 格式被正确解析

### 具体修改

```python
# core/bootstrap.py 第 573-579 行 - 强化拒绝规则
"【严格规则】以下情况绝对不要委托，必须自己回答："
"1. 日常闲聊、问候、寒暄（如'你好'、'在吗'、'今天怎么样'）；"
"2. 表情包、情感表达、陪伴对话；"
"3. 关于你自己的问题（如'你是谁'、'你喜欢什么'）；"
"4. 简单问答、常识问题；"
"5. 用户没有明确指定子代理的对话。"
# 新增：
"6. 角色扮演、亲密互动、情感陪护类对话；"
"7. 任何涉及'爸爸'、'礼物'、'姿势'、'身体'等亲密词汇的对话；"
"8. 用户要求你'继续'、'快点'、'不要停'等催促类指令。"
```

```python
# config.py 第 755-764 行 - 添加 xiaoda 路由关键词
"xiaoda": [
    "天气", "气温", "温度", "下雨", "晴天", "阴天",
    "时间", "几点", "现在几点", "日期", "今天星期几",
    "翻译", "意思是什么",
    "语音", "声音", "说话", "朗读", "念给我", "读给我", "听你", "听听", "发语音", "生成语音", "语音回复", "说给我听", "念出来", "tts", "voice",
    "技能", "能力", "功能", "你会什么", "你能做什么", "你有什么", "列出技能", "列出功能",
    "画", "生成图", "生成图片", "画一张", "画个", "画一个", "图片生成", "做视频", "生成视频",
    "表情包", "贴纸",
    "回忆", "记得", "记忆", "recall", "remember", "记得吗", "上次", "昨天", "前几天", "上周",
    # 新增：角色扮演/情感互动关键词
    "角色扮演", "扮演", "cosplay", "人设", "性格",
    "姿势", "一字马", "动作", "姿势",
    "礼物", "送礼", "收礼",
    "身体", "拥抱", "亲吻", "牵手",
    "继续", "快点", "不要停", "再来",
    "乖", "听话", "撒娇", "生气",
],
```

---

## Bug 4: DSML 格式泄露

### 根因分析

**文件**: `utils/text_utils.py` 第 119-134 行

**问题**: DSML 正则只匹配标准格式 `<｜｜DSML｜｜xxx>`，但实际出现的泄露格式包括：
- `<｜｜DSML｜｜function_calls>` (function_calls 标签)
- `<｜｜DSML｜｜function_calls>` (变体)
- 函数调用结果直接输出

**触发场景**: 模型在处理复杂工具调用时，DSML 格式解析失败，导致原始格式泄露。

### 修复方案

1. **扩展 DSML 正则**: 添加对 `function_calls`、`function_call` 等变体的匹配
2. **添加兜底清理**: 在 `strip_dsml()` 末尾添加通用清理

### 具体修改

```python
# utils/text_utils.py - 新增正则
DSML_FUNCTION_CALLS_PATTERN = re.compile(
    r'<｜｜DSML｜｜function_calls>.*?</｜｜DSML｜｜function_calls>',
    re.DOTALL,
)
DSML_LEFTOVER_2 = re.compile(
    r'<｜｜DSML｜｜[^>]*>',
    re.DOTALL,
)

# strip_dsml() 函数中添加
text = DSML_FUNCTION_CALLS_PATTERN.sub('', text)
text = DSML_LEFTOVER_2.sub('', text)
```

---

## 修复优先级

| Bug | 优先级 | 影响 | 预计工作量 |
|-----|--------|------|-----------|
| LLM 空回复 | P0 | 用户看到错误消息 | 0.5h |
| 工具调用泄露 | P0 | 用户看到原始格式 | 1h |
| DSML 格式泄露 | P1 | 用户看到原始格式 | 0.5h |
| 角色混乱 (路由错误) | P1 | 错误委托导致回复断裂 | 1h |

**总计**: 约 3 小时

---

## 测试计划

1. 修复后重新运行 7月19号的聊天场景
2. 验证空回复不再出现
3. 验证工具调用格式不再泄露
4. 验证角色扮演/情感类消息由 xiaoda 直接处理，不错误委托给子代理
5. 验证 DSML 格式正确解析
