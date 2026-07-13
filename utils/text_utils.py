import random
import re
import base64
from pathlib import Path
import contextlib

AI_PATTERNS = [
    (r'此外[，,]?\s*', ''),
    (r'值得注意的是[，,]?\s*', ''),
    (r'需要强调的是[，,]?\s*', ''),
    (r'总的来说[，,]?\s*', ''),
    (r'综上所述[，,]?\s*', ''),
    (r'首先[，,]?\s*', ''),
    (r'其次[，,]?\s*', ''),
    (r'最后[，,]?\s*', ''),
    (r'总而言之[，,]?\s*', ''),
    (r'简而言之[，,]?\s*', ''),
    (r'不仅如此[，,]?\s*', ''),
    (r'更重要的是[，,]?\s*', ''),
    (r'不仅如此，.*更是', ''),
    (r'它不仅.*更是.*', ''),
    (r'—+', '——'),
    (r'\*\*([^*]+)\*\*', r'\1'),
    (r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1'),
    (r'^[\s]*[-*+•]\s+[*#]*', ''),
    (r'^[\s]{0,3}#{1,6}\s+', ''),
    (r'`([^`]+)`', r'\1'),
    (r'^>\s*', ''),
    (r'\s*\*$', ''),
    (r'^\s*\*', ''),
    (r'团队成员汇报[：:]?\s*', ''),
    (r'综合评估与建议[：:]?\s*', ''),
    (r'行动建议[：:]?\s*', ''),
    (r'任务目标[：:]?\s*', ''),
    (r'执行状态[：:]?\s*', ''),
    (r'信息缺口[：:]?\s*', ''),
    (r'后续支持[：:]?\s*', ''),
    (r'总体概述[：:]?\s*', ''),
    (r'备注[：:]?\s*', ''),
    (r'可能原因[：:]?\s*', ''),
    (r'结果缺失[：:]?\s*', ''),
]

AI_WORDS = [
    'crucial', 'pivotal', 'landscape', 'testament', 'underscore',
    'highlight', 'fostering', 'enhancing', 'vibrant', 'rich tapestry',
    'seamless', 'intuitive', 'comprehensive', 'robust',
]

# 豆包味 / AI 腔结尾套话 —— 直接删除整行或整句
# 这些是 Doubao 等模型最典型的"客服腔"，与小妲人格冲突
DOUBAO_PATTERNS = [
    # 1. 结尾客套话（整句删除）
    (r'[，,。\s]*希望[能对].*?[帮有]助.*?[。.\s]*$', ''),
    (r'[，,。\s]*希望这[些个].*?[能就].*?帮.*?[。.\s]*$', ''),
    (r'[，,。\s]*如有[需要任何].*?[请随].*?[告诉联系].*?[我你].*?[。.\s]*$', ''),
    (r'[，,。\s]*如有.*?疑问.*?[请随].*?[问联系].*?[。.\s]*$', ''),
    (r'[，,。\s]*祝你.*?[愉快开心顺利].*?[。.\s]*$', ''),
    (r'[，,。\s]*祝[你您].*?生活.*?[愉快美好].*?[。.\s]*$', ''),
    (r'[，,。\s]*如果.*?还.*?问题.*?[随时].*?[问联系].*?[。.\s]*$', ''),
    (r'[，,。\s]*欢迎.*?随时.*?[提问咨询].*?[。.\s]*$', ''),
    (r'[，,。\s]*如果.*?可以.*?帮.*?[到你]?[，,]?\s*[请随]?.*?[告诉].*?[。.\s]*$', ''),
    # 2. 空洞鼓励（整句删除）
    (r'[，,。\s]*相信[你一].*?[一定能定].*?[做好实现].*?[。.\s]*$', ''),
    (r'[，,。\s]*加油[！!。.\s]*$', ''),
    (r'[，,。\s]*期待[你你].*?[的表现成果].*?[。.\s]*$', ''),
    # 3. AI 自我意识（替换为更自然的表述，小妲不说这种话）
    (r'作为[一个]?AI[，,]?\s*', ''),
    (r'作为[一个]?人工智能[，,]?\s*', ''),
    (r'我是[一个]?AI[，,]?\s*', ''),
    (r'我是[一个]?人工智能[，,]?\s*', ''),
    (r'作为[语言]?模型[，,]?\s*', ''),
    (r'我作为[一个]?AI[，,]?\s*', ''),
    # 4. 过度礼貌的"请"
    (r'^请[您你]注意[，,]?\s*', ''),
    (r'^请[您你]放心[，,]?\s*', ''),
    # 5. "总结一下/总结来说" 开头的总结段（删整行）
    (r'^总结[一下来说][：:，,]?\s*[^\n]*$', ''),
    # 6. "以下是为您..." 的开场白（删整行）
    (r'^以下是为[您你][^。\n]*[：:]\s*$', ''),
    (r'^这是为[您你][^。\n]*[：:]\s*$', ''),
]


def humanize(text: str, style: str = "xiaoda") -> str:
    """清洗 AI 腔文本, 移除套话/客套/列表编号等机器味痕迹.

    Args:
        text: 原始文本
        style: 风格名 (保留参数, 当前未使用), 默认 xiaoda

    Returns:
        清洗后的自然文本
    """
    for pattern, replacement in AI_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.MULTILINE)

    # 豆包味 / AI 腔清理：在通用 AI_PATTERNS 之后运行，针对结尾套话和客套话
    for pattern, replacement in DOUBAO_PATTERNS:
        text = re.sub(pattern, replacement, text, flags=re.MULTILINE)

    for word in AI_WORDS:
        text = re.sub(r'\b' + word + r'\b', '', text, flags=re.IGNORECASE)

    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'^\s*\d+\.\s+', '· ', text, flags=re.MULTILINE)
    text = re.sub(r'---+\s*', '', text)

    return text.strip()


TRUNCATION_MARKERS = [
    "\n——人家还有好多话想说呢，下次继续聊～",
    "\n——先说到这里吧，剩下的下次告诉你～",
    "\n——嗯……话有点长，剩下的让人家慢慢说～",
    "\n——人家说到一半啦，等下次再继续吧～",
]

DSML_PATTERN = re.compile(
    r'<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>',
    re.DOTALL,
)
DSML_INVOKE_PATTERN = re.compile(
    r'<｜｜DSML｜｜invoke\s+name="(\w+)">.*?</｜｜DSML｜｜invoke>',
    re.DOTALL,
)
DSML_PARAM_PATTERN = re.compile(
    r'<｜｜DSML｜｜parameter\s+name="(\w+)"[^>]*>(.*?)</｜｜DSML｜｜parameter>',
    re.DOTALL,
)
DSML_LEFTOVER = re.compile(
    r'<｜｜DSML｜｜[^>]*>',
    re.DOTALL,
)
FAKE_XML_TOOL_PATTERN = re.compile(
    r'<function=\w+>.*?</function>|'
    r'<parameter=\w+>.*?</parameter>|'
    r'<(read_file|write_file|list_files|search_files|vision_analyze|camera_capture|'
    r'multi_search|web_browse|web_search|search_cn|shell_command|python_executor|document_reader|'
    r'save_memory|recall_memory|search_memory|nudge|get_hardware_info|'
    r'control_gpio|read_sensor|wolfram_query|analyze_code|run_code|edit_code|'
    r'create_file|arg|calculator|get_weather|get_current_time|'
    r'agnes_image|agnes_video|agnes_tts)\b[^>]*/?>.*?</\1>|'
    r'<(read_file|write_file|list_files|search_files|vision_analyze|camera_capture|'
    r'multi_search|web_browse|web_search|search_cn|shell_command|python_executor|document_reader|'
    r'save_memory|recall_memory|search_memory|nudge|get_hardware_info|'
    r'control_gpio|read_sensor|wolfram_query|analyze_code|run_code|edit_code|'
    r'create_file|arg|calculator|get_weather|get_current_time|'
    r'agnes_image|agnes_video|agnes_tts)\b[^>]*/>',
    re.DOTALL,
)

# 非标准工具调用格式：[TOOL_CALL] {tool => "...", args => {...}} [/TOOL_CALL]
# 使用非贪婪匹配避免误删过多内容；[\s\S] 匹配任意字符（含换行）
TOOL_CALL_PATTERN = re.compile(
    r'\[TOOL_CALL\][\s\S]*?\[/TOOL_CALL\]',
)
# 裸 <tool_call>...</tool_call> 标签（含错配闭合标签 </think> 的容错）
TOOL_CALL_BARE_TAG_PATTERN = re.compile(
    r'<tool_call\b[^>]*>[\s\S]*?(?:</tool_call>|</think>)',
    re.IGNORECASE,
)
# 独立未闭合的 <tool_call> 开头（容错）
TOOL_CALL_OPEN_ONLY_PATTERN = re.compile(
    r'<tool_call\b[^>]*>[\s\S]*',
    re.IGNORECASE,
)


def strip_dsml(text: str) -> str:
    """移除文本中的 DSML/工具调用/推理标签等机器泄露内容.

    Args:
        text: 原始文本

    Returns:
        清理后的纯文本
    """
    text = DSML_PATTERN.sub('', text)
    text = DSML_INVOKE_PATTERN.sub('', text)
    text = DSML_LEFTOVER.sub('', text)
    text = FAKE_XML_TOOL_PATTERN.sub('', text)
    text = TOOL_CALL_PATTERN.sub('', text)
    # 清理裸 <tool_call>...</tool_call>（含 </think> 错配）
    text = TOOL_CALL_BARE_TAG_PATTERN.sub('', text)
    # 清理未闭合的 <tool_call> 开头到末尾
    text = TOOL_CALL_OPEN_ONLY_PATTERN.sub('', text)
    # 清理孤立的 </think> 闭合标签（tool_call错配后残留）
    text = re.sub(r'</think>', '', text, flags=re.IGNORECASE)
    # 清理其他常见的工具调用泄露格式
    # 1. 代码块中的 function_call JSON
    text = re.sub(r'```(?:json)?\s*\{[^}]*?function_call[^}]*?\}\s*```', '', text, flags=re.DOTALL)
    # 2. 纯 JSON 格式的工具调用
    text = re.sub(r'\{\s*"function_call"\s*:\s*\{[^}]*?\}\s*\}', '', text)
    # 3. 残留的 <think>...</think> 标签
    text = re.sub(r'<think>[\s\S]*?</think>', '', text)
    # 4. 残留的 tool_call 代码块
    text = re.sub(r'```tool_call[\s\S]*?```', '', text)
    # 5. 空的代码块
    text = re.sub(r'```\s*```', '', text)
    # 6. 裸 JSON 工具参数（不在代码块内的独立 JSON）
    # 6a. JSON 数组格式：[{"city": "...", ...}]
    text = re.sub(r'(?<!```)\n?\s*\[\s*\{[\s\S]*?\}\s*\]\s*(?!```)', '', text)
    # 6b. JSON 对象格式（含工具参数特征字段）：{"city": "...", ...}
    text = re.sub(r'(?<!```)\n?\s*\{\s*"(?:city|query_type|search_query|query|url|keyword|prompt|text|input)"\s*:[\s\S]*?\}\s*(?!```)', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ── 推理/思考内容剥离 ──────────────────────────────────────────
# 匹配各种推理标签格式（尖括号 <think>...</think> 和方括号 [thinking]...[/thinking]）
_REASONING_TAG_PATTERN = re.compile(
    r'[<\[](?:think|thinking|reasoning|analysis|reflection|thought|scratchpad)[\s\S]*?'
    r'</(?:think|thinking|reasoning|analysis|reflection|thought|scratchpad)>'
    r'|'
    r'\[(?:think|thinking|reasoning|analysis|reflection|thought|scratchpad)[\s\S]*?'
    r'\[/(?:think|thinking|reasoning|analysis|reflection|thought|scratchpad)\]',
    re.IGNORECASE,
)
# 自闭合推理标签（无闭合标签的情况）
_REASONING_OPEN_PATTERN = re.compile(
    r'<(?:think|thinking|reasoning|analysis|reflection|thought|scratchpad)\s*/?>'
    r'|\[(?:think|thinking|reasoning|analysis|reflection|thought|scratchpad)\s*/?\]',
    re.IGNORECASE,
)
# Agnes 模型推理标签：[emotion thinking]`` 或 [emotion xxx] 格式
_EMOTION_REASONING_PATTERN = re.compile(
    r'\[emotion\s+[a-z]+\s*\]\s*``?[^\n]*',
    re.IGNORECASE,
)
# 第三人称引用：They ask / The user asks / User is asking
_THIRD_PERSON_PATTERN = re.compile(
    r'(?:They|The\s+user|User)\s+(?:ask|is\s+asking|asked)\s*["\'][^"\']+["\']',
    re.IGNORECASE,
)
# 内部决策：We need respond / We should / We must
_INTERNAL_DECISION_PATTERN = re.compile(
    r'We\s+(?:need\s+to|should|must|will)\s+(?:respond|answer|reply|deliver)',
    re.IGNORECASE,
)
# 裸文本推理特征：以 "Need " / "Let me " / "I should " / "I need " 开头的英文推理行
# 这些是模型将内部推理当作正文输出的典型特征
_REASONING_PHRASES = [
    r"Need\s+(?:think|no\s+tool|to\s+answer|to\s+recall|to\s+check|to\s+consider|to\s+mention|to\s+include|to\s+decide)",
    r"Let\s+me\s+(?:think|recall|check|consider|analyze|review|craft|construct|formulate|ensure|make\s+sure)",
    r"I\s+(?:should|need to|must|have to|will)\s+(?:think|recall|check|consider|analyze|review|craft|construct|formulate|ensure|include|mention|decide|answer|respond|be\s+(?:honest|careful|clear|safe|respectful|mindful))",
    r"(?:Must|Should)\s+(?:exactly|also|not|be|include|end|avoid|use|ensure)",
    r"(?:First|Next|Then|Now|Also|Finally),\s+(?:I|let me|need to)",
    r"Looking\s+(?:at|back|into|for)",
    r"I['\u2019]ve\s+already\s+(?:clearly\s+)?(?:stated|said|told|mentioned|established|set)",
    r"The\s+request\s+has\s+escalated",
    r"I\s+can\s+(?:not|and\s+cannot)\s+go",
    r"This\s+feels\s+(?:safe|unsafe|right|wrong|appropriate|beyond)",
    r"I\s+need\s+to\s+be\s+(?:honest|careful|clear|safe|respectful|mindful)",
    r"I\s+must\s+be\s+(?:honest|careful|clear|safe|respectful|mindful)",
]
_REASONING_LINE_PATTERN = re.compile(
    r'^(?:' + '|'.join(_REASONING_PHRASES) + r')[^\n]*$',
    re.MULTILINE | re.IGNORECASE,
)
# 多行连续推理块（3行以上以 "Need "/"Let me "/"I should " 开头的英文行）
_REASONING_BLOCK_PATTERN = re.compile(
    r'(?:^[' + ''.join(re.escape(c) for c in 'Need Let I Mus Sho Fir Nex The Now Als Fin') + r'].*\n){3,}',
    re.MULTILINE,
)
# Agnes 风格连续英文推理段：包含多个关键词的整段英文（无换行）
# 特征：包含 They ask / We need / Must include / Need adhere 等组合
_AGNES_REASONING_BLOCK = re.compile(
    r'[^\n]*?(?:They\s+ask|We\s+need|Must\s+include|Need\s+adhere|previous\s+assistant)[^\n]*'
    r'(?:[^\n]*?(?:Need|Must|Should|We\s+can|final\s+answer)[^\n]*){2,}',
    re.IGNORECASE,
)
# 扩展英文推理段：包含 I need / I must / I should / I've already / Looking at
# 等第一人称元推理关键词的连续英文段落（含中文人名插花）
_EXTENDED_REASONING_BLOCK = re.compile(
    r'(?:[A-Z][a-z]*(?:\s+[\w\u4e00-\u9fff]+)*[.?!]\s*){2,}'  # 2+ 英文句子
    r'(?:[^\n]*?(?:I\s+(?:need|must|should|have\s+to|can(?:not)?)\s+|'
    r"I['\u2019]ve\s+already|Looking\s+at|This\s+feels|The\s+request|"
    r'my\s+boundary|be\s+honest|safe\s+for\s+me|as\s+a\s+character)[^\n]*)+',
    re.IGNORECASE,
)
# 中文内部独白/推理特征短语（模型将思维链当作正文输出）
# 这些短语是模型在"思考如何回复"而非"实际回复"，应被清理
# 注意：仅保留明确是"内部独白"的模式，移除过宽的"我来分析"等
# 正常回复中也会使用的短语，防止过度截断
_CHINESE_REASONING_PHRASES = [
    r"现在开始(?:回复|组织回复|返回|生成)",
    r"根据SOUL\.md",
    r"根据记忆碎片",
    r"我需要用.*?语气",
    r"我需要确保",
    r"我应该.*?(?:回应|回答|回复|告诉|给出)",
    r"我可以温柔(?:提醒|提示)",
    r"(?:沙|汐)问.*?(?:几点|时间|现在)",
    r"(?:沙|汐)在.*?时间点",
    r"现在时间是",
    r"让我(?:想想|回忆|思考)",
    r"我直接(?:回答|告诉)",
]
_CHINESE_REASONING_LINE_PATTERN = re.compile(
    r'^(?:' + '|'.join(_CHINESE_REASONING_PHRASES) + r')[^\n]*$',
    re.MULTILINE,
)


def strip_reasoning(text: str) -> str:
    """剥离模型输出中的推理/思考内容。

    处理以下情况：
    1.  ....Predicate 等标签包裹的推理
    2. 裸文本推理行（Need think about... / Let me recall... 等）
    3. 连续多行英文推理块
    4. Agnes 模型风格的推理标签（[emotion thinking]``）
    5. 第三人称引用（They ask "..."）
    6. Agnes 风格连续英文推理段
    7. 中文内部独白/推理行
    """
    if not text:
        return text
    original_len = len(text)
    # 1. 标签包裹的推理
    text = _REASONING_TAG_PATTERN.sub('', text)
    text = _REASONING_OPEN_PATTERN.sub('', text)
    # 2. Agnes 模型推理标签
    text = _EMOTION_REASONING_PATTERN.sub('', text)
    # 3. 第三人称引用
    text = _THIRD_PERSON_PATTERN.sub('', text)
    # 4. 内部决策
    text = _INTERNAL_DECISION_PATTERN.sub('', text)
    # 5. 裸文本推理行
    text = _REASONING_LINE_PATTERN.sub('', text)
    # 6. 连续多行英文推理块（3行以上）
    text = _REASONING_BLOCK_PATTERN.sub('', text)
    # 7. Agnes 风格连续英文推理段
    text = _AGNES_REASONING_BLOCK.sub('', text)
    # 8. 扩展英文推理段（I need to be honest / Looking at / I've already 等模式）
    text = _EXTENDED_REASONING_BLOCK.sub('', text)
    # 9. 中文内部独白/推理行
    text = _CHINESE_REASONING_LINE_PATTERN.sub('', text)
    # 清理多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    # 过度截断保护：如果清理后内容不到原始内容的 30%，说明可能误删了正常回复
    # 此时记录警告，保留清理后内容（因为推理内容确实不该出现）
    # 但额外记录日志便于诊断
    cleaned_len = len(text)
    if original_len > 100 and cleaned_len < original_len * 0.3:
        from loguru import logger
        logger.warning(
            "text_utils.strip_reasoning_overstrip original_len={} cleaned_len={} ratio={:.1%}",
            original_len, cleaned_len, cleaned_len / original_len if original_len else 0,
        )
    return text


# 裸 <tool_call>...</tool_call> XML 块（含 </think> 错配容错）
TOOL_CALL_XML_PATTERN = re.compile(
    r'<tool_call\b[^>]*>([\s\S]*?)(?:</tool_call>|</think>)',
    re.IGNORECASE,
)


def has_dsml_tool_calls(text: str) -> bool:
    """判断文本中是否包含 DSML/工具调用标签."""
    return (bool(DSML_INVOKE_PATTERN.search(text))
            or bool(TOOL_CALL_PATTERN.search(text))
            or bool(TOOL_CALL_XML_PATTERN.search(text)))


def parse_dsml_tool_calls(text: str, allowed_tools: set | None = None) -> list[dict]:
    """从文本中解析 DSML 工具调用块为结构化列表.

    Args:
        text: 原始文本
        allowed_tools: 允许的工具名集合, None 表示全部允许

    Returns:
        工具调用字典列表 (含 name/arguments)
    """
    import json
    results = []

    # ── 1. DSML <｜｜DSML｜｜invoke name="xxx">...</｜｜DSML｜｜invoke> ──
    invoke_blocks = list(DSML_INVOKE_PATTERN.finditer(text))
    for _i, invoke_match in enumerate(invoke_blocks):
        tool_name = invoke_match.group(1)
        if allowed_tools and tool_name not in allowed_tools:
            continue

        start = invoke_match.start()
        end = invoke_match.end()
        block = text[start:end]

        args = {}
        for param_match in DSML_PARAM_PATTERN.finditer(block):
            param_name = param_match.group(1)
            param_value = param_match.group(2).strip()
            with contextlib.suppress(json.JSONDecodeError, ValueError):
                param_value = json.loads(param_value)
            args[param_name] = param_value

        results.append({
            "id": f"dsml_{len(results)}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(args, ensure_ascii=False),
            }
        })

    # ── 2. 裸 <tool_call>JSON</tool_call> 格式（含 </think> 错配容错）──
    for xml_match in TOOL_CALL_XML_PATTERN.finditer(text):
        inner = xml_match.group(1).strip()
        # 去掉可能的 ```json ... ``` 包裹
        inner = re.sub(r'^```(?:json)?\s*|\s*```$', '', inner, flags=re.DOTALL).strip()
        try:
            parsed = json.loads(inner)
        except json.JSONDecodeError:
            # 可能是多个工具调用的数组
            try:
                # 容错：非标准 JSON 修复后再试
                fixed = re.sub(r',\s*([}\]])', r'\1', inner)
                parsed = json.loads(fixed)
            except json.JSONDecodeError:
                continue

        calls = parsed if isinstance(parsed, list) else [parsed]
        for call in calls:
            if not isinstance(call, dict):
                continue
            # 兼容 {"name": "xxx", "arguments": {...}} 和 {"tool_name": "xxx", "parameters": {...}}
            tool_name = call.get("name") or call.get("tool_name") or call.get("function", {}).get("name")
            if not tool_name:
                continue
            if allowed_tools and tool_name not in allowed_tools:
                continue
            args = call.get("arguments") or call.get("parameters") or call.get("function", {}).get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            results.append({
                "id": f"xml_{len(results)}",
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
                }
            })

    return results

BREAK_PATTERNS = [
    '\n\n',
    '。\n', '！\n', '？\n',
    '。', '！', '？',
    '；', ',', ';',
    '，',
    '\n',
    ' ',
]

# F7: 分层截断 —— 按角色设置不同截断上限
SUMMARY_LIMITS = {
    "user": 200,       # 用户消息保留更多（通常较短且重要）
    "assistant": 150,  # 小妲回复保留关键决策
    "tool": 100,       # 工具结果保留关键数据
    "default": 120,
}

# 分层截断的句子边界优先级（从高到低）
_SENTENCE_BREAKS = ['\n\n', '。', '！', '？', '；', '\n', '，', ',', ' ']


def smart_summary_truncate(content: str, role: str = "default") -> str:
    """F7 分层截断 —— 按角色设置不同上限，优先在句子边界切分。

    替代原有的 content[:80] 粗暴截断：
    - user: 200 字符
    - assistant: 150 字符
    - tool: 100 字符
    - 在句子边界（。！？；\n）处切分，避免截断半句话
    - 截断时添加 […] 标记
    """
    if not content:
        return ""
    limit = SUMMARY_LIMITS.get(role, SUMMARY_LIMITS["default"])
    if len(content) <= limit:
        return content

    # 在 limit 附近寻找最佳句子边界
    search_start = max(0, limit - 30)
    search_end = min(len(content), limit + 10)
    best_pos = -1
    for pattern in _SENTENCE_BREAKS:
        pos = content.rfind(pattern, search_start, search_end)
        if pos != -1:
            best_pos = pos + len(pattern)
            break

    if best_pos == -1 or best_pos < search_start:
        best_pos = limit

    return content[:best_pos].rstrip() + "[…]"

QQ_MSG_BYTE_LIMIT = 8000
# 群聊单条消息字节上限（保守值，避免服务端截断）。
# QQ 官方群消息 content 字段实际限制约 2000 字符（≈6000 字节 UTF-8），
# 取 4000 字节作为安全阈值，留出表情包/媒体混排时的余量。
QQ_GROUP_MSG_BYTE_LIMIT = 4000


def _find_char_boundary(text: str, byte_limit: int) -> int:
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(text[:mid].encode('utf-8')) <= byte_limit:
            low = mid
        else:
            high = mid - 1
    return low


def smart_truncate(text: str, max_len: int = QQ_MSG_BYTE_LIMIT) -> str:
    """按字节上限智能截断文本, 优先在句末/换行处切分.

    Args:
        text: 原始文本
        max_len: 字节上限, 默认 QQ_MSG_BYTE_LIMIT

    Returns:
        截断后的文本
    """
    encoded = text.encode('utf-8')
    if len(encoded) <= max_len:
        return text

    safe_limit = int(max_len * 0.9)
    target_chars = _find_char_boundary(text, safe_limit)

    search_start = max(0, target_chars - 200)
    search_end = min(len(text), target_chars + 100)

    best_pos = -1
    for pattern in BREAK_PATTERNS:
        pos = text.rfind(pattern, search_start, search_end)
        if pos != -1:
            best_pos = pos + len(pattern)
            break

    if best_pos == -1 or best_pos < search_start:
        best_pos = target_chars

    truncated = text[:best_pos].rstrip()

    if len(truncated.encode('utf-8')) > max_len:
        truncated = truncated[:_find_char_boundary(truncated, safe_limit)].rstrip()

    if truncated.count('```') % 2 != 0:
        truncated += '\n```'

    marker = random.choice(TRUNCATION_MARKERS)
    truncated += marker

    return truncated


_SEGMENT_CONTINUATIONS = [
    "（继续～）",
    "（接着说～）",
    "（还有呢～）",
    "（还没完哦～）",
    "（继续往下～）",
]


def split_long_reply(text: str, max_len: int = QQ_MSG_BYTE_LIMIT) -> list[str]:
    """将超长文本按字节上限拆分为多段, 每段附加续接提示.

    Args:
        text: 原始文本
        max_len: 字节上限, 默认 QQ_MSG_BYTE_LIMIT

    Returns:
        拆分后的文本段列表
    """
    encoded = text.encode('utf-8')
    if len(encoded) <= max_len:
        return [text]

    safe_limit = int(max_len * 0.9)
    target_chars = _find_char_boundary(text, safe_limit)
    segments = []
    remaining = text

    while remaining:
        encoded = remaining.encode('utf-8')
        if len(encoded) <= max_len:
            segments.append(remaining)
            break

        search_start = max(0, target_chars - 200)
        search_end = min(len(remaining), target_chars + 100)

        best_pos = -1
        for pattern in BREAK_PATTERNS:
            pos = remaining.rfind(pattern, search_start, search_end)
            if pos != -1:
                best_pos = pos + len(pattern)
                break

        if best_pos == -1 or best_pos < search_start:
            best_pos = target_chars

        chunk = remaining[:best_pos].rstrip()

        if len(chunk.encode('utf-8')) > max_len:
            chunk = chunk[:_find_char_boundary(chunk, safe_limit)].rstrip()

        if chunk.count('```') % 2 != 0:
            chunk += '\n```'

        segments.append(chunk)
        remaining = remaining[best_pos:].lstrip('\n')

    # 为中间段添加轻量衔接词（保持小妲语气）
    if len(segments) > 1:
        for i in range(len(segments) - 1):
            hint = random.choice(_SEGMENT_CONTINUATIONS)
            segments[i] = segments[i].rstrip() + "\n" + hint

    return segments


def split_for_group_passive(text: str, byte_limit: int = QQ_GROUP_MSG_BYTE_LIMIT,
                            max_segments: int = 4) -> list[str]:
    """群聊被动回复分片：按字节上限切片，最多 max_segments 片，不加衔接词。

    QQ 群聊被动回复（带 msg_id）每条用户消息 5 分钟内最多 5 次。
    策略：ACK 占 1 次，流式分片最多 4 次，总共 5 次，不超限。
    分片之间不加任何衔接词，避免对话突兀。
    最后一片超长时按字节截断，不加任何标记，自动闭合代码块。

    Args:
        text: 原始文本
        byte_limit: 字节上限, 默认 QQ_GROUP_MSG_BYTE_LIMIT
        max_segments: 最大分片数, 默认 4（ACK + 4 片 = 5 次配额）

    Returns:
        1~max_segments 片文本列表
    """
    encoded = text.encode('utf-8')
    if len(encoded) <= byte_limit:
        return [text]

    segments: list[str] = []
    remaining = text

    while remaining and len(segments) < max_segments:
        # 最后一片配额：直接按字节截断，不加标记
        if len(segments) == max_segments - 1:
            if len(remaining.encode('utf-8')) > byte_limit:
                encoded_rem = remaining.encode('utf-8')
                remaining = encoded_rem[:byte_limit].decode('utf-8', errors='ignore')
                # 闭合截断后未结束的代码块
                if remaining.count('```') % 2 != 0:
                    remaining += '\n```'
            segments.append(remaining.rstrip())
            break

        # 在字节上限附近找句子边界切分
        safe_limit = int(byte_limit * 0.9)
        target_chars = _find_char_boundary(remaining, safe_limit)
        search_start = max(0, target_chars - 200)
        search_end = min(len(remaining), target_chars + 100)
        best_pos = -1
        for pattern in BREAK_PATTERNS:
            pos = remaining.rfind(pattern, search_start, search_end)
            if pos != -1:
                best_pos = pos + len(pattern)
                break
        if best_pos == -1 or best_pos < search_start:
            best_pos = target_chars

        part = remaining[:best_pos].rstrip()
        remaining = remaining[best_pos:].lstrip('\n')

        # 闭合当前片的代码块
        if part.count('```') % 2 != 0:
            part += '\n```'
            # 下一片以代码块开头补全
            remaining = '```\n' + remaining

        segments.append(part)

        # 剩余内容未超字节上限，作为最后一片
        if len(remaining.encode('utf-8')) <= byte_limit:
            if remaining.strip():
                segments.append(remaining.rstrip())
            break

    return segments


_IMAGE_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB


def encode_image_to_base64(image_path: str) -> tuple[str, str]:
    """将图片文件编码为 base64 字符串，并返回对应的 MIME 类型。

    Args:
        image_path: 图片文件的路径

    Returns:
        tuple[str, str]: (mime_type, base64_string)

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 文件超过大小限制
    """
    p = Path(image_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"图片文件不存在: {image_path}")
    if p.stat().st_size > _MAX_IMAGE_SIZE:
        raise ValueError(f"图片文件超过 {_MAX_IMAGE_SIZE // (1024*1024)} MB 限制: {image_path}")
    mime = _IMAGE_MIME_MAP.get(p.suffix.lower(), "image/jpeg")
    img_b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return mime, img_b64
