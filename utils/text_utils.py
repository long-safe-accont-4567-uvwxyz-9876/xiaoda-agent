import random
import re
import base64
from pathlib import Path

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


def humanize(text: str, style: str = "nahida") -> str:
    for pattern, replacement in AI_PATTERNS:
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


def strip_dsml(text: str) -> str:
    text = DSML_PATTERN.sub('', text)
    text = DSML_INVOKE_PATTERN.sub('', text)
    text = DSML_LEFTOVER.sub('', text)
    text = FAKE_XML_TOOL_PATTERN.sub('', text)
    text = TOOL_CALL_PATTERN.sub('', text)
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
# 匹配各种推理标签格式
_REASONING_TAG_PATTERN = re.compile(
    r'<(?:think|thinking|reasoning|analysis|reflection|thought|scratchpad)[\s\S]*?'
    r'</(?:think|thinking|reasoning|analysis|reflection|thought|scratchpad)>',
    re.IGNORECASE,
)
# 自闭合推理标签（无闭合标签的情况）
_REASONING_OPEN_PATTERN = re.compile(
    r'<(?:think|thinking|reasoning|analysis|reflection|thought|scratchpad)\s*/?>',
    re.IGNORECASE,
)
# 裸文本推理特征：以 "Need " / "Let me " / "I should " / "I need " 开头的英文推理行
# 这些是模型将内部推理当作正文输出的典型特征
_REASONING_PHRASES = [
    r"Need\s+(?:think|no\s+tool|to\s+answer|to\s+recall|to\s+check|to\s+consider|to\s+mention|to\s+include|to\s+decide)",
    r"Let\s+me\s+(?:think|recall|check|consider|analyze|review|craft|construct|formulate|ensure|make\s+sure)",
    r"I\s+(?:should|need to|must|have to|will)\s+(?:think|recall|check|consider|analyze|review|craft|construct|formulate|ensure|include|mention|decide|answer|respond)",
    r"(?:Must|Should)\s+(?:exactly|also|not|be|include|end|avoid|use|ensure)",
    r"(?:First|Next|Then|Now|Also|Finally),\s+(?:I|let me|need to)",
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


def strip_reasoning(text: str) -> str:
    """剥离模型输出中的推理/思考内容。

    处理以下情况：
    1. <think>...</think> 等标签包裹的推理
    2. 裸文本推理行（Need think about... / Let me recall... 等）
    3. 连续多行英文推理块
    """
    if not text:
        return text
    # 1. 标签包裹的推理
    text = _REASONING_TAG_PATTERN.sub('', text)
    text = _REASONING_OPEN_PATTERN.sub('', text)
    # 2. 裸文本推理行
    text = _REASONING_LINE_PATTERN.sub('', text)
    # 3. 连续多行英文推理块（3行以上）
    text = _REASONING_BLOCK_PATTERN.sub('', text)
    # 清理多余空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def has_dsml_tool_calls(text: str) -> bool:
    return bool(DSML_INVOKE_PATTERN.search(text)) or bool(TOOL_CALL_PATTERN.search(text))


def parse_dsml_tool_calls(text: str, allowed_tools: set | None = None) -> list[dict]:
    import json
    results = []
    invoke_blocks = list(DSML_INVOKE_PATTERN.finditer(text))
    for i, invoke_match in enumerate(invoke_blocks):
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
            try:
                param_value = json.loads(param_value)
            except (json.JSONDecodeError, ValueError):
                pass
            args[param_name] = param_value

        results.append({
            "id": f"dsml_{len(results)}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(args, ensure_ascii=False),
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

QQ_MSG_BYTE_LIMIT = 8000


def _find_char_boundary(text: str, byte_limit: int) -> int:
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if len(text[:mid].encode('utf-8')) <= byte_limit:
            low = mid
        else:
            high = mid - 1
    return low


def smart_truncate(text: str, max_len: int = 2000) -> str:
    encoded = text.encode('utf-8')
    if len(encoded) <= QQ_MSG_BYTE_LIMIT:
        return text

    safe_limit = int(QQ_MSG_BYTE_LIMIT * 0.9)
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

    if len(truncated.encode('utf-8')) > QQ_MSG_BYTE_LIMIT:
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


def split_long_reply(text: str, max_len: int = 2000) -> list[str]:
    encoded = text.encode('utf-8')
    if len(encoded) <= QQ_MSG_BYTE_LIMIT:
        return [text]

    safe_limit = int(QQ_MSG_BYTE_LIMIT * 0.9)
    target_chars = _find_char_boundary(text, safe_limit)
    segments = []
    remaining = text

    while remaining:
        encoded = remaining.encode('utf-8')
        if len(encoded) <= QQ_MSG_BYTE_LIMIT:
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

        if len(chunk.encode('utf-8')) > QQ_MSG_BYTE_LIMIT:
            chunk = chunk[:_find_char_boundary(chunk, safe_limit)].rstrip()

        if chunk.count('```') % 2 != 0:
            chunk += '\n```'

        segments.append(chunk)
        remaining = remaining[best_pos:].lstrip('\n')

    # 为中间段添加轻量衔接词（保持纳西妲语气）
    if len(segments) > 1:
        for i in range(len(segments) - 1):
            hint = random.choice(_SEGMENT_CONTINUATIONS)
            segments[i] = segments[i].rstrip() + "\n" + hint

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
