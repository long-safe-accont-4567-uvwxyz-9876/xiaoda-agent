"""LLM 输出清洗工具 —— 移除推理模型的思维链，仅保留最终回复。

从 greeting_scheduler.py / nudge_engine.py 提取的公共模块，
修复了正则模式过于具体导致推理文本泄漏的问题。
"""
from __future__ import annotations

import re

from loguru import logger


# 推理模型（DeepSeek-R1/MiMo Pro 等）会输出 <think>...</think> 思维链
_THINK_TAG_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.DOTALL | re.IGNORECASE)

# 未闭合的 <think> 或 CoT 前缀段落 —— 遇到则跳过该段
_THINK_PREFIX_PATTERNS = [
    re.compile(r"^\s*<think\b[^>]*>.*", re.DOTALL | re.IGNORECASE),
    re.compile(r"^\s*(嗯[，,].*?(?:\n\s*\n|。\s*\n))", re.DOTALL),
    re.compile(r"^\s*(首先[，,].*?(?:\n\s*\n|。\s*\n))", re.DOTALL),
    re.compile(r"^\s*(作为[^。，]+[，,].*?(?:\n\s*\n|。\s*\n))", re.DOTALL),
    re.compile(r"^\s*(我的角色是.*?(?:\n\s*\n|。\s*\n))", re.DOTALL),
    re.compile(r"^\s*(关键点[：:].*?(?:\n\s*\n|$))", re.DOTALL),
    # 以下为新增：覆盖实际推理输出中出现的复述 prompt 的模式
    re.compile(r"^\s*(问候主题.*?(?:\n\s*\n|$))", re.DOTALL),
    re.compile(r"^\s*(关键指令.*?(?:\n\s*\n|$))", re.DOTALL),
    re.compile(r"^\s*(所以[，,].*?(?:\n\s*\n|$))", re.DOTALL),
    re.compile(r"^\s*(这意味着.*?(?:\n\s*\n|$))", re.DOTALL),
]

# 清洗后仍含推理痕迹的检测 —— 扩展覆盖实际出现的关键词
_REASONING_INDICATORS = re.compile(
    r"关键点[：:]|我的角色是|问候主题|关键指令|这意味着|"
    r"所以[，,](?:在|问候|我应该)|并且时间是|"
    r"直接输出最终回复|不要思考过程|我只能给出|"
    r"我必须|我来分析|让我想想|现在是我主动|"
    r"数一下字数|检查字数|字数[：:]|输出[：:]|输出内容[：:]"
)


def deduplicate_multi_reply(text: str, *, context: str = "") -> str:
    """检测并去重多回复：当 LLM 输出了多个候选回复（如多行问候）时只保留第一个。

    可独立于 strip_thinking 使用，用于主回复链路的去重。
    """
    if not text:
        return ""

    greeting_patterns = [
        r'早安', r'早上好', r'中午好', r'下午好', r'晚上好', r'晚安',
        r'好呀', r'好啊', r'在呀', r'在啊', r'在哒'
    ]

    lines = text.split('\n')
    if len(lines) > 1:
        greeting_lines = []
        for line in lines:
            line = line.strip()
            if line and any(pattern in line for pattern in greeting_patterns):
                greeting_lines.append(line)

        if len(greeting_lines) > 1:
            logger.info("llm_cleanup.multiple_greetings_detected",
                       context=context, total_lines=len(lines),
                       greeting_count=len(greeting_lines),
                       first_greeting=greeting_lines[0][:50])
            return greeting_lines[0]

    return text


def strip_thinking(text: str, *, context: str = "") -> str:
    """移除推理模型的思维链输出，仅保留最终回复。

    Args:
        text: LLM 原始输出
        context: 调用场景（如 "greeting" / "nudge"），用于日志
    """
    if not text:
        return ""
    raw = text

    # 1. 完整 <think>...</think> 标签
    text = _THINK_TAG_RE.sub("", text)
    # 2. 未闭合的 <think> 或 CoT 前缀段落
    for pat in _THINK_PREFIX_PATTERNS:
        m = pat.match(text)
        if m:
            text = text[m.end():]
            break
    text = text.strip()

    # 3. 清洗后仍含推理痕迹 → 尝试取最后一句短句，否则丢弃
    if _REASONING_INDICATORS.search(text):
        sentences = re.split(r'[。！？\n]', text)
        for s in reversed(sentences):
            s_c = s.strip()
            if s_c and len(s_c) <= 50 and not _REASONING_INDICATORS.search(s_c):
                return s_c
        # 整段都是推理文本，记录并丢弃
        logger.warning("llm_cleanup.all_reasoning_discarded",
                       context=context, raw_len=len(raw),
                       raw_preview=raw[:120])
        return ""

    # 4. 处理多个回复的情况（模型可能输出了多个回复，如"早安"、"中午好"、"晚上好"）
    return deduplicate_multi_reply(text, context=context)
