"""系统提示词装配子模块（自 prompt_builder.py 逐字节搬移）。

内容：稳定段 mtime 指纹与缓存（_get_stable_section_mtimes/_stable_prompt_cache）、
占位符替换与 USER.md 语义标注（_replace_placeholders/_annotate_user_profile）、
统一 section 装配引擎（单一事实源注释块 + _MD_MODULES/_ORDER_* 有序表 +
_iter_module_sections/_assemble_module_list/_compose_skills_segment +
_build_stable_prompt）、XP 段（_build_xp_segment）、动态注入
（_inject_dynamic_segments/_inject_xp_and_extra）、总入口
（build_system_prompt/_build_workspace_sections/_build_hardware_context/
build_safe_system_prompt/_strip_owner_references）。

兼容契约：所有名称经包门面 re-export；对 scene/workspace 子模块的引用
改为包内相对导入；config 为函数内延迟导入。
"""
import re as _re
import time

from loguru import logger

from prompt_builder._prompt_common import (
    _cache_lock,
    _guard_injected_text,
    _inject_canary,
)
from prompt_builder._prompt_scene import (
    _build_dynamic_prompt,
)
from prompt_builder._prompt_workspace import (
    load_skills,
    load_workspace_file,
)

# ── P6: 增量上下文构建（稳定段缓存） ──────────────────────────
# 稳定段只随 address_term 变化，缓存计算结果；动态段每次构建
# mtime 校验：编辑 SOUL.md/AGENTS.md 等文件后自动失效缓存
_stable_prompt_cache: dict = {}
_stable_prompt_cache_mtimes: dict = None

# ── system prompt 缓存变量 ────────────────────────────────────
_SYSTEM_PROMPT_CACHE: str = ""
_SYSTEM_PROMPT_CACHE_TS: float = 0.0
_SYSTEM_PROMPT_CACHE_TTL: float = 60.0
_SYSTEM_PROMPT_CACHE_MTIMES: dict[str, float] = {}
_SYSTEM_PROMPT_CACHE_ADDR_TERM: str = ""

# ── 非主人安全化 system prompt 缓存变量（防隐私泄露） ──────────
_SAFE_PROMPT_CACHE: str | None = None
_SAFE_PROMPT_CACHE_TS: float = 0.0
_SAFE_PROMPT_CACHE_NAME: str = ""  # 构建缓存时使用的 display_name，变化时失效缓存
_SAFE_PROMPT_CACHE_ADDR: str = ""  # 构建缓存时的 address_term，变化时失效缓存

# ── P6: 增量上下文构建（稳定段缓存） ──────────────────────────
# 稳定段只随 address_term 变化，缓存计算结果；动态段每次构建
# mtime 校验：编辑 SOUL.md/AGENTS.md 等文件后自动失效缓存


def _get_stable_section_mtimes() -> dict[str, float]:
    """获取稳定段文件的 mtime 指纹，用于缓存失效判断。"""
    from config import WORKSPACE_DIR
    mtimes: dict[str, float] = {}
    # 矩阵覆盖的所有模块 (9 个 MD + skills + hardware)
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md",
                 "USER.md", "MEMORY.md", "HEARTBEAT.md"):
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
        logger.debug("prompt_builder.stable_section_mtimes_failed", exc_info=True)
    return mtimes


def _replace_placeholders(content: str, address_term: str, agent_name: str = "") -> str:
    """替换 workspace 文件中的 {address_term}、{agent_name}、{name} 占位符。

    {address_term} - 对话中使用的称呼（如"爸爸"）
    {agent_name} - Agent 的显示名称（如"小妲"）
    {name} - 用户的昵称/姓名（如"飞"），从 USER.md 读取
    """
    if "{address_term}" in content:
        content = content.replace("{address_term}", address_term)
    if agent_name and "{agent_name}" in content:
        content = content.replace("{agent_name}", agent_name)

    # 支持用户昵称/姓名占位符 {name}
    if "{name}" in content:
        try:
            import re as _re_inner

            from config import WORKSPACE_DIR
            user_md = WORKSPACE_DIR / "USER.md"
            if user_md.exists():
                user_content = user_md.read_text(encoding="utf-8-sig")
                m = _re_inner.search(r'-\s*姓名[：:]\s*(.+)', user_content)
                if m:
                    user_name = m.group(1).strip()
                    # 过滤占位符
                    if user_name and not user_name.startswith("（"):
                        content = content.replace("{name}", user_name)
        except Exception as e:
            logger.debug("prompt_builder.user_name_substitution_failed", error=str(e), exc_info=True)
    return content


def _annotate_user_profile(content: str, address_term: str) -> str:
    """为 USER.md 中的称呼/姓名字段添加语义标注，帮助 LLM 区分角色。

    称呼 = 对外表达时使用的唯一称谓（active）
    姓名 = 仅供了解的背景信息，不要用来称呼（passive）
    """
    # 标注称呼行：强调这是唯一的对话称谓
    content = _re.sub(
        r'^(-\s*称呼[：:]\s*.+)$',
        r'\1（对话中对用户的唯一称呼，所有场景都用这个）',
        content,
        flags=_re.MULTILINE,
    )
    # 标注姓名行：明确这是背景知识，不用于称呼
    content = _re.sub(
        r'^(-\s*姓名[：:]\s*.+)$',
        r'\1（背景信息，不要用来称呼用户）',
        content,
        flags=_re.MULTILINE,
    )
    return content


# ════════════════════════════════════════════════════════════════════════════
# 统一 section 装配引擎（单一事实源）
# ════════════════════════════════════════════════════════════════════════════
# 背景：此前三处装配点（_build_stable_prompt / _load_cached_modules /
# _build_workspace_sections）各自复制了一份「读文件 → 清洗 → 收集」循环，
# 且清洗规则互有出入（USER/MEMORY/HEARTBEAT 的占位符处理不一致）。
# 现收敛为：数据驱动的有序装配表 + 单一引擎循环 + 统一的加载/清洗原语。
#
# 缓存契约（关键约束）：
#   - _normalize_module 与 address_term 无关，结果可安全进入 mtime 缓存；
#   - _finalize_module 每请求执行占位符定稿，绝不写入 mtime 缓存
#     （同一进程可能服务多个不同称呼的用户）。
#
# 各路径的 section 顺序差异是既有产品行为，由下方有序装配表分别声明；
# 内容加载与清洗逻辑则全部经由本节的原语走同一份代码。
# （主路径 Stable Prefix 序见上方 _STABLE_PREFIX_ORDER；场景层由 _BUCKET_ORDERINGS 动态排序。）
_MD_MODULES: tuple = ("AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md",
                      "USER.md", "MEMORY.md", "HEARTBEAT.md")

# 遗留增量路径（PROMPT_CACHING_ENABLED）：稳定段 / 动态段各自固定序
_ORDER_INCREMENTAL_STABLE: tuple = ("AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md")
_ORDER_INCREMENTAL_DYNAMIC: tuple = ("USER.md", "MEMORY.md", "HEARTBEAT.md")

# 遗留兜底路径（增量构建异常时）：全模块固定序
_ORDER_LEGACY_FALLBACK: tuple = ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md",
                                 "TOOLS.md", "MEMORY.md", "HEARTBEAT.md")


def _resolve_main_display_name() -> str:
    """主体 agent 的 display_name（{agent_name} 占位符替换用），失败回退 xiaoda。

    此前三处装配点各有一份相同的 try/except 副本，现收敛于此。
    """
    try:
        from config import get_agent_display_name
        return get_agent_display_name("xiaoda") or "xiaoda"
    except (ImportError, AttributeError, ValueError):
        return "xiaoda"
    except Exception:
        logger.exception("prompt_builder.agent_display_name_unexpected")
        return "xiaoda"


def _normalize_module(name: str, content: str) -> str:
    """模块归一化（缓存安全）：与 address_term 无关的内容级清洗。

    目前仅 USER.md 需要称呼/姓名语义标注；_annotate_user_profile 的标注文本
    为常量、不依赖称呼词，故归一化结果可安全进入 mtime 缓存。
    """
    if name == "USER.md":
        return _annotate_user_profile(content, "")
    return content


def _finalize_module(name: str, content: str, address_term: str, agent_dn: str) -> str:
    """模块定稿（每请求）：替换 {address_term}/{agent_name}/{name} 占位符。

    所有 MD 模块统一走此出口 —— 不再区分「带/不带 display_name」两种变体，
    消除遗留路径把占位符字面量发给 LLM 的隐患。
    """
    return _replace_placeholders(content, address_term, agent_dn)


def _iter_module_sections(order: tuple, *, loader):
    """装配引擎：按有序表逐个产出 (模块名, 归一化内容)，跳过缺失/空模块。

    _normalize_module 在此统一应用 —— 三处装配点的清洗逻辑由此收敛为一份；
    loader 只负责原始读取（如共享的 load_workspace_file 或带缓存的内部读取器），
    保证 USER.md 语义标注等清洗在所有路径下恰好执行一次、不会叠加。
    """
    for name in order:
        content = loader(name)
        if content:
            yield name, _normalize_module(name, content)


def _assemble_module_list(order: tuple, address_term: str, agent_dn: str, *,
                          loader=None) -> list[str]:
    """按有序装配表产出定稿 section 列表（loader 缺省用 load_workspace_file）。"""
    if loader is None:
        loader = load_workspace_file
    return [
        _finalize_module(name, content, address_term, agent_dn)
        for name, content in _iter_module_sections(order, loader=loader)
    ]


def _compose_skills_segment(skills: list[dict]) -> str:
    """skills → "[已安装的 Skills]" 组成段；无有效技能返回空串。"""
    if not skills:
        return ""
    skill_texts = "\n\n".join(
        f"### Skill: {s['name']}\n{s['content']}" for s in skills if s["content"])
    return "[已安装的 Skills]\n\n" + skill_texts if skill_texts else ""


def _get_hardware_segment() -> str:
    """硬件上下文组成段（运行时动态探测，各装配点共用）。"""
    from config import DATA_DIR
    from core.capability_detector import detect_capabilities
    return detect_capabilities().to_prompt_segment(data_dir=str(DATA_DIR))


def _build_stable_prompt(address_term: str) -> str:
    """构建系统提示「稳定段」：SOUL.md/AGENTS.md/IDENTITY.md/TOOLS.md/skills/硬件信息。

    这些内容不随请求变化，只随 address_term 变化，因此用模块级 dict 缓存。
    缓存通过 workspace 文件 mtime 失效：编辑任意稳定段文件后，下次调用重新构建。

    section 装配统一走 _assemble_module_list / _compose_skills_segment /
    _get_hardware_segment（顺序见 _ORDER_INCREMENTAL_STABLE）。
    """
    global _stable_prompt_cache_mtimes
    with _cache_lock:
        current_mtimes = _get_stable_section_mtimes()
        if _stable_prompt_cache_mtimes is None or current_mtimes != _stable_prompt_cache_mtimes:
            _stable_prompt_cache.clear()
            _stable_prompt_cache_mtimes = current_mtimes

        cache_key = address_term
        if cache_key in _stable_prompt_cache:
            return _stable_prompt_cache[cache_key]

    _agent_dn = _resolve_main_display_name()

    sections = _assemble_module_list(
        _ORDER_INCREMENTAL_STABLE, address_term, _agent_dn,
        loader=load_workspace_file,
    )

    skills_segment = _compose_skills_segment(load_skills())
    if skills_segment:
        sections.append(skills_segment)

    # 硬件上下文（稳定，不随请求变化）
    sections.append(_get_hardware_segment())

    result = "\n\n---\n\n".join(sections)
    with _cache_lock:
        _stable_prompt_cache[cache_key] = result
    return result


def _build_xp_segment(user_id: str | None, address_term: str = "爸爸") -> str:
    """构建 XP 等级 prompt 段落。

    注入到 system prompt 中，让小妲根据用户 XP 等级调整亲密度。
    同时注入用户画像学习器的交互统计和 LLM 认知结果。
    per-user 动态段，不进入稳定段缓存，以保持 KV Cache 命中率。
    任何异常均被吞掉（零质量回退），不影响主流程。
    """
    if not user_id:
        return ""

    try:
        from core.xp_system import XPLevel, get_xp_system
        xp_sys = get_xp_system()
        state = xp_sys.get_state(user_id)
        config = xp_sys.get_intimacy_config(state.level)

        segment = "\n\n[关系亲密度配置]\n"
        segment += f"{address_term}等级：LV{state.level.value} {config.get('label', '')}\n"
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
                segment += f"\n[{address_term}交互统计]\n{stats_summary}\n"
            # LV2+ 注入 LLM 认知结果
            if state.level >= XPLevel.LV2_ACQUAINTANCE:
                insight = learner.get_learned_insight(user_id)
                if insight:
                    segment += f"\n[对{address_term}的认知]\n{insight}\n"
        except (AttributeError, ImportError, TypeError):
            logger.debug("prompt_builder.xp_segment_learner_failed", exc_info=True)

        return segment
    except Exception as e:
        logger.warning("prompt.xp_segment_failed", error=str(e), exc_info=True)
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
            dynamic = _build_dynamic_prompt("", address_term)
            system_prompt = stable + "\n\n---\n\n" + dynamic if dynamic else stable
        except Exception as e:
            # 失败安全：降级到原始构建
            logger.debug("prompt_builder.incremental_fallback error={}", str(e))

    if not system_prompt:
        # 可测性契约:tests 以 monkeypatch.setattr(prompt_builder, ...) 在【门面】上
        # 替换缓存变量与协作函数;拆分后唯一事实命名空间是包门面,此处运行时解析,
        # 与单文件时代"同模块全局可 patch"的行为等价。
        import prompt_builder as _pkg
        from config import DATA_DIR

        now = time.time()
        with _cache_lock:
            current_mtimes = _pkg._get_workspace_mtimes()
            mtime_changed = current_mtimes != _pkg._SYSTEM_PROMPT_CACHE_MTIMES
            addr_changed = address_term != _pkg._SYSTEM_PROMPT_CACHE_ADDR_TERM

            if _pkg._SYSTEM_PROMPT_CACHE and (now - _pkg._SYSTEM_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL and not mtime_changed and not addr_changed:
                system_prompt = _pkg._SYSTEM_PROMPT_CACHE
            else:
                system_prompt = None

        if system_prompt is None:
            sections = _pkg._build_workspace_sections(address_term)
            sections.append(_pkg._build_hardware_context(DATA_DIR))
            system_prompt = "\n\n---\n\n".join(sections)

            with _cache_lock:
                _pkg._SYSTEM_PROMPT_CACHE = system_prompt
                _pkg._SYSTEM_PROMPT_CACHE_TS = now
                _pkg._SYSTEM_PROMPT_CACHE_MTIMES = current_mtimes
                _pkg._SYSTEM_PROMPT_CACHE_ADDR_TERM = address_term
        # extra_context 移到末尾注入
    return system_prompt


def _inject_dynamic_segments(system_prompt: str, user_id: str | None, user_input: str | None, address_term: str = "爸爸") -> str:
    """注入 per-user 动态段落（心理状态、永久记忆、情感记忆）。"""
    if not user_id:
        return system_prompt

    # === 注入新能力段落（per-user 动态段，不进入稳定段缓存） ===
    # 1. L/M/S 心理状态段落
    try:
        from core.mental_state import get_mental_state_manager
        mgr = get_mental_state_manager(user_id=user_id or "")
        mental_segment = mgr.get_prompt_segment()
        if mental_segment:
            system_prompt += "\n\n" + mental_segment
    except Exception as e:
        logger.warning("prompt.mental_state_inject_failed", error=str(e), exc_info=True)

    # 2. 永久记忆段落
    try:
        from core.permanent_memory import get_permanent_memory_manager
        mgr = get_permanent_memory_manager()
        permanent_segment = mgr.get_prompt_segment(user_id)
        if permanent_segment:
            system_prompt += "\n\n" + permanent_segment
    except Exception as e:
        logger.warning("prompt.permanent_memory_inject_failed", error=str(e), exc_info=True)

    # 3. 情感记忆召回段落（需要 user_input）
    if user_input:
        try:
            from core.xp_system import get_xp_system
            from memory.emotional_memory import get_emotional_memory_manager
            xp_sys = get_xp_system()
            xp_state = xp_sys.get_state(user_id)
            em_mgr = get_emotional_memory_manager()
            emotional_segment = em_mgr.recall_and_enact(
                user_id, user_input, xp_state.level.value
            )
            if emotional_segment:
                system_prompt += "\n\n" + emotional_segment
        except Exception as e:
            logger.warning("prompt.emotional_memory_inject_failed", error=str(e), exc_info=True)

    # 4. 学习反馈教训段落（需要 user_input 做相关性匹配）
    #    修复数据黑洞: record_tool_outcome/record_reflection_lesson 有写入,
    #    但 get_relevant_lessons/get_strategy 此前零调用, 教训对推理完全不可见
    if user_input:
        try:
            from core.learning_feedback import get_learning_feedback_loop
            lf_loop = get_learning_feedback_loop()
            relevant_lessons = lf_loop.get_relevant_lessons(user_input, top_k=3)
            if relevant_lessons:
                lesson_lines = ["（以前学到的经验）"]
                for lesson in relevant_lessons:
                    lesson_lines.append(
                        _guard_injected_text(lesson.content[:120])
                    )
                system_prompt += "\n\n" + "\n".join(lesson_lines)
            strategy = lf_loop.get_strategy(user_input)
            if strategy:
                system_prompt += f"\n\n（应对建议）{_guard_injected_text(strategy[:200])}"
        except Exception as e:
            logger.warning("prompt.learning_feedback_inject_failed", error=str(e), exc_info=True)

    # 5. 活跃约束段落（用户纠正的实时行为边界，必须遵守）
    #    修复数据黑洞: get_active_constraints 此前零调用, 约束提取了推理时完全不知道
    try:
        from core.learning_loop import get_learning_loop
        _loop = get_learning_loop()
        constraints = _loop.get_active_constraints()
        if constraints:
            constraint_lines = [f"[{address_term}明确的行为约束（必须遵守）]"]
            for c in constraints:
                constraint_lines.append(f"· {_guard_injected_text(c)}")
            system_prompt += "\n\n" + "\n".join(constraint_lines)
    except Exception as e:
        logger.warning("prompt.learning_loop_inject_failed", error=str(e), exc_info=True)

    return system_prompt


def _inject_xp_and_extra(system_prompt: str, user_id: str | None, extra_context: str, address_term: str = "爸爸") -> str:
    """注入 XP 等级段落和 extra_context。"""
    # 4. XP 等级段落（已有，per-user 不进缓存以保持稳定段 KV Cache 命中率）
    xp_segment = _build_xp_segment(user_id, address_term)
    if xp_segment:
        system_prompt += xp_segment

    # 5. extra_context（末尾注入，保证顺序: base → mental → permanent → emotional → XP → extra_context）
    if extra_context:
        system_prompt += "\n\n---\n\n" + extra_context

    # 6. 系统能力声明 — 让 Agent 了解自己能操控什么
    _cap = (
        f"\n\n---\n\n## 系统能力\n\n"
        f"你可以操控以下系统功能：\n"
        f"- **定时提醒**：当{address_term}要求提醒或设定时任务时，系统会自动在定时调度页面创建提醒条目，"
        f"{address_term}可在 Web UI「定时问候」页面查看、编辑或删除。支持每天/按周几/一次性触发。\n"
        f"- **笔记/洞察**：你对{address_term}的新发现（性格、习惯、偏好）会自动记录为笔记。\n"
        f"- **斜杠命令**：/note 查看笔记，/status 查看系统状态，/help 查看所有命令。\n"
        f"- **定时问候**：系统每天会按计划发送问候消息，{address_term}可在定时问候页面管理。\n"
    )
    system_prompt += _cap
    return system_prompt


def build_system_prompt(extra_context: str = "", address_term: str = "爸爸",
                         user_id: str | None = None,
                         user_input: str | None = None,
                         context: dict | None = None) -> str:
    """构建系统提示词，组合稳定段缓存、动态段与额外上下文。

    Args:
        extra_context: 额外上下文文本（末尾注入）
        address_term: 称呼词
        user_id: 用户 ID（用于 per-user 动态段）
        user_input: 用户输入（用于情感记忆召回等相关性匹配）
        context: J-Space 方向上下文（可选，用于 Hook #8 方向干预 prompt）
    """
    # P6: 增量上下文构建路径 —— 稳定段缓存 + 动态段每次构建
    # extra_context 延迟到末尾注入，保证新段落顺序:
    # base → mental → permanent → emotional → XP → extra_context
    system_prompt = _build_cached_system_prompt(address_term)
    system_prompt = _inject_dynamic_segments(system_prompt, user_id, user_input, address_term)
    system_prompt = _inject_xp_and_extra(system_prompt, user_id, extra_context, address_term)

    # J-Space Hook: 方向干预 prompt
    try:
        from config import ENABLE_J_SPACE_HOOKS
        if ENABLE_J_SPACE_HOOKS:
            prompt_modifier = context.get("prompt_modifier", 0.0) if context else 0.0
            if prompt_modifier > 0:
                # 应用 prompt 方向权重：附加行为倾向提示（消费端闭环）
                system_prompt += (
                    f"\n\n[J-Space 方向] 当前行为方向权重：{prompt_modifier:.2f}。"
                    "请在回复中适度体现此方向倾向。"
                )
    except Exception as e:
        logger.debug("prompt_builder.j_space_direction_hook_failed", error=str(e), exc_info=True)
    from config import apply_agent_name_replacements
    _final_prompt = apply_agent_name_replacements(system_prompt)
    # 技能系统诊断：记录系统提示词 token 数（与 to_openai_tools 的 tools_tokens 配对，
    # 两者之和即发给 LLM 的固定开销，技能系统的渐进式披露可压缩此开销）
    _cn = sum(1 for c in _final_prompt if '\u4e00' <= c <= '\u9fff')
    _en = len(_final_prompt) - _cn
    _prompt_tokens = int(_cn * 1.5 + _en * 0.25)
    logger.info("prompt_builder.system_prompt_tokens",
                tokens_est=_prompt_tokens, length=len(_final_prompt))
    return _inject_canary(_final_prompt)


def _build_workspace_sections(address_term: str) -> list[str]:
    """加载 workspace 配置文件并组装 sections 列表（不含硬件信息段）。

    section 装配统一走 _assemble_module_list（顺序见 _ORDER_LEGACY_FALLBACK，
    与主路径的 IDENTITY 前置分层顺序不同 —— 既有产品行为，保留差异）；
    表情包指令块仅存在于本遗留路径：主路径的表情包由 main_path 运行时
    预选机制负责（_prepare_sticker_and_tools 注入上下文 system message +
    ensure_emotion_tag 自动补情绪标签），不依赖静态指令块。
    """
    _agent_dn = _resolve_main_display_name()

    sections = _assemble_module_list(_ORDER_LEGACY_FALLBACK, address_term, _agent_dn)

    skills_segment = _compose_skills_segment(load_skills())
    if skills_segment:
        sections.append(skills_segment)

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
def build_safe_system_prompt(extra_context: str = "", address_term: str = "你") -> str:
    """为非主人用户构建安全化的 system prompt。

    剥离所有个人隐私信息（USER.md、MEMORY.md、IDENTITY.md 中的敏感内容），
    仅保留基本人格和行为规则，防止通过 prompt injection 泄露隐私。
    """
    global _SAFE_PROMPT_CACHE, _SAFE_PROMPT_CACHE_TS, _SAFE_PROMPT_CACHE_NAME, _SAFE_PROMPT_CACHE_ADDR

    from config import get_agent_display_name
    xiaoda_name = get_agent_display_name('xiaoda')

    now = time.time()
    with _cache_lock:
        cache_hit = (_SAFE_PROMPT_CACHE
                and (now - _SAFE_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL
                and xiaoda_name == _SAFE_PROMPT_CACHE_NAME
                and address_term == _SAFE_PROMPT_CACHE_ADDR)
        safe_prompt = _SAFE_PROMPT_CACHE if cache_hit else None

    if safe_prompt is None:
        sections = []

        soul = load_workspace_file("SOUL.md")
        if soul:
            safe_soul = _strip_owner_references(soul)
            # 先替换占位符，再替换 display_name，最后替换"爸爸"→"你"
            safe_soul = _replace_placeholders(safe_soul, address_term, xiaoda_name)
            from config import apply_agent_name_replacements
            safe_soul = apply_agent_name_replacements(safe_soul)
            safe_soul = safe_soul.replace("爸爸", "你")
            safe_soul = safe_soul.replace("称呼用户为\"你\"", "称呼用户为\"你\"")
            sections.append(safe_soul)

        sections.append(
            "# 身份\n\n"
            f"你是{xiaoda_name}，一个温柔聪慧的 AI 助手。\n\n"
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
        with _cache_lock:
            _SAFE_PROMPT_CACHE = safe_prompt
            _SAFE_PROMPT_CACHE_TS = now
            _SAFE_PROMPT_CACHE_NAME = xiaoda_name
            _SAFE_PROMPT_CACHE_ADDR = address_term

    if extra_context:
        safe_prompt += f"\n\n---\n\n{extra_context}"

    return _inject_canary(safe_prompt)


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
            if line.startswith(("## ", "### ", "# ")):
                skip_block = False
            else:
                continue
        filtered.append(line)

    return "\n".join(filtered)
