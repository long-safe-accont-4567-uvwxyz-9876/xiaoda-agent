"""系统提示词构建包（门面）。

自单文件 prompt_builder.py（1669 行）拆分而来（2026-08-25 技术债专项 P2，
docs/giant_files_split_plan_2026-08-22.md）。逐字节搬移，零逻辑改动。

子模块分工：
  _prompt_common     共享原语：注入护栏/canary/缓存锁
  _prompt_scene      场景识别 + 分桶排序 + 分层 LRU（build_scene_aware_prompt）
  _prompt_workspace  模块 mtime 缓存/模板初始化/技能加载（load_skills 等）
  _prompt_assembly   稳定段装配引擎 + build_system_prompt / build_safe_system_prompt

兼容契约（tests/test_prompt_builder_package.py 守护）：
  1. 本门面 re-export 全部历史顶层名称（含带类型标注的模块级常量），
     from prompt_builder import X 面不破；
  2. config.py 的 PEP 562 懒转发（config.__getattr__ → 本包）不受影响；
  3. clear_module_cache() 仍一次性清空全链路缓存。
"""

from prompt_builder._prompt_assembly import (  # noqa: F401
    _MD_MODULES,
    _ORDER_INCREMENTAL_DYNAMIC,
    _ORDER_INCREMENTAL_STABLE,
    _ORDER_LEGACY_FALLBACK,
    _SAFE_PROMPT_CACHE,
    _SAFE_PROMPT_CACHE_ADDR,
    _SAFE_PROMPT_CACHE_NAME,
    _SAFE_PROMPT_CACHE_TS,
    _STICKER_INSTRUCTIONS,
    _SYSTEM_PROMPT_CACHE,
    _SYSTEM_PROMPT_CACHE_ADDR_TERM,
    _SYSTEM_PROMPT_CACHE_MTIMES,
    _SYSTEM_PROMPT_CACHE_TS,
    _SYSTEM_PROMPT_CACHE_TTL,
    _annotate_user_profile,
    _assemble_module_list,
    _build_cached_system_prompt,
    _build_hardware_context,
    _build_stable_prompt,
    _build_workspace_sections,
    _build_xp_segment,
    _compose_skills_segment,
    _finalize_module,
    _get_hardware_segment,
    _get_stable_section_mtimes,
    _inject_dynamic_segments,
    _inject_xp_and_extra,
    _iter_module_sections,
    _normalize_module,
    _replace_placeholders,
    _resolve_main_display_name,
    _stable_prompt_cache,
    _stable_prompt_cache_mtimes,
    _strip_owner_references,
    build_safe_system_prompt,
    build_system_prompt,
)
from prompt_builder._prompt_common import (  # noqa: F401
    _cache_lock,
    _canary_manager,
    _guard_injected_text,
    _inject_canary,
)
from prompt_builder._prompt_scene import (  # noqa: F401
    _BASE_STICKINESS_THRESHOLD,
    _BUCKET_LRU_LEVEL,
    _BUCKET_LRU_QUOTA,
    _BUCKET_ORDERINGS,
    _COLLOQUIAL_MAP,
    _MODULE_SCENE_PRIORITY,
    _NEGATION_PREFIXES,
    _SCENE_BUCKET,
    _SCENE_CACHE_MAX_SIZE,
    _SCENE_CONFIDENCE_THRESHOLD,
    _SCENE_KEYWORDS,
    _SCENE_LEVEL,
    _SCENE_PATTERNS,
    _STABLE_PREFIX_ORDER,
    _build_dynamic_prompt,
    _build_scene_middle,
    _classify_scene,
    _classify_scene_blended,
    _compute_scene_signature,
    _compute_scene_signature_with_stickiness,
    _dynamic_stickiness_threshold,
    _get_bucket_for_sig,
    _get_identity_primary_keywords,
    _get_scene_level,
    _has_negation_before,
    _inject_instruction_hierarchy,
    _layered_lru_evict,
    _normalize_colloquial,
    build_scene_aware_prompt,
    get_scene_cache_stats,
    reset_scene_cache,
)
from prompt_builder._prompt_workspace import (  # noqa: F401
    _detect_device_info,
    _ensure_workspace_template,
    _get_template_dir,
    _get_workspace_mtimes,
    _load_cached_modules,
    _module_cache,
    _module_cache_mtimes,
    load_skills,
    load_workspace_file,
)

# ── 场景缓存可变状态（单一事实源）────────────────────────────
# tests 直接 patch/读门面命名空间(test_harness_verification 等),
# 生产代码(_prompt_scene)经 _pkg 前缀访问同一绑定,防双命名空间分裂。
_scene_prompt_cache: dict[tuple, str] = {}

_current_scene_sig: tuple = ()
_scene_cache_hits: int = 0
_scene_cache_misses: int = 0

# ── 跨区缓存失效入口（原文件头部搬移）───────────────────────────
# _module_cache* 在 workspace、_scene_prompt_cache 在 scene——本函数横跨两区,
# 故定义于门面而非任一子模块。

def clear_module_cache():
    """清除模块缓存。

    当 display_name 变更时调用，确保下次构建 prompt 时获取最新内容。
    """
    with _cache_lock:
        _module_cache.clear()
        global _module_cache_mtimes
        _module_cache_mtimes = None
        _scene_prompt_cache.clear()


__all__ = [
    "_build_dynamic_prompt",
    "_build_stable_prompt",
    "_build_xp_segment",
    "_canary_manager",
    "_classify_scene",
    "_detect_device_info",
    "_ensure_workspace_template",
    "_get_workspace_mtimes",
    "_strip_owner_references",
    "build_safe_system_prompt",
    "build_scene_aware_prompt",
    "build_system_prompt",
    "load_skills",
    "load_workspace_file",
]
