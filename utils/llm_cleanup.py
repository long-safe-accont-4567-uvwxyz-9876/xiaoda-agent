"""LLM 输出清洗工具 —— 移除推理模型的思维链，仅保留最终回复。

从 greeting_scheduler.py / nudge_engine.py 提取的公共模块，
修复了正则模式过于具体导致推理文本泄漏的问题。
"""
from __future__ import annotations

import re

from loguru import logger

from utils.metrics import metrics

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


# N6: 生图类泄漏 —— LLM 伪造图片生成时复述的模型名 / 状态行 / 生图参数元数据
# 生产样本 conversation_logs id 1965/1966：
#   "Agnes Image 2.1 Flash 刚才跟我撒娇..."
#   "【图片生成中 —— Agnes Image 2.1 Flash ⚡】"
#   'Width Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| Prompt: "..."'
# 模型名用精确串删除（不误伤正常讨论）；状态行/元数据行要求完整特征序列才删。
_IMAGE_GEN_MODEL_NAMES = (
    "Agnes Image 2.1 Flash",
    "Agnes Video V2.0",
    "agnes-image-2.1-flash",
    "agnes-video-v2.0",
)
# 伪造状态行：【图片生成中 ...】/【视频生成中 ...】（不锚定行首，容忍内联出现）
_IMAGE_GEN_STATUS_LINE_RE = re.compile(r'【(?:图片|视频)生成中[^】]*】')
# 伪造生图元数据片段：要求 Width/Size + Seed + Model + Prompt 完整序列才删
# （避免误删普通含 Model/Prompt 的文本）。生产样本中元数据常内联在 markdown 图后。
# Prompt 分隔符容忍 : / . / ：，值用双引号包裹（生产样本均如此）。
_IMAGE_GEN_META_LINE_RE = re.compile(
    r'\.?\s*(?:Width\s*Height|Size|尺寸)[:：]?\s*\d+\s*[x×]\s*\d+.*?'
    r'(?:Seed|种子).*?(?:Model|模型).*?'
    r'(?:Prompt|提示词)\s*[.：:]?\s*"[^"]*"',
    re.IGNORECASE,
)
# pollinations URL 参数残留：异常 markdown 如 ![alt](url)?width=...&nologo=true)
# md_img_re 在 url 后第一个 ) 处停止匹配，留下 ?width=...&nologo=true) 残留。
# 要求完整参数序列（width+height+seed+nologo）才删，避免误伤正常 ?key=val 文本。
_IMAGE_GEN_URL_PARAMS_RE = re.compile(
    r'\?width=\d+&height=\d+&seed=\d+&nologo=true\)?'
)


def strip_image_gen_leak(text: str, *, context: str = "") -> str:
    """清洗 LLM 伪造图片生成时泄漏的模型名/状态行/生图参数元数据。

    覆盖（生产样本 id 1965/1966）：
    - 模型名：Agnes Image 2.1 Flash / Agnes Video V2.0 及其 model_id 形式
    - 伪造状态行：【图片生成中 —— ...】/【视频生成中 ...】
    - 伪造生图元数据行：Width Height: WxH | Seed: .. | Model: .. | Prompt: ..

    注意：markdown 图语法 ![](url) 的剥离由 _extract_fabricated_images_from_reply
    负责（它会下载 URL 发真图），本函数只管文本类泄漏，避免双重处理。
    正常人格回复不含上述精确串/完整元数据序列，无过删风险。
    """
    if not text:
        return ""
    # 1. 先删伪造状态行 / 元数据行 / URL 参数残留，避免模型名删除留下碎片
    text = _IMAGE_GEN_STATUS_LINE_RE.sub('', text)
    text = _IMAGE_GEN_META_LINE_RE.sub('', text)
    text = _IMAGE_GEN_URL_PARAMS_RE.sub('', text)
    # 2. 模型名精确删除：连同紧跟的单个空格一起删，避免行首空格残留
    for _name in _IMAGE_GEN_MODEL_NAMES:
        text = text.replace(_name + " ", "")
        text = text.replace(_name, "")
    # 3. 收拢多余空格与空行
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# QQ 表情标签泄漏：<faceType=1,faceId="N",ext="base64"/> 由 botpy 将用户发表情
# 序列化进 message.content，经对话历史被 LLM 模仿输出。输入侧（qq_bot_adapter）
# 与输出侧（_clean_reply_full）均需剥离，防止原始标签泄漏到 QQ 回复。
# 样本：'你凶我<faceType=1,faceId="5",ext="eyJ0ZXh0Ijoi5rWB5rOqIn0=">'
_QQ_FACE_TAG_RE = re.compile(
    r'<faceType=\d+,\s*faceId=["\']?\d+["\']?,\s*ext=["\'][^"\']*["\']\s*/?>'
)


def strip_qq_face_tags(text: str, *, context: str = "") -> str:
    """剥离 QQ 表情标签 <faceType=1,faceId="N",ext="..."/>。

    botpy 把用户发表情序列化成该字面标签塞进 message.content，会污染 LLM 上下文
    并被模仿输出。输入侧调用以阻止污染，输出侧调用作防御兜底。
    """
    if not text or "<faceType" not in text:
        return text
    cleaned = _QQ_FACE_TAG_RE.sub('', text)
    # 标签剥离后清理粘连的多余空白
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    return cleaned


# N7: 英文推理/计划泄漏 —— LLM（尤其是 agnes-2.0-flash）在生成长结构化回复时
# 会突然切换到英文"计划/总结"模式，泄漏内部推理过程。
# 生产样本 conversation_logs id 2107（2026-07-25 记忆回忆任务）：
#   "Anyway continuing now ~~~~\n\n(深吸一口气)\n好吧接下来继续讲完 ——>\n\n(Summary complete) -> Final Output Below."
# 共同特征：LLM 在中文回复中途插入英文过渡句/元指令，标志着实际内容已截断，
# 后续全是推理碎片而非用户可见回复。
# 检测到此类泄漏时：(1) 截断检测应判定为不完整 → 触发重试获取剩余内容；
# (2) 清洗时从首个泄漏标记处截断，保留之前的正常内容。
#
# CodeRabbit 复审修复：弱标记（"好吧接下来继续"、"Output Below"）单独出现时
# 可能是正常 prose，需要附近有强标记（——>、(Summary 等）才视为泄漏。
_STRONG_LEAK_PATTERNS = [
    re.compile(r'Anyway\s+continuing\s+now', re.IGNORECASE),
    re.compile(r'Summary\s+complete', re.IGNORECASE),
    re.compile(r'Final\s+Output\s+Below', re.IGNORECASE),
    re.compile(r'Output\s+Below\s*\.?', re.IGNORECASE),
    re.compile(r'继续讲完\s*——?>'),
]
# 弱标记：需要附近（前后100字符）有强标记才视为泄漏
# "好吧接下来继续"是中文，可能出现在正常prose中，需强标记上下文确认
_WEAK_LEAK_PATTERNS = [
    re.compile(r'好吧接下来继续'),
]
# 合并强标记为单一正则用于快速检测
_ENGLISH_REASONING_LEAK_RE = re.compile(
    r'(?:Anyway\s+continuing\s+now|Summary\s+complete|Final\s+Output\s+Below|'
    r'Output\s+Below\s*\.?|继续讲完\s*——?>)',
    re.IGNORECASE,
)


def has_english_reasoning_leak(text: str) -> bool:
    """检测回复中是否包含英文推理/计划泄漏。

    用于截断检测：当 LLM 在中文回复中途插入英文过渡句/元指令时，
    标志着实际内容已截断，应触发重试获取剩余内容。

    判定规则：
    - 强标记（Anyway continuing now / Summary complete / Final Output Below / 继续讲完 ——>）单独命中即视为泄漏
    - 弱标记（Output Below / 好吧接下来继续）只有在附近100字符内有强标记时才视为泄漏

    Returns:
        True 如果检测到英文推理泄漏模式
    """
    if not text:
        return False
    # 强标记：单独命中即视为泄漏
    for pat in _STRONG_LEAK_PATTERNS:
        if pat.search(text):
            return True
    # 弱标记：检查附近100字符内是否有强标记
    for pat in _WEAK_LEAK_PATTERNS:
        for m in pat.finditer(text):
            # 检查弱标记前后100字符范围内是否有强标记
            ctx_start = max(0, m.start() - 100)
            ctx_end = min(len(text), m.end() + 100)
            context_window = text[ctx_start:ctx_end]
            if any(sp.search(context_window) for sp in _STRONG_LEAK_PATTERNS):
                return True
    return False


def strip_english_reasoning_leak(text: str, *, context: str = "") -> str:
    """剥离英文推理/计划泄漏：从首个泄漏标记处截断，保留之前的正常内容。

    生产样本 id 2107 的泄漏尾部：
        "Anyway continuing now ~~~~\\n\\n(深吸一口气)\\n好吧接下来继续讲完 ——>\\n\\n(Summary complete) -> Final Output Below."
    清洗后只保留 "...大事件发生耶～" 之前的正常中文回复。

    注意：本函数只做截断（保留泄漏前的内容），不尝试修复截断——
    修复由上层截断重试机制（model_router / verification loop）负责，
    N8 后改用 assistant-prefill 续写（追加 assistant 消息让模型从末尾自然续写），
    merge_continuation(assume_tail=True) 兜底去重合并，避免原 user 消息
    "请继续"被 LLM 当成新提问回应导致续写指令泄漏与人格切换。

    CodeRabbit 复审修复：清洗后如果结果为空，返回原输入，避免空回复传入重试/force-close路径。
    """
    if not text:
        return ""
    # 找到首个强泄漏标记的位置
    earliest_pos = -1
    for pat in _STRONG_LEAK_PATTERNS:
        m = pat.search(text)
        if m and (earliest_pos == -1 or m.start() < earliest_pos):
            earliest_pos = m.start()
    if earliest_pos == -1:
        return text  # 无强泄漏标记，原样返回
    # 检查弱标记是否在强标记之前100字符内（如"好吧接下来继续"在"继续讲完 ——>"之前）
    # 如果是，从弱标记位置截断（弱标记是强标记的前缀）
    for pat in _WEAK_LEAK_PATTERNS:
        for m in pat.finditer(text):
            if m.start() <= earliest_pos and (earliest_pos - m.start()) <= 100:
                # 弱标记在强标记之前且相邻，从弱标记位置截断
                earliest_pos = m.start()
                break
    # 从泄漏标记处截断，保留之前的内容
    cleaned = text[:earliest_pos].rstrip()
    # 清理尾部残留的空行和空白
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    # CodeRabbit 复审：清洗后如果结果为空（泄漏在开头），记录日志
    # 调用者负责处理空回复（触发重试获取完整回复）
    if not cleaned:
        logger.warning("llm_cleanup.english_reasoning_leak_empty_after_strip",
                       context=context, original_len=len(text))
        return ""
    if cleaned != text:
        logger.warning("llm_cleanup.english_reasoning_leak_stripped",
                       context=context,
                       original_len=len(text),
                       cleaned_len=len(cleaned),
                       removed_len=len(text) - len(cleaned),
                       leak_preview=text[earliest_pos:earliest_pos + 80])
    return cleaned


# ── 截断重试合并：把续写内容安全合并进原回复 ──────────────────────
# 生产事故根因修复（conversation_logs id 2110/2112，2026-07-25）：
# 截断重试时若 LLM 重生成完整回复而非续写尾巴，盲拼接 `reply + retry_reply`
# 会产生 3-4 倍重复内容；多轮重试叠加后雪崩成 4 段人格切换的混乱回复。
# 本函数复用 message_processor._try_simple_chat_fast_path 的三态去重模式
# （生产验证的最佳实现），在所有重试拼接点统一调用，杜绝重复。
# 严格大于判定：overlap > _MERGE_OVERLAP_MIN 才视为有效续写重叠（即 overlap>=11），
# 避免"嗯。"等短尾部与下文偶然字符相同被误判为 spliced 而去重。
_MERGE_OVERLAP_MIN = 10


# 合法句末标点：用于判断 original 是否被截断（不以这些结尾 = 可能被截断）
# CodeRabbit #2 修复：移除逗号/分号/冒号/顿号（非句末标点）
# 原 implementation 含 ,;:、:，导致以这些结尾的回复被误判完整，截断无法修复
# CodeRabbit #6 修复：对齐 text_utils._SENTENCE_END_PUNCT 完整字符集
# 包含引号和右括号变体（' " " ' 》 〉 〕 ｝），避免以这些结尾的回复被误判截断
# 2830 事故修复：加入 ASCII ~ (0x7E) 与 〜 (U+301C) —— 颜文字/中文软语气常以
# ASCII ~ 结尾（如 (•̀ᴗ•́)و~、"好呀~"），旧集合只有全角 ～ (U+FF5E)，
# 导致 _looks_truncated 误判截断 → 假重试 → merge_continuation 拼接重复。
_SENTENCE_END_CHARS = set("。！？～…）」】.!?\"'”'）」】》〉〕｝\n~〜")


def _looks_truncated(text: str) -> bool:
    """判断文本是否像被截断（不以合法句末标记结尾）。

    用于 merge_continuation 的 assume_tail 分支：原回复被截断时必须拼接续写，
    否则截断无法修复（用户反馈"截断问题非常严重"根因）。

    判定规则（与 text_utils.ends_with_valid_ending 对齐，但本模块避免循环导入）：
      - 空文本 → 截断
      - 以标准句末标点结尾 → 完整
      - 以 emoji 结尾 → 完整
      - 以 ]/） 结尾（表情包/情绪标签）→ 完整
      - 其他 → 截断
    """
    if not text:
        return True
    rstripped = text.rstrip()
    if not rstripped:
        return True
    last = rstripped[-1]
    # 标准句末标点
    if last in _SENTENCE_END_CHARS:
        return False
    # emoji 范围（粗略覆盖常见 emoji 区段）
    cp = ord(last)
    if (0x1F000 <= cp <= 0x1FAFF  # Emoji 1.0-15.0
        or 0x2600 <= cp <= 0x27BF   # Misc symbols & dingbats
        or 0xFE00 <= cp <= 0xFE0F   # Variation selectors
        or cp == 0x200D):           # ZWJ（emoji 组合）
        return False
    # 标签结尾（[sticker:xxx] / [emotion:xxx]）
    if last in "]）":
        return False
    # 颜文字手部结尾（(•̀ᴗ•́)و、(๑˃̵ᴗ˂̵)و 等）：و 前是右括号 → 完整
    # 2830 事故配套：ASCII ~ 已在 _SENTENCE_END_CHARS 覆盖，此处兜底无尾部波浪线的
    # 纯手部颜文字结尾（结束字符恰为阿拉伯字母 و）。
    if last == "و" and len(rstripped) >= 2 and rstripped[-2] in ")）":
        return False
    return True


def _looks_like_regeneration(original: str, continuation: str) -> bool:
    """检测 continuation 是否是 LLM 重生成的完整回复（而非截断尾巴）。

    2830 事故（2026-08-08，DB reply 2829/2830）配套兜底：
    original 以颜文字结尾被误判截断后触发重试，LLM 重新生成的完整回复与
    original 主体内容高度相似——但并非逐字重复，漏过 merge_continuation 的
    子串/边界重叠检测，最终 truncated_appended 直接拼接出重复内容。

    判定依据：重生成的回复会复述 original 的主体内容，真尾巴是全新内容。
    用字符 bigram 重合率量化相似度（continuation 前 2/3 与 original 比较，
    真尾巴是增量内容，重合率天然低；重生成则大面积重合）。

    Returns:
        True 表示 continuation 疑似完整重生成（应替换而非拼接）
    """
    if not original or not continuation:
        return False
    o = original.strip()
    c = continuation.strip()
    # 过短的文本不做相似度判定（真尾巴也可能恰好短）
    if len(o) < 12 or len(c) < 12:
        return False
    probe = c[: max(len(c) * 2 // 3, 1)]
    o_bigrams = {o[i:i + 2] for i in range(len(o) - 1)}
    p_bigrams = {probe[i:i + 2] for i in range(len(probe) - 1)}
    if not o_bigrams or not p_bigrams:
        return False
    return len(o_bigrams & p_bigrams) / len(o_bigrams) >= 0.25


def merge_continuation(
    original: str,
    continuation: str,
    *,
    context: str = "",
    assume_tail: bool = False,
) -> tuple[str, str]:
    """把续写内容合并进原回复。返回 (合并结果, 动作)。

    用于截断重试场景：LLM 首轮回复被 max_tokens 截断，重试获取后续内容后合并。
    assistant-prefill 正常工作时 continuation 是纯尾巴，直接拼接；若 provider
    不支持 prefill 导致 LLM 重生成完整回复，本函数的三态去重也能避免重复。

    Args:
        original: 首轮（被截断的）回复内容
        continuation: 重试返回的续写内容
        context: 调用场景标识（用于日志追踪）
        assume_tail: 调用方是否采用 assistant-prefill 续写。
                    True 时，无边界重叠视为 prefill 成功的纯尾巴直接拼接（'appended'）。
                    CodeRabbit 复审 I1 评估：长度/字符特征无法可靠区分 prefill 尾巴
                    vs 人格切换重生成，强行硬判会误伤合法 prefill 尾巴。最终方案：
                    保持 appended 行为，长度比例 >= 0.7 时记录 warning 便于线上观测。
                    真正的根因（人格切换）由 N9（recall 超时不降级）+ N10（curator
                    I/O 减压）避免降级场景产生，而非在合并层硬判。
                    False 时，无重叠判定为 LLM 重生成完整回复，保留较长者。
                    子串检测（分支 2/3）始终优先于 assume_tail，能拦截 LLM 重生成
                    含 original 的扩展场景（事故 ID 2110 根因）。

    动作取值：
    - 'discarded'：丢弃 continuation（重复或较短的重生成），保留 original
    - 'replaced'：用 continuation 替换 original（continuation 是扩展或较长的重生成）
    - 'spliced'：去边界重叠后拼接（真正的续写，overlap>10 字符）
    - 'appended'：无重叠但 assume_tail=True，直接拼接（信任 prefill 尾巴）

    分支：
    1. continuation 空/过短(<=5字符) → 'discarded'
    2. continuation 是 original 子串 → LLM 重复，'discarded'
    3. original 是 continuation 子串 → continuation 是扩展，'replaced'
    4. 边界重叠(original 末尾 == continuation 开头，>10字符) → 'spliced'
    5. 无重叠 + assume_tail=False → 判定为重生成，保留较长者（'replaced'/'discarded'）
    6. 无重叠 + assume_tail=True → 视为 prefill 尾巴，'appended'（直接拼接）
    """
    if not continuation or len(continuation) <= 5:
        metrics.inc("llm.merge_continuation.discarded")
        return original, "discarded"
    if not original:
        metrics.inc("llm.merge_continuation.replaced")
        return continuation, "replaced"

    o_lower = original.lower()
    c_lower = continuation.lower()

    # 1/2. continuation 是 original 的子串 → LLM 重复了，丢弃
    # CodeRabbit #6：原条件 `c_lower in o_lower` 会让短续写（如"好的"）偶然命中
    # 正文中的重复措辞而被误杀。改为：仅当 continuation 较长（>=2*_MERGE_OVERLAP_MIN，
    # 子串匹配更可靠）或出现在 original 尾部区域（真正的重复续写才会出现在尾巴）时
    # 才判为重复。保留对 genuinely repeated trailing content 的丢弃语义。
    if c_lower in o_lower and (
        len(continuation) >= 2 * _MERGE_OVERLAP_MIN
        or c_lower in o_lower[-(len(continuation) + 100):]
    ):
        logger.info("llm_cleanup.merge_continuation.duplicate_discarded",
                    context=context, original_len=len(original),
                    continuation_len=len(continuation))
        metrics.inc("llm.merge_continuation.discarded")
        return original, "discarded"

    # 3. original 是 continuation 的子串 → continuation 是 original 的扩展，替换
    if o_lower in c_lower:
        logger.info("llm_cleanup.merge_continuation.extended_replaced",
                    context=context, original_len=len(original),
                    continuation_len=len(continuation))
        metrics.inc("llm.merge_continuation.replaced")
        return continuation, "replaced"

    # 4. 边界重叠检测：original 末尾与 continuation 开头是否相同
    overlap = 0
    check_len = min(len(original), len(continuation), 100)
    for i in range(check_len, _MERGE_OVERLAP_MIN, -1):
        if original[-i:].lower() == continuation[:i].lower():
            overlap = i
            break

    if overlap > _MERGE_OVERLAP_MIN:
        merged = original + continuation[overlap:]
        logger.info("llm_cleanup.merge_continuation.spliced",
                    context=context, original_len=len(original),
                    continuation_len=len(continuation), overlap=overlap,
                    merged_len=len(merged))
        metrics.inc("llm.merge_continuation.spliced")
        return merged, "spliced"

    # 5/6. 无重叠的两种处理路径
    if assume_tail:
        return _merge_no_overlap_assume_tail(original, continuation, context)

    # assume_tail=False：判定为重生成（assistant-prefill 失效，LLM 重新生成完整回复）
    # 保留较长者，避免两份完整回复拼接产生重复（生产事故根因）
    if len(continuation) > len(original):
        logger.warning("llm_cleanup.merge_continuation.regeneration_replaced",
                       context=context, original_len=len(original),
                       continuation_len=len(continuation),
                       note="no_overlap_kept_longer")
        metrics.inc("llm.merge_continuation.replaced")
        return continuation, "replaced"

    logger.info("llm_cleanup.merge_continuation.regeneration_discarded",
                context=context, original_len=len(original),
                continuation_len=len(continuation),
                note="no_overlap_kept_original")
    metrics.inc("llm.merge_continuation.discarded")
    return original, "discarded"


def _merge_no_overlap_assume_tail(
    original: str, continuation: str, context: str,
) -> tuple[str, str]:
    """assume_tail=True 时无重叠的合并策略：区分截断 vs 完整回复。"""
    if _looks_truncated(original):
        _cont_bytes = len(continuation.encode("utf-8"))
        _orig_bytes = len(original.encode("utf-8"))
        _MIN_CONT_BYTES = 20
        _MIN_ORIG_BYTES_FOR_DROP = 80
        if _cont_bytes < _MIN_CONT_BYTES and _orig_bytes >= _MIN_ORIG_BYTES_FOR_DROP:
            logger.info("llm_cleanup.merge_continuation.short_continuation_discarded",
                        context=context, original_len=_orig_bytes,
                        continuation_len=_cont_bytes,
                        note="continuation_too_short_keep_truncated_original")
            metrics.inc("llm.merge_continuation.short_continuation_discarded")
            return original, "discarded"
        if _looks_like_regeneration(original, continuation):
            logger.warning("llm_cleanup.merge_continuation.regeneration_detected",
                           context=context, original_len=_orig_bytes,
                           continuation_len=_cont_bytes,
                           note="similar_regeneration_replace_not_append")
            if _cont_bytes >= _orig_bytes:
                metrics.inc("llm.merge_continuation.replaced")
                return continuation, "replaced"
            metrics.inc("llm.merge_continuation.discarded")
            return original, "discarded"
        merged = original + continuation
        logger.warning("llm_cleanup.merge_continuation.truncated_appended",
                       context=context, original_len=_orig_bytes,
                       continuation_len=_cont_bytes,
                       note="original_truncated_append_to_recover")
        metrics.inc("llm.merge_continuation.truncated_appended")
        return merged, "appended"
    logger.warning("llm_cleanup.merge_continuation.assume_tail_no_overlap_discarded",
                   context=context, original_len=len(original),
                   continuation_len=len(continuation),
                   note="original_complete_discard_no_overlap_continuation")
    metrics.inc("llm.merge_continuation.assume_tail_no_overlap_discarded")
    return original, "discarded"
