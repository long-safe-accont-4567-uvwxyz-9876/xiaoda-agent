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
    r'multi_search|web_browse|shell_command|python_executor|document_reader|'
    r'save_memory|recall_memory|search_memory|nudge|get_hardware_info|'
    r'control_gpio|read_sensor|wolfram_query|analyze_code|run_code|edit_code|'
    r'create_file|arg)\b[^>]*/?>.*?</\1>|'
    r'<(read_file|write_file|list_files|search_files|vision_analyze|camera_capture|'
    r'multi_search|web_browse|shell_command|python_executor|document_reader|'
    r'save_memory|recall_memory|search_memory|nudge|get_hardware_info|'
    r'control_gpio|read_sensor|wolfram_query|analyze_code|run_code|edit_code|'
    r'create_file|arg)\b[^>]*/>',
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
