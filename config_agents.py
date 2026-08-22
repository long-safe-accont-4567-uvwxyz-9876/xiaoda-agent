"""config.py 的 agent 命名/display_name 块 — 自 config.py 拆分（上帝文件 Phase 3）。

内容：默认显示名回退表 _DEFAULT_DISPLAY_NAMES、display_name 带文件 mtime
缓存的读取与清除（get_agent_display_name / _best_display_name /
clear_display_name_cache）、agent key 目录扫描（agent_names）、旧名映射
（get_agent_deprecated_names / get_all_deprecated_names，含硬编码兜底
_FALLBACK_DEPRECATED_NAMES）与人格文件全局名称替换/还原
（apply_agent_name_replacements / reverse_agent_name_replacements）。
函数体自 config.py 逐字节搬移。

兼容契约（tests/test_config_agents_module.py）：
    - 本模块不得 import config（防循环依赖；仅依赖 config_paths）
    - config 同名 re-export，from config import get_agent_display_name /
      agent_names / apply_agent_name_replacements 等既有用法不受影响

JSON5 注释剥离器（_strip_json5_comments / load_agent_config）留在 config.py：
本块不依赖它们，且 config.py 的 AGENT_CONFIG 加载仍在使用。
"""
from __future__ import annotations

import logging
from pathlib import Path

from config_paths import AGENTS_CONFIG_DIR

logger = logging.getLogger(__name__)


# ── Agent display_name 动态读取（规避 IP 风险，用户可自定义）──
# 默认 display_name（当用户未自定义时的 fallback）
_DEFAULT_DISPLAY_NAMES: dict[str, str] = {
    "xiaoda": "小妲",
    "xiaoli": "小莉",
    "xiaolang": "小狼",
    "xiaolian": "小涟",
    "xiaoke": "小可",
}
_display_name_cache: dict[str, tuple[float, str]] = {}  # {name: (mtime, display_name)}


def clear_display_name_cache(name: str | None = None):
    """清除显示名缓存。

    当 display_name 变更时调用，确保下次读取时获取最新值。
    Args:
        name: 指定 agent 名称清除，None 则清除全部
    """
    if name:
        _display_name_cache.pop(name, None)
    else:
        _display_name_cache.clear()
    # 同时清除 prompt_builder 的模块缓存
    try:
        from prompt_builder import clear_module_cache
        clear_module_cache()
    except ImportError:
        logger.debug("config.prompt_builder_import_unavailable", exc_info=True)


def agent_names() -> list[str]:
    """返回所有 agent key（通过扫描 config/agents/ 目录）。

    AGENTS_CONFIG_DIR 可能指向外置存储（KIOXIA_DATA_DIR），若该目录为空
    （用户未在外置存储放置 agent 配置），回退到源码 config/agents/ 目录。
    agent 配置文件是源码资源，应始终能被找到，避免 display name / CLI 列表
    在外置存储未初始化时全部失效。
    """
    names = [
        fp.stem for fp in AGENTS_CONFIG_DIR.glob("*.json")
        if fp.stem and not fp.stem.startswith("_")
    ]
    if names:
        return names
    # 外置存储为空时回退到源码目录（agent 配置是源码资源，非用户数据）
    _src_agents_dir = Path(__file__).resolve().parent / "config" / "agents"
    return [
        fp.stem for fp in _src_agents_dir.glob("*.json")
        if fp.stem and not fp.stem.startswith("_")
    ]


def get_agent_display_name(name: str) -> str:
    """读取 agent 的 display_name（从 config/agents/{name}.json）。

    用于规避 IP 风险：发布版可改默认值为中性名，用户拿到后改回原名即可全局生效。
    带文件 mtime 缓存，避免频繁 IO。
    """
    if not name:
        return ""
    fp = AGENTS_CONFIG_DIR / f"{name}.json"
    default = _DEFAULT_DISPLAY_NAMES.get(name, name)
    try:
        mtime = fp.stat().st_mtime
    except OSError:
        return default
    cached = _display_name_cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        import json
        data = json.loads(fp.read_text(encoding="utf-8"))
        dn = data.get("display_name") or default
    except (ValueError, KeyError, ImportError):
        dn = default
    except Exception:
        logger.exception(".config_agents.get_agent_display_name_unexpected")
        dn = default
    _display_name_cache[name] = (mtime, dn)
    return dn


def _best_display_name(agent_key: str) -> str:
    """返回 agent 的显示名；未配置时回退为 agent key。"""
    return get_agent_display_name(agent_key) or agent_key


# ── Agent 原名 → display_name 全局替换 ────────────────────────
# 每个 agent 的人格文件中使用原名，运行时自动替换为用户配置的显示名。
# 全局统一机制：所有 agent 共用一套替换逻辑，不分主次。
# 旧名映射从 config/agents/*.json 的 deprecated_names 字段读取，无需手动维护。

# 硬编码兜底（当配置文件缺失或无 deprecated_names 时使用）
_FALLBACK_DEPRECATED_NAMES: dict[str, str] = {
    "纳西妲": "xiaoda", "nahida": "xiaoda",
    "可莉": "xiaoli", "keli": "xiaoli",
    "银狼": "xiaolang", "yinlang": "xiaolang",
    "昔涟": "xiaolian", "xilian": "xiaolian",
    "尼可": "xiaoke", "nike": "xiaoke",
}

# 缓存: {agent_key: (mtime, deprecated_names_list)}
_deprecated_names_cache: dict[str, tuple[float, list[str]]] = {}


def get_agent_deprecated_names(agent_key: str) -> list[str]:
    """读取 agent 的旧名列表（从 config/agents/{name}.json 的 deprecated_names 字段）。"""
    fp = AGENTS_CONFIG_DIR / f"{agent_key}.json"
    try:
        mtime = fp.stat().st_mtime
    except OSError:
        return [k for k, v in _FALLBACK_DEPRECATED_NAMES.items() if v == agent_key]
    cached = _deprecated_names_cache.get(agent_key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        import json
        data = json.loads(fp.read_text(encoding="utf-8"))
        names = data.get("deprecated_names", [])
    except (ValueError, KeyError, ImportError):
        names = []
    except Exception:
        logger.exception(".config_agents.get_agent_deprecated_names_unexpected")
        names = []
    if not names:
        names = [k for k, v in _FALLBACK_DEPRECATED_NAMES.items() if v == agent_key]
    _deprecated_names_cache[agent_key] = (mtime, names)
    return names


def get_all_deprecated_names() -> dict[str, str]:
    """返回所有旧名 → agent_key 的映射（从配置文件自动生成）。"""
    result: dict[str, str] = {}
    for key in agent_names():
        for old_name in get_agent_deprecated_names(key):
            result[old_name] = key
    return result


def apply_agent_name_replacements(content: str) -> str:
    """将人格文件中所有 agent 原名替换为 config 中的显示名。

    替换来源（优先级从高到低）：
    1. 配置文件 deprecated_names 字段（旧名）
    2. 当前 display_name（新名）
    3. agent key（如 xiaoda）
    按原名长度降序替换，避免短名破坏长名。
    """
    # 1. 替换旧名（从配置文件读取）
    for old_name, agent_key in sorted(
        get_all_deprecated_names().items(), key=lambda x: -len(x[0])
    ):
        dn = _best_display_name(agent_key)
        if dn and dn != old_name:
            content = content.replace(old_name, dn)
    # 2. 替换当前 display_name（如用户改了显示名，旧人格文件中的新名也要同步）
    for agent_key in agent_names():
        dn = _best_display_name(agent_key)
        if dn and dn != agent_key:
            content = content.replace(agent_key, dn)
    return content


def reverse_agent_name_replacements(content: str) -> str:
    """将 display_name 还原为 agent key（用于编辑器保存时还原模板）。

    与 apply_agent_name_replacements 互为逆操作。
    只做 display_name → agent key 这一层还原，不涉及旧名（如"纳西妲"）。
    """
    for agent_key in agent_names():
        dn = _best_display_name(agent_key)
        if dn and dn != agent_key:
            content = content.replace(dn, agent_key)
    return content
