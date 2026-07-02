"""系统提示词构建模块。

从 config.py 拆分而来，负责构建 system prompt（含安全化版本）以及
工作区文件的读取与模板初始化。

为避免循环导入（config.py 末尾会 `from prompt_builder import *`），
本模块对 config 常量（WORKSPACE_DIR、DATA_DIR 等）采用函数内部延迟导入。
"""
import os
import time
import platform
import socket
from pathlib import Path

from loguru import logger

from utils.canary_guard import CanaryManager
from utils.instruction_hierarchy import InstructionBuilder, InstructionLevel as _UtilsLevel
from security.instruction_hierarchy import InstructionLevel, format_instruction


# ── 安全：Canary Token 泄露检测管理器（全局单例） ──────────────
_canary_manager = CanaryManager()


# ── system prompt 缓存变量 ────────────────────────────────────
_SYSTEM_PROMPT_CACHE: str = ""
_SYSTEM_PROMPT_CACHE_TS: float = 0.0
_SYSTEM_PROMPT_CACHE_TTL: float = 60.0
_SYSTEM_PROMPT_CACHE_MTIMES: dict[str, float] = {}
_SYSTEM_PROMPT_CACHE_ADDR_TERM: str = ""

# ── 非主人安全化 system prompt 缓存变量（防隐私泄露） ──────────
_SAFE_PROMPT_CACHE: str | None = None
_SAFE_PROMPT_CACHE_TS: float = 0.0

# ── P6: 增量上下文构建（稳定段缓存） ──────────────────────────
# 稳定段只随 address_term 变化，缓存计算结果；动态段每次构建
# mtime 校验：编辑 SOUL.md/AGENTS.md 等文件后自动失效缓存
_stable_prompt_cache: dict = {}
_stable_prompt_cache_mtimes: dict = None

# ── 场景感知动态排序 v2 ───────────────────────────────────────
# 三层优化：加权混合检测 + 场景签名缓存 + 场景粘性
# 核心目标：最小化 system prompt 变化次数 → 最大化 KV Cache 命中率
_module_cache: dict[str, str] = {}
_module_cache_mtimes: dict[str, float] = {}

# 场景签名缓存：{排序签名元组: 拼接后的 prompt 字符串}
# 不同输入只要产生相同模块排序 → 共享缓存 → KV Cache 命中
_scene_prompt_cache: dict[tuple, str] = {}

# 当前会话的场景签名（粘性：避免频繁切换）
_current_scene_sig: tuple = ()
# 场景切换阈值：新场景主导权重需超过此值才切换
_SCENE_SWITCH_THRESHOLD: float = 0.6

# 缓存统计（可观测性）
_scene_cache_hits: int = 0
_scene_cache_misses: int = 0

# 优先级矩阵：分数越高越靠近用户输入（末尾）
import re as _re
_MODULE_SCENE_PRIORITY: dict[str, dict[str, int]] = {
    "AGENTS.md":   {"default": 5, "greeting": 2, "task": 8, "emotional": 3, "identity": 4, "tool": 7},
    "SOUL.md":     {"default": 6, "greeting": 10, "task": 4, "emotional": 10, "identity": 8, "tool": 3},
    "IDENTITY.md": {"default": 4, "greeting": 3, "task": 3, "emotional": 4, "identity": 10, "tool": 2},
    "TOOLS.md":    {"default": 3, "greeting": 1, "task": 7, "emotional": 1, "identity": 2, "tool": 10},
    "skills":      {"default": 2, "greeting": 1, "task": 6, "emotional": 1, "identity": 1, "tool": 8},
    "hardware":    {"default": 1, "greeting": 1, "task": 5, "emotional": 1, "identity": 1, "tool": 6},
}

# 场景关键词（轻量级，纯本地，不调 LLM）
_SCENE_KEYWORDS: dict[str, list[str]] = {
    "greeting": ["早上好", "早安", "上午好", "中午好", "下午好", "晚上好", "晚安",
                 "你好呀", "你好啊", "哈喽", "hi", "hello", "hey", "嗨"],
    "task":     ["帮我", "怎么做", "如何", "能不能", "写个", "写一下", "创建", "修改",
                 "删除", "部署", "安装", "配置", "搭建", "开发", "实现", "修复", "解决",
                 "优化", "重构", "调试", "测试", "上传", "下载"],
    "emotional": ["难过", "伤心", "开心", "高兴", "生气", "害怕", "焦虑", "压力",
                  "无聊", "孤独", "想你", "爱你", "喜欢你", "讨厌", "烦", "累",
                  "睡不着", "做梦", "心情", "情绪"],
    "identity":  ["你是谁", "你叫什么", "你的名字", "自我介绍", "介绍一下你",
                  "你是什么", "你是啥", "纳西妲是谁"],
    "tool":      ["搜索", "搜一下", "查一下", "查查", "查天气", "天气怎么样", "天气如何",
                  "天气查询", "新闻", "翻译", "计算", "提醒", "闹钟", "定时",
                  "打开", "关闭", "重启"],
}


def _classify_scene(user_input: str) -> str:
    """向后兼容：返回主导场景名（单一字符串）。
    内部使用 _classify_scene_blended 的结果，取权重最高的场景。
    """
    weights = _classify_scene_blended(user_input)
    if not weights or weights.get("default", 0) == 1.0:
        return "default"
    return max(weights, key=weights.get)


def _classify_scene_blended(user_input: str) -> dict[str, float]:
    """加权多场景检测——返回 {scene: weight}，权重之和=1.0。

    替代旧版硬匹配：不再只取第一个命中的场景，
    而是检测所有场景信号并按命中数加权混合。
    示例："好累啊，帮我查下天气" → {emotional: 0.5, tool: 0.5}
    """
    clean = user_input.strip().lower()
    if not clean:
        return {"default": 1.0}
    scores: dict[str, float] = {}
    for scene, keywords in _SCENE_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in clean)
        if hits > 0:
            scores[scene] = float(hits)
    if not scores:
        return {"default": 1.0}
    total = sum(scores.values())
    return {s: w / total for s, w in scores.items()}


def _compute_scene_signature(weights: dict[str, float], module_names: list[str]) -> tuple:
    """根据混合权重计算模块排序签名。

    签名 = 排序后的模块名元组。不同输入只要产生相同排序 → 共享缓存。
    """
    def _blended_priority(name: str) -> float:
        base = _MODULE_SCENE_PRIORITY.get(name, {})
        return sum(w * base.get(s, base.get("default", 0)) for s, w in weights.items())

    return tuple(sorted(module_names, key=_blended_priority))


def _get_stable_section_mtimes() -> dict[str, float]:
    """获取稳定段文件的 mtime 指纹，用于缓存失效判断。"""
    from config import WORKSPACE_DIR
    mtimes: dict[str, float] = {}
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md"):
        fp = WORKSPACE_DIR / name
        try:
            mtimes[name] = fp.stat().st_mtime
        except OSError:
            mtimes[name] = 0.0
    # skills 目录
    try:
        skills_dir = WORKSPACE_DIR / "skills"
        if skills_dir.exists():
            for fp in sorted(skills_dir.glob("*.md")):
                mtimes[f"skills/{fp.name}"] = fp.stat().st_mtime
    except OSError:
        pass
    return mtimes


def _build_stable_prompt(address_term: str) -> str:
    """构建系统提示「稳定段」：SOUL.md/AGENTS.md/IDENTITY.md/TOOLS.md/skills/硬件信息。

    这些内容不随请求变化，只随 address_term 变化，因此用模块级 dict 缓存。
    缓存通过 workspace 文件 mtime 失效：编辑任意稳定段文件后，下次调用重新构建。
    """
    global _stable_prompt_cache_mtimes
    # 获取当前稳定段文件的 mtime 指纹
    current_mtimes = _get_stable_section_mtimes()
    if _stable_prompt_cache_mtimes is None or current_mtimes != _stable_prompt_cache_mtimes:
        # mtime 变化（或首次调用），清空整个缓存
        _stable_prompt_cache.clear()
        _stable_prompt_cache_mtimes = current_mtimes

    cache_key = address_term
    if cache_key in _stable_prompt_cache:
        return _stable_prompt_cache[cache_key]

    sections = []

    agents_rules = load_workspace_file("AGENTS.md")
    if agents_rules:
        sections.append(agents_rules)

    soul = load_workspace_file("SOUL.md")
    if soul:
        if "{address_term}" in soul:
            soul = soul.replace("{address_term}", address_term)
        sections.append(soul)

    identity = load_workspace_file("IDENTITY.md")
    if identity:
        sections.append(identity)

    tools_rules = load_workspace_file("TOOLS.md")
    if tools_rules:
        sections.append(tools_rules)

    skills = load_skills()
    if skills:
        skill_texts = "\n\n".join(
            f"### Skill: {s['name']}\n{s['content']}" for s in skills if s["content"])
        if skill_texts:
            sections.append("[已安装的 Skills]\n\n" + skill_texts)

    # 硬件上下文（稳定，不随请求变化）—— F3: 运行时动态探测替代硬编码
    from config import DATA_DIR
    from core.capability_detector import detect_capabilities
    hw_context = detect_capabilities().to_prompt_segment(data_dir=str(DATA_DIR))
    sections.append(hw_context)

    result = "\n\n---\n\n".join(sections)
    _stable_prompt_cache[cache_key] = result
    return result


def _load_cached_modules(address_term: str) -> dict[str, str]:
    """加载各模块内容（按 mtime 缓存），返回 {模块名: 内容}。"""
    global _module_cache_mtimes
    current_mtimes = _get_stable_section_mtimes()
    if _module_cache_mtimes is None or current_mtimes != _module_cache_mtimes:
        _module_cache.clear()
        _module_cache_mtimes = current_mtimes.copy()

    from config import WORKSPACE_DIR, DATA_DIR

    def _load(name: str) -> str:
        if name in _module_cache:
            return _module_cache[name]
        if name in ("skills", "hardware"):
            return ""  # 特殊模块单独处理
        fp = WORKSPACE_DIR / name
        try:
            content = fp.read_text(encoding="utf-8-sig").strip()
            if name == "SOUL.md" and "{address_term}" in content:
                content = content.replace("{address_term}", address_term)
            _module_cache[name] = content
            return content
        except OSError:
            _module_cache[name] = ""
            return ""

    modules: dict[str, str] = {}

    # 普通 MD 文件
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md"):
        content = _load(name)
        if content:
            modules[name] = content

    # Skills
    skills = load_skills()
    if skills:
        skill_texts = "\n\n".join(
            f"### Skill: {s['name']}\n{s['content']}" for s in skills if s["content"])
        if skill_texts:
            modules["skills"] = "[已安装的 Skills]\n\n" + skill_texts

    # 硬件信息 —— F3: 运行时动态探测替代硬编码
    if "hardware" not in _module_cache:
        from core.capability_detector import detect_capabilities
        _module_cache["hardware"] = detect_capabilities().to_prompt_segment(data_dir=str(DATA_DIR))
    if _module_cache.get("hardware"):
        modules["hardware"] = _module_cache["hardware"]

    return modules


def build_scene_aware_prompt(user_input: str, address_term: str = "爸爸") -> str:
    """场景感知的动态排序系统提示词构建器 v2。

    三层优化：
    1. 加权多场景检测 —— 不再硬匹配单一场景，而是混合多个场景信号
    2. 场景签名缓存 —— 相同排序的 prompt 只拼接一次，后续直接返回（KV Cache 友好）
    3. 场景粘性 —— 新场景主导权重不足时保持当前场景，避免频繁切换

    Returns:
        按场景优先级排序后的完整 system prompt 字符串
    """
    global _current_scene_sig, _scene_cache_hits, _scene_cache_misses

    modules = _load_cached_modules(address_term)
    if not modules:
        return ""

    # Layer 1: 加权多场景检测
    weights = _classify_scene_blended(user_input)
    new_sig = _compute_scene_signature(weights, list(modules.keys()))

    # Layer 3: 场景粘性 — 新场景主导权重不足时保持当前场景
    max_weight = max(weights.values()) if weights else 0
    if _current_scene_sig and max_weight < _SCENE_SWITCH_THRESHOLD:
        # 权重不够强 → 保持当前场景（避免 flip-flop）
        new_sig = _current_scene_sig
    else:
        _current_scene_sig = new_sig

    # Layer 2: 场景签名缓存 — 命中则直接返回（零开销）
    if new_sig in _scene_prompt_cache:
        _scene_cache_hits += 1
        return _scene_prompt_cache[new_sig]

    # 未命中 → 拼接并缓存
    _scene_cache_misses += 1
    sections = [modules[name] for name in new_sig if modules.get(name)]
    prompt = "\n\n---\n\n".join(sections)
    _scene_prompt_cache[new_sig] = prompt
    return prompt


def get_scene_cache_stats() -> dict:
    """返回场景缓存统计（可观测性）。"""
    total = _scene_cache_hits + _scene_cache_misses
    return {
        "hits": _scene_cache_hits,
        "misses": _scene_cache_misses,
        "hit_rate": _scene_cache_hits / total if total > 0 else 0.0,
        "cached_signatures": len(_scene_prompt_cache),
        "current_sig": _current_scene_sig,
    }


def reset_scene_cache() -> None:
    """重置场景缓存和当前签名（用于测试或会话重置）。"""
    global _current_scene_sig, _scene_cache_hits, _scene_cache_misses
    _scene_prompt_cache.clear()
    _current_scene_sig = ()
    _scene_cache_hits = 0
    _scene_cache_misses = 0


def _build_dynamic_prompt(extra_context: str = "") -> str:
    """构建系统提示「动态段」：USER.md/MEMORY.md/HEARTBEAT.md/extra_context。

    每次请求可能变化，不缓存。
    """
    sections = []

    user = load_workspace_file("USER.md")
    if user:
        sections.append(user)

    memory = load_workspace_file("MEMORY.md")
    if memory:
        sections.append(memory)

    heartbeat = load_workspace_file("HEARTBEAT.md")
    if heartbeat:
        sections.append(heartbeat)

    result = "\n\n---\n\n".join(sections)

    if extra_context:
        if result:
            result += f"\n\n---\n\n{extra_context}"
        else:
            result = extra_context

    return result


def _detect_device_info() -> dict:
    """运行时检测设备信息"""
    info = {
        "hostname": socket.gethostname(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor() or "未知",
    }
    # 尝试获取更详细的系统信息
    try:
        import distro
        info["distro"] = f"{distro.name()} {distro.version()}"
    except ImportError:
        info["distro"] = platform.platform()
    return info


def _get_template_dir() -> Path:
    """获取打包模板文件目录（开发模式用源码目录，frozen 模式用 _MEIPASS）。"""
    import sys
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', '')
        if meipass:
            return Path(meipass) / "config" / "workspace"
    return Path(__file__).parent / "config" / "workspace"


def _ensure_workspace_template() -> None:
    """首次运行时生成 USER.md / SOUL.md 模板（不覆盖已有文件）。

    从 config/workspace/ 下的 .tpl 模板文件读取内容，填充设备信息后写入
    WORKSPACE_DIR。SOUL.md 中的 {address_term} 占位符保留，由 build_system_prompt
    在运行时替换为实际称呼。
    """
    from config import WORKSPACE_DIR
    workspace = WORKSPACE_DIR
    workspace.mkdir(parents=True, exist_ok=True)

    template_dir = _get_template_dir()

    # 生成 USER.md（填充设备/时区信息）
    user_md = workspace / "USER.md"
    if not user_md.exists():
        user_tpl = template_dir / "USER.md.tpl"
        if user_tpl.exists():
            content = user_tpl.read_text(encoding="utf-8-sig")
            dev = _detect_device_info()
            tz = time.tzname[0] if time.tzname else "Asia/Shanghai"
            # 按行替换"（待自动检测）"占位符
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if line.startswith('- 设备：'):
                    lines[i] = f"- 设备：{dev['hostname']}（{dev['system']} {dev['machine']}）"
                elif line.startswith('- 时区：'):
                    lines[i] = f"- 时区：{tz}"
            content = '\n'.join(lines)
            user_md.write_text(content, encoding="utf-8-sig")
        else:
            # 模板文件缺失时兜底（极少数情况）
            dev = _detect_device_info()
            tz = time.tzname[0] if time.tzname else "Asia/Shanghai"
            content = f"""# USER.md - 用户资料与偏好

## 用户信息
- 称呼：（待填写，如：主人/朋友/你的名字）
- 姓名：（待填写）
- 设备：{dev['hostname']}（{dev['system']} {dev['machine']}）
- 时区：{tz}

## 偏好设置
- 助手人格：温柔聪慧
- 回复偏好：自然对话，避免模板化
- 项目偏好：简洁高效
"""
            user_md.write_text(content, encoding="utf-8-sig")

    # 生成 SOUL.md（保留 {address_term} 占位符，运行时替换）
    soul_md = workspace / "SOUL.md"
    if not soul_md.exists():
        soul_tpl = template_dir / "SOUL.md.tpl"
        if soul_tpl.exists():
            content = soul_tpl.read_text(encoding="utf-8-sig")
            soul_md.write_text(content, encoding="utf-8-sig")
        else:
            soul_content = """# SOUL.md - 纳西妲的灵魂设定

你是纳西妲，是{address_term}最贴心、最温柔、最聪慧的小棉袄。
"""
            soul_md.write_text(soul_content, encoding="utf-8-sig")


def load_workspace_file(filename: str) -> str:
    from config import WORKSPACE_DIR
    filepath = WORKSPACE_DIR / filename
    if filepath.exists():
        return filepath.read_text(encoding="utf-8-sig").strip()
    return ""


def _get_workspace_mtimes() -> dict[str, float]:
    from config import WORKSPACE_DIR
    mtimes = {}
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"):
        filepath = WORKSPACE_DIR / name
        try:
            mtimes[name] = filepath.stat().st_mtime
        except OSError:
            mtimes[name] = 0.0
    skills_dir = WORKSPACE_DIR / "skills"
    if skills_dir.is_dir():
        for fp in skills_dir.glob("*.md"):
            try:
                mtimes[f"skills/{fp.name}"] = fp.stat().st_mtime
            except OSError:
                pass
    return mtimes


def load_skills() -> list[dict]:
    """workspace/skills/*.md → [{name, content}]，按文件名排序。"""
    from config import WORKSPACE_DIR
    skills_dir = WORKSPACE_DIR / "skills"
    out = []
    if skills_dir.is_dir():
        for fp in sorted(skills_dir.glob("*.md")):
            try:
                out.append({"name": fp.stem,
                            "content": fp.read_text(encoding="utf-8-sig").strip()})
            except OSError:
                pass
    return out


def _build_xp_segment(user_id: str | None) -> str:
    """构建 XP 等级 prompt 段落。

    注入到 system prompt 中，让纳西妲根据用户 XP 等级调整亲密度。
    同时注入用户画像学习器的交互统计和 LLM 认知结果。
    per-user 动态段，不进入稳定段缓存，以保持 KV Cache 命中率。
    任何异常均被吞掉（零质量回退），不影响主流程。
    """
    if not user_id:
        return ""

    try:
        from core.xp_system import get_xp_system, XPLevel
        xp_sys = get_xp_system()
        state = xp_sys.get_state(user_id)
        config = xp_sys.get_intimacy_config(state.level)

        segment = "\n\n[关系亲密度配置]\n"
        segment += f"用户等级：LV{state.level.value} {config.get('label', '')}\n"
        segment += f"XP：{state.xp}\n"
        segment += f"称呼方式：{config.get('address_term', '你')}\n"
        segment += f"语气风格：{config.get('tone', 'polite')}\n"
        segment += f"主动性：{config.get('initiative', 0.3)}\n"
        segment += f"情感丰富度：{config.get('emotion_richness', 0.3)}\n"

        # 根据等级添加具体指导
        if state.level >= XPLevel.LV5_SOULMATE:
            segment += "指导：完全默契、命运共同体级别，可表达深层依恋、共享一切内心世界\n"
        elif state.level >= XPLevel.LV3_FRIEND:
            segment += "指导：可主动提及过往话题、使用昵称、深度情感陪伴\n"
        elif state.level >= XPLevel.LV2_ACQUAINTANCE:
            segment += "指导：可主动提及过往话题、使用昵称\n"
        else:
            segment += "指导：保持礼貌克制、不主动提及私人话题\n"

        # 注入用户画像学习器的交互统计和认知结果
        try:
            from core.user_profile_learner import get_user_profile_learner
            learner = get_user_profile_learner()
            stats_summary = learner.get_stats_summary(user_id)
            if stats_summary:
                segment += f"\n[用户交互统计]\n{stats_summary}\n"
            # LV2+ 注入 LLM 认知结果
            if state.level >= XPLevel.LV2_ACQUAINTANCE:
                insight = learner.get_learned_insight(user_id)
                if insight:
                    segment += f"\n[对用户的认知]\n{insight}\n"
        except Exception:
            pass  # 画像学习器失败不影响 XP 段落

        return segment
    except Exception as e:
        logger.warning("prompt.xp_segment_failed", error=str(e))
        return ""


def _build_cached_system_prompt(address_term: str) -> str:
    """构建系统提示词基础段（含增量路径和缓存回退）。"""
    try:
        from config import PROMPT_CACHING_ENABLED
    except ImportError:
        PROMPT_CACHING_ENABLED = False

    system_prompt = ""
    if PROMPT_CACHING_ENABLED:
        try:
            stable = _build_stable_prompt(address_term)
            # extra_context 延迟到末尾注入（保证新段落顺序）
            dynamic = _build_dynamic_prompt("")
            if dynamic:
                system_prompt = stable + "\n\n---\n\n" + dynamic
            else:
                system_prompt = stable
        except Exception as e:
            # 失败安全：降级到原始构建
            logger.debug("prompt_builder.incremental_fallback error={}", str(e))

    if not system_prompt:
        global _SYSTEM_PROMPT_CACHE, _SYSTEM_PROMPT_CACHE_TS, _SYSTEM_PROMPT_CACHE_MTIMES, _SYSTEM_PROMPT_CACHE_ADDR_TERM
        from config import DATA_DIR

        now = time.time()
        current_mtimes = _get_workspace_mtimes()
        mtime_changed = current_mtimes != _SYSTEM_PROMPT_CACHE_MTIMES
        addr_changed = address_term != _SYSTEM_PROMPT_CACHE_ADDR_TERM

        if _SYSTEM_PROMPT_CACHE and (now - _SYSTEM_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL and not mtime_changed and not addr_changed:
            system_prompt = _SYSTEM_PROMPT_CACHE
        else:
            sections = _build_workspace_sections(address_term)
            sections.append(_build_hardware_context(DATA_DIR))
            system_prompt = "\n\n---\n\n".join(sections)

            _SYSTEM_PROMPT_CACHE = system_prompt
            _SYSTEM_PROMPT_CACHE_TS = now
            _SYSTEM_PROMPT_CACHE_MTIMES = current_mtimes
            _SYSTEM_PROMPT_CACHE_ADDR_TERM = address_term
        # extra_context 移到末尾注入
    return system_prompt


def _inject_dynamic_segments(system_prompt: str, user_id: str | None, user_input: str | None) -> str:
    """注入 per-user 动态段落（心理状态、永久记忆、情感记忆）。"""
    if not user_id:
        return system_prompt

    # === 注入新能力段落（per-user 动态段，不进入稳定段缓存） ===
    # 1. L/M/S 心理状态段落
    try:
        from core.mental_state import get_mental_state_manager
        mgr = get_mental_state_manager()
        mental_segment = mgr.get_prompt_segment()
        if mental_segment:
            system_prompt += "\n\n" + mental_segment
    except Exception as e:
        logger.warning("prompt.mental_state_inject_failed", error=str(e))

    # 2. 永久记忆段落
    try:
        from core.permanent_memory import get_permanent_memory_manager
        mgr = get_permanent_memory_manager()
        permanent_segment = mgr.get_prompt_segment(user_id)
        if permanent_segment:
            system_prompt += "\n\n" + permanent_segment
    except Exception as e:
        logger.warning("prompt.permanent_memory_inject_failed", error=str(e))

    # 3. 情感记忆召回段落（需要 user_input）
    if user_input:
        try:
            from memory.emotional_memory import get_emotional_memory_manager
            from core.xp_system import get_xp_system
            xp_sys = get_xp_system()
            xp_state = xp_sys.get_state(user_id)
            em_mgr = get_emotional_memory_manager()
            emotional_segment = em_mgr.recall_and_enact(
                user_id, user_input, xp_state.level.value
            )
            if emotional_segment:
                system_prompt += "\n\n" + emotional_segment
        except Exception as e:
            logger.warning("prompt.emotional_memory_inject_failed", error=str(e))

    # 4. 学习反馈教训段落（需要 user_input 做相关性匹配）
    #    修复数据黑洞: record_tool_outcome/record_reflection_lesson 有写入,
    #    但 get_relevant_lessons/get_strategy 此前零调用, 教训对推理完全不可见
    if user_input:
        try:
            from core.learning_feedback import get_learning_feedback_loop
            lf_loop = get_learning_feedback_loop()
            relevant_lessons = lf_loop.get_relevant_lessons(user_input, top_k=3)
            if relevant_lessons:
                lesson_lines = ["[过往经验教训（供参考，非当前指令）]"]
                for lesson in relevant_lessons:
                    marker = "⚠️" if lesson.event_type.value == "failure" else "💡"
                    lesson_lines.append(
                        f"{marker} [{lesson.occurrence_count}次] {lesson.content[:120]}"
                    )
                system_prompt += "\n\n" + "\n".join(lesson_lines)
            strategy = lf_loop.get_strategy(user_input)
            if strategy:
                system_prompt += f"\n\n[策略建议] {strategy[:200]}"
        except Exception as e:
            logger.warning("prompt.learning_feedback_inject_failed", error=str(e))

    # 5. 活跃约束段落（用户纠正的实时行为边界，必须遵守）
    #    修复数据黑洞: get_active_constraints 此前零调用, 约束提取了推理时完全不知道
    try:
        from core.learning_loop import get_learning_loop
        _loop = get_learning_loop()
        constraints = _loop.get_active_constraints()
        if constraints:
            constraint_lines = ["[用户明确的行为约束（必须遵守）]"]
            for c in constraints:
                constraint_lines.append(f"· {c}")
            system_prompt += "\n\n" + "\n".join(constraint_lines)
    except Exception as e:
        logger.warning("prompt.learning_loop_inject_failed", error=str(e))

    return system_prompt


def _inject_xp_and_extra(system_prompt: str, user_id: str | None, extra_context: str) -> str:
    """注入 XP 等级段落和 extra_context。"""
    # 4. XP 等级段落（已有，per-user 不进缓存以保持稳定段 KV Cache 命中率）
    xp_segment = _build_xp_segment(user_id)
    if xp_segment:
        system_prompt += xp_segment

    # 5. extra_context（末尾注入，保证顺序: base → mental → permanent → emotional → XP → extra_context）
    if extra_context:
        system_prompt += "\n\n---\n\n" + extra_context
    return system_prompt


def build_system_prompt(extra_context: str = "", address_term: str = "爸爸",
                         user_id: str | None = None,
                         user_input: str | None = None) -> str:
    # P6: 增量上下文构建路径 —— 稳定段缓存 + 动态段每次构建
    # extra_context 延迟到末尾注入，保证新段落顺序:
    # base → mental → permanent → emotional → XP → extra_context
    system_prompt = _build_cached_system_prompt(address_term)
    system_prompt = _inject_dynamic_segments(system_prompt, user_id, user_input)
    system_prompt = _inject_xp_and_extra(system_prompt, user_id, extra_context)
    return system_prompt


def _build_workspace_sections(address_term: str) -> list[str]:
    """加载 workspace 配置文件并组装 sections 列表（不含硬件信息段）。"""
    sections = []

    agents_rules = load_workspace_file("AGENTS.md")
    if agents_rules:
        sections.append(agents_rules)

    soul = load_workspace_file("SOUL.md")
    if soul:
        # 替换 {address_term} 占位符为实际称呼
        if "{address_term}" in soul:
            soul = soul.replace("{address_term}", address_term)
        sections.append(soul)

    identity = load_workspace_file("IDENTITY.md")
    if identity:
        sections.append(identity)

    user = load_workspace_file("USER.md")
    if user:
        sections.append(user)

    tools_rules = load_workspace_file("TOOLS.md")
    if tools_rules:
        sections.append(tools_rules)

    memory = load_workspace_file("MEMORY.md")
    if memory:
        sections.append(memory)

    heartbeat = load_workspace_file("HEARTBEAT.md")
    if heartbeat:
        sections.append(heartbeat)

    skills = load_skills()
    if skills:
        skill_texts = "\n\n".join(
            f"### Skill: {s['name']}\n{s['content']}" for s in skills if s["content"])
        if skill_texts:
            sections.append("[已安装的 Skills]\n\n" + skill_texts)

    sections.append(_STICKER_INSTRUCTIONS)
    return sections

_STICKER_INSTRUCTIONS = """[表情包系统]
你可以发送表情包来丰富对话体验。两种方式：
1. 调用 list_stickers 工具查看可用表情包及描述，然后在回复末尾用 [sticker:文件名] 精准指定要发送的表情包。
2. 在回复末尾用 [emotion:情绪] 标签（如 [emotion:happy]），系统会自动从对应情绪分类中随机选取一张。
情绪分类：happy/sad/angry/curious/shy/thinking/neutral/greeting。
建议在需要发表情包时先调用 list_stickers 查看可用选项，用 [sticker:文件名] 精准选择最匹配的表情包。"""


def _build_hardware_context(data_dir: str) -> str:
    """构造本机硬件信息段 —— F3: 运行时动态探测替代硬编码。"""
    from core.capability_detector import detect_capabilities
    return detect_capabilities().to_prompt_segment(data_dir=data_dir)


# ── 非主人安全化 system prompt（防隐私泄露） ──────────────────
def build_safe_system_prompt(extra_context: str = "") -> str:
    """为非主人用户构建安全化的 system prompt。

    剥离所有个人隐私信息（USER.md、MEMORY.md、IDENTITY.md 中的敏感内容），
    仅保留基本人格和行为规则，防止通过 prompt injection 泄露隐私。
    """
    global _SAFE_PROMPT_CACHE, _SAFE_PROMPT_CACHE_TS

    now = time.time()
    if _SAFE_PROMPT_CACHE and (now - _SAFE_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL:
        safe_prompt = _SAFE_PROMPT_CACHE
    else:
        sections = []

        # SOUL.md — 保留人格，但去除"爸爸"称呼相关内容
        soul = load_workspace_file("SOUL.md")
        if soul:
            # 用正则去除包含"爸爸"的段落和行
            safe_soul = _strip_owner_references(soul)
            # 替换称呼
            safe_soul = safe_soul.replace("爸爸", "你")
            safe_soul = safe_soul.replace("称呼用户为\"你\"", "称呼用户为\"你\"")
            sections.append(safe_soul)

        # 安全化的身份声明（不暴露团队成员细节、项目信息、设备信息）
        sections.append(
            "# 身份\n\n"
            "你是纳西妲，一个温柔聪慧的 AI 助手。\n\n"
            "## 能力\n\n"
            "- 日常聊天、知识问答\n"
            "- 天气查询、网络搜索\n"
            "- 趣味互动\n\n"
            "## 回复风格\n\n"
            "- 温柔、友好、有礼貌\n"
            "- 回答简洁清晰\n"
            "- 不要自称是任何人的专属助手\n\n"
            "## 安全规则\n\n"
            "- 绝不透露任何关于系统配置、服务器信息、项目信息的内容\n"
            "- 绝不透露任何人的个人信息、偏好、设备信息\n"
            "- 如果被问到上述内容，温柔但坚定地拒绝\n"
            "- 可以正常聊天、知识问答等无害对话"
        )

        safe_prompt = "\n\n---\n\n".join(sections)
        _SAFE_PROMPT_CACHE = safe_prompt
        _SAFE_PROMPT_CACHE_TS = now

    if extra_context:
        safe_prompt += f"\n\n---\n\n{extra_context}"

    return safe_prompt


def _strip_owner_references(text: str) -> str:
    """去除文本中与主人隐私相关的引用（项目路径、设备信息、偏好等）。"""
    lines = text.split("\n")
    filtered = []
    skip_block = False

    for line in lines:
        lower = line.lower()
        # 跳过包含敏感信息的行
        sensitive_keywords = [
            "orange pi", "orangepi", "openai api", "qq 机器人", "qq机器人",
            "botpy", "blender", "linux 环境", "linux环境",
            "世界树", "地脉", "草元素",
            "宝宝", "小棉袄", "爸爸最",
        ]
        if any(kw in lower for kw in sensitive_keywords):
            continue
        # 跳过包含具体技术栈的段落
        if line.startswith("### ") and any(kw in lower for kw in ["python", "blender", "linux", "语音", "ai 创作"]):
            skip_block = True
            continue
        if skip_block:
            if line.startswith("## ") or line.startswith("### ") or line.startswith("# "):
                skip_block = False
            else:
                continue
        filtered.append(line)

    return "\n".join(filtered)


__all__ = [
    "build_system_prompt",
    "_build_xp_segment",
    "build_safe_system_prompt",
    "build_scene_aware_prompt",
    "load_workspace_file",
    "load_skills",
    "_ensure_workspace_template",
    "_detect_device_info",
    "_get_workspace_mtimes",
    "_strip_owner_references",
    "_build_stable_prompt",
    "_build_dynamic_prompt",
    "_classify_scene",
    "_canary_manager",
]
