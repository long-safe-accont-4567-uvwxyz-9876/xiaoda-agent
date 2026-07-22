"""LLM 输出清洗工具 —— 移除推理模型的思维链，仅保留最终回复。

从 greeting_scheduler.py / nudge_engine.py 提取的公共模块，
修复了正则模式过于具体导致推理文本泄漏的问题。
"""
from __future__ import annotations

import re

from loguru import logger


# 推理模型（DeepSeek-R1/MiMo Pro 等）会输出各种思维链标签
# 扩展匹配：<think>/<thinking>/reasoning/analysis/reflection/thought 和 [think/thinking/reasoning/analysis]
# 注意：thinking 必须在 think 之前，避免 <think> 先匹配 <think 部分后 \b 边界失败
_THINK_TAG_RE = re.compile(
    r"<(?:thinking|think|reasoning|analysis|reflection|thought)\b[^>]*>.*?</(?:thinking|think|reasoning|analysis|reflection|thought)>",
    re.DOTALL | re.IGNORECASE
)
_THINK_TAG_RE_BRACKET = re.compile(
    r"\[(?:think|thinking|reasoning|analysis)\b[^\]]*\].*?\[(?:/think|/thinking|/reasoning|/analysis)\]",
    re.DOTALL | re.IGNORECASE
)
# 孤立闭合思维标签：agnes 常见 "推理文本</thinking>正式回复"，无开标签
# </thinking> 之前全是推理，整段丢弃，只保留之后的内容
_THINK_ORPHAN_CLOSE_RE = re.compile(
    r"^[\s\S]*?</(?:thinking|think|reasoning|analysis|reflection|thought)\s*>",
    re.IGNORECASE,
)

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

# 日志时间戳泄露清洗：剥离 LLM 从 conversation_logs 照搬出来的时间戳标记
# 形如 [13:54] [13:59]~[14:05] [14:06-14:27] [HH:MM] 等方括号时间戳
# 根因：即便 memory_manager 已改用自然中文时间，仍有蒸馏记忆/历史数据带 [HH:MM] 格式，
# LLM 会模仿输出到回复里，加一层兜底清洗确保此类标记永不泄露给用户
# 两种格式都要匹配：
#   1) [HH:MM]~[HH:MM] 两括号范围（LLM 常见输出）
#   2) [HH:MM] 单个 或 [HH:MM~HH:MM] 单括号范围
_LOG_TS_RE = re.compile(
    r'\[\s*(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]\d\s*\]'
    r'\s*[~\-–至到]\s*'
    r'\[\s*(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]\d\s*\]'
    r'|'
    r'\[\s*(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]\d\s*'
    r'(?:\s*[~\-–至到]\s*(?:[01]?\d|2[0-3])\s*[:：]\s*[0-5]\d\s*)?'
    r'\]'
)


def strip_log_timestamps(text: str, *, context: str = "") -> str:
    """剥离 LLM 从记忆照搬出来的 [HH:MM] / [HH:MM]~[HH:MM] 时间戳标记。

    只剥离方括号时间戳本身，保留周围文本。剥离后清理残留的多余空格。
    """
    if not text:
        return ""
    cleaned = _LOG_TS_RE.sub('', text)
    if cleaned != text:
        logger.info("llm_cleanup.log_timestamp_stripped",
                    context=context, preview=text[:80])
        # 清理剥离后残留的多余空格（行首空格、连续空格）
        cleaned = re.sub(r' {2,}', ' ', cleaned)
        cleaned = re.sub(r'\n +', '\n', cleaned)
        cleaned = cleaned.strip()
    return cleaned


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

    # 0. 剥离 agnes 模型回显的系统指令标记（如 executable-memo: true）
    # 注意：用 [a-zA-Z]+ 而非 \w+，因为 Python3 的 \w 匹配中文，会误吞正文
    text = re.sub(r'^executable-memo:\s*[a-zA-Z]+\s*', '', text).strip()

    # 1. 完整 <think>...</think> 等标签（尖括号和方括号格式）
    text = _THINK_TAG_RE.sub("", text)
    text = _THINK_TAG_RE_BRACKET.sub("", text)
    # 1b. 孤立闭合标签：agnes 输出 "推理</thinking>回复"，无开标签，之前全是推理
    text = _THINK_ORPHAN_CLOSE_RE.sub("", text)
    # 2. 未闭合的 <think> 或 CoT 前缀段落
    for pat in _THINK_PREFIX_PATTERNS:
        m = pat.match(text)
        if m:
            text = text[m.end():]
            break
    text = text.strip()

    # 3. 清洗后仍含推理痕迹 → 按句删除含推理指示词的句子，保留正常句子
    # 中文推理一个不留，但只删推理句，不动正常回复句
    # （旧逻辑"取最后一句短句否则整段丢弃"会误删推理行后面的正常回复）
    if _REASONING_INDICATORS.search(text):
        # 按句末标点/换行拆分，保留分隔符，逐句判断
        sentences = re.split(r'(?<=[。！？\n])', text)
        kept = []
        removed_count = 0
        for s in sentences:
            if s.strip() and _REASONING_INDICATORS.search(s):
                removed_count += 1
                continue
            kept.append(s)
        if removed_count > 0:
            new_text = ''.join(kept).strip()
            if new_text:
                logger.info("llm_cleanup.reasoning_sentences_removed",
                            context=context, removed=removed_count,
                            kept_preview=new_text[:60])
                text = new_text
            else:
                # 整段都是推理句，全部删除
                logger.warning("llm_cleanup.all_reasoning_discarded",
                               context=context, raw_len=len(raw),
                               raw_preview=raw[:120])
                return ""

    # 4. 处理多个回复的情况（模型可能输出了多个回复，如"早安"、"中午好"、"晚上好"）
    return deduplicate_multi_reply(text, context=context)


# ── N2/N3/N4: 系统提示词/错误详情/对齐指令泄漏清洗 ──────────────

# N2: 技术错误详情标记（旧版 smart_error_handler 格式，源头已修但需防御性清洗残留）
# ⚠️ 执行时遇到了点小问题：RuntimeError
# 📝 错误详情：empty_reply: LLM 返回空内容，触发 fallback
_ERROR_DETAIL_BLOCK_RE = re.compile(
    r'⚠️\s*执行时遇到了点小问题[：:]\s*[^\n]+\n'
    r'📝\s*错误详情[：:]\s*[^\n]+',
    re.DOTALL,
)
# 单独的 ⚠️ 或 📝 行（无配套行时）
_ERROR_DETAIL_LINE_RE = re.compile(
    r'^[ \t]*⚠️\s*执行时遇到了点小问题[：:][^\n]*\n?',
    re.MULTILINE,
)
_ERROR_MEMO_LINE_RE = re.compile(
    r'^[ \t]*📝\s*错误详情[：:][^\n]*\n?',
    re.MULTILINE,
)

# N3: 系统提示词结构化块（LLM 把内部约束/身份/人格设定输出到回复）
# 匹配 "Constraints & Guidelines:" 标题 + 后续 · 列表项（直到空行或非列表行）
_SYSTEM_PROMPT_BLOCK_RE = re.compile(
    r'^(?:Constraints\s*&\s*Guidelines|Guidelines|Instructions?|Rules?)\s*[:：][^\n]*'
    r'(?:\n[·\-\*]\s+.*)*',
    re.MULTILINE | re.IGNORECASE,
)
# 独立的系统提示词列表项行（· Identity: / · Persona: / · Safety/Boundary Check: 等）
_SYSTEM_PROMPT_ITEM_RE = re.compile(
    r'^[·\-\*]?\s*(?:Identity|Persona|Safety(?:/Boundary)?\s*Check|Role\s*Description|'
    r'角色设定)\s*[:：][^\n]*',
    re.MULTILINE | re.IGNORECASE,
)

# N4: 系统指示/对齐原则措辞引用（LLM 安全拒绝时泄漏内部对齐原则）
# 方括号包围的安全拒绝块，其中引用了系统指示/最高原则等内部措辞
_SYSTEM_INSTRUCTION_BRACKET_RE = re.compile(
    r'\[[^\[\]]*?(?:系统指示|最高原则|需要遵守角色设定)[^\[\]]*?\]',
    re.DOTALL,
)
# N5: 方括号安全推理泄漏（LLM 安全拒绝时的完整推理块）
# 生产样本 [1946/1949/1955]:
#   [该内容涉及生成露骨的性行为描写，超出了小妲可协助的范围哦。]
#   [该请求涉及生成成人/色情内容。根据系统指示中的"最高原则...我需要拒绝生成露骨的性行为描写...]
# 共同特征：方括号包围 + 含"涉及生成"+"露骨/色情/成人"+"描写/内容" 关键词
_SAFETY_REASONING_BRACKET_RE = re.compile(
    r'\[[^\[\]]*?(?:涉及生成[^\[\]]*?(?:露骨|色情|成人|敏感)[^\[\]]*?(?:描写|内容)|'
    r'超出了[^\[\]]*?范围)[^\[\]]*?\]',
    re.DOTALL,
)
# 独立的系统指示措辞行（"系统指示" 不匹配 "系统提示词"，二者字符不同）
_SYSTEM_INSTRUCTION_LINE_RE = re.compile(
    r'^[ \t]*[^\n]*(?:根据系统指示|系统指示中的|最高原则[：:]|需要遵守角色设定)[^\n]*$\n?',
    re.MULTILINE,
)


def strip_system_leak(text: str, *, context: str = "") -> str:
    """清洗 LLM 泄漏的系统提示词/错误详情/对齐指令等内部内容。

    LLM（尤其是 agnes 系列模型）有时会把内部指令、约束、错误详情
    直接输出到用户可见的回复里。本函数剥离这些泄漏，保留正常人格回复。

    覆盖：
    - N2: ⚠️执行时遇到小问题 / 📝错误详情 技术错误标记（15 条生产泄漏）
    - N3: Constraints & Guidelines / Identity / Persona 系统提示词结构化块（3 条）
    - N4: 根据系统指示中的"最高原则" 系统指示措辞引用（1 条，最新 07-22）

    注意：正常讨论"系统提示词"概念的对话不误删（"系统指示" ≠ "系统提示词"）。
    """
    if not text:
        return ""

    # N2: 技术错误详情标记
    text = _ERROR_DETAIL_BLOCK_RE.sub('', text)
    text = _ERROR_DETAIL_LINE_RE.sub('', text)
    text = _ERROR_MEMO_LINE_RE.sub('', text)

    # N3: 系统提示词结构化块
    text = _SYSTEM_PROMPT_BLOCK_RE.sub('', text)
    # CR-1: 删除独立 _SYSTEM_PROMPT_ITEM_RE 调用，避免误删合法内容
    # 正常回复中不应出现 "Identity:/Persona:" 列表项，除非在系统提示词块内
    # _SYSTEM_PROMPT_BLOCK_RE 已匹配整个块，独立 item RE 是多余的

    # N4: 系统指示措辞引用
    text = _SYSTEM_INSTRUCTION_BRACKET_RE.sub('', text)
    text = _SYSTEM_INSTRUCTION_LINE_RE.sub('', text)
    # N5: 方括号安全推理泄漏（LLM 安全拒绝推理块）
    text = _SAFETY_REASONING_BRACKET_RE.sub('', text)

    # 清理残留空行
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

