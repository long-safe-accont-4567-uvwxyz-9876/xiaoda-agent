import logging
import os
import re
import json
import sys

logger = logging.getLogger(__name__)
from typing import Any
import shutil
from pathlib import Path


# ── Phase 1 拆分：路径与 workspace 引导块抽为 config_paths（逐字节搬移）──
# 同名 re-export 保持兼容：from config import DATA_DIR / get_config_dir 等
# 既有用法不受影响（契约见 tests/test_config_paths_module.py）。
from config_paths import (  # noqa: F401,E402
    get_base_dir, get_env_path, get_credentials_dir, get_config_dir,
    is_data_dir_writable, _ensure_workspace, _init_user_resources,
    _migrate_old_data, _merge_dir, _resolve_data_path, _ensure_fallback,
    _get_fallback_base, _init_agent_json5, _init_agents_subdir,
    _init_workspace_templates, _workspace_initialized,
    ENV_PATH, _KIOXIA_BASE, _FALLBACK_BASE, _KIOXIA_AVAILABLE,
    DATA_DIR, LOG_DIR, WORKSPACE_DIR, CREDENTIALS_DIR,
    CONFIG_DIR, AGENT_CONFIG_PATH, STICKER_DIR, XIAOLI_STICKER_DIR,
    AGENT_STICKER_BASE, FILE_DIR, MEDIA_DIR, VOICE_REF_DIR,
    MEMORY_STATE_DIR, PLUGINS_CONFIG_DIR, AGENTS_CONFIG_DIR,
)

# ── Phase 2 拆分：provider 目录块抽为 config_providers（逐字节搬移）──
# 同名 re-export 保持兼容（契约见 tests/test_config_providers_module.py）。
# 可变状态 DEFAULT_PROVIDER/set_default_provider 保留在本模块（见 config_providers docstring）。
from config_providers import (  # noqa: F401,E402
    get_provider_catalog, get_default_model_for_provider,
    get_base_url_for_provider, get_default_provider, get_builtin_providers,
    get_provider_config,
    MIMO_MODEL, MIMO_BASE_URL, DEEPSEEK_BASE_URL,
)

# ── Phase 3 拆分：agent 命名/display_name 块抽为 config_agents（逐字节搬移）──
# 同名 re-export 保持兼容（契约见 tests/test_config_agents_module.py）。
from config_agents import (  # noqa: F401,E402
    _DEFAULT_DISPLAY_NAMES,
    _FALLBACK_DEPRECATED_NAMES,
    _best_display_name,
    _deprecated_names_cache,
    _display_name_cache,
    agent_names,
    apply_agent_name_replacements,
    clear_display_name_cache,
    get_agent_deprecated_names,
    get_agent_display_name,
    get_all_deprecated_names,
    reverse_agent_name_replacements,
)

# ── Phase 4 拆分：env 开关/常量表抽为 config_constants（逐字节搬移）──
# 同名 re-export 保持兼容（契约见 tests/test_config_constants_module.py）。
from config_constants import (  # noqa: F401,E402
    get_secret, _safe_positive_float, _safe_float,
    TRUST_FORWARDED_FOR, DEEPSEEK_API_KEY, MIMO_API_KEY,
    AGNES_API_KEY, AGNES_BASE_URL, AGNES_TEXT_MODEL, AGNES_IMAGE_MODEL, AGNES_VIDEO_MODEL,
    ASR_API_KEY, ASR_BASE_URL, ASR_MODEL, JINA_API_KEY,
    AGENT_ROUTE_KEYWORDS, AGENT_TASK_MAP,
    RERANKER_API_KEY, RERANKER_BASE_URL, RERANKER_MODEL, RERANKER_ENABLED,
    RERANKER_OVERSAMPLE_RATIO,
    QUERY_TRANSFORM_ENABLED, QUERY_EXPAND_COUNT, MEMORY_RETRIEVAL_DIFFUSION, HYDE_ENABLED,
    INTENT_LLM_CLASSIFY, INTENT_CLASSIFY_TIMEOUT,
    RETRIEVAL_SMART_SKIP, RETRIEVAL_PARALLEL_TRANSFORM, RETRIEVAL_PARALLEL_SEARCH,
    QUERY_CACHE_ENABLED, QUERY_CACHE_THRESHOLD, QUERY_CACHE_MAX_SIZE, QUERY_CACHE_TTL,
    MEMORY_RETRIEVE_TIMEOUT,
    PARENT_CHILD_CHUNK_ENABLED, KG_V2_ENABLED, CONTEXTUAL_RETRIEVAL_ENABLED,
    CHILD_CHUNK_OVERLAP_CHARS, CHILD_CHUNK_MAX_PER_PARENT, CHILD_CHUNK_SEGMENT_MAX_LEN,
    CHILD_VEC_TABLE, CHILD_CHUNK_TYPES,
    SUB_AGENT_API_TIMEOUT, SUB_AGENT_TOTAL_TIMEOUT, SUB_AGENT_API_RETRY,
    TTS_ASYNC_MODE, STREAM_STATUS_PUSH, SIMPLE_CHAT_FASTPATH, STREAM_TEXT_PUSH,
    STREAM_TOOL_STATUS,
    CIRCUIT_BREAKER_COOLDOWN, CIRCUIT_BREAKER_HALF_OPEN_PROBES, CIRCUIT_BREAKER_MAX_COOLDOWN,
    ERROR_RULE_STRICT_MODE, PROMPT_CACHING_ENABLED,
    RAG_RERANK_WEIGHT, RAG_KG_WEIGHT, RAG_IMPORTANCE_WEIGHT,
    RAG_RECALL_LIMIT, RAG_RERANK_LIMIT, RAG_MIN_FINAL_SCORE, RAG_VEC_MAX_DISTANCE,
    RAG_VEC_SOFT_PENALTY, RAG_RRF_RANK_PENALTY, FTS_DROP_CJK_SINGLE, FTS_CJK_STOP_WORDS_FILTER,
    EMOTION_TRIGGER_THRESHOLD, SCENE_STICKINESS_THRESHOLD,
    MEMORY_COLD_MAX, MEMORY_WARM_MAX, MEMORY_WARM_VEC_WEIGHT,
    MAX_EPISODIC_MEMORIES, MEMORY_DISTILL_BATCH, MEMORY_DISTILL_ENABLED, MAX_EPISODIC_ROWS,
    ENABLE_J_SPACE_HOOKS, ENABLE_EMOTION_LLM, DIRECTION_REGISTRY_PATH,
    SIGNAL_STREAM_MAX_HISTORY, INTERVENTION_DEFAULT_COOLDOWN,
)

# ── 默认 Provider ──
# 初始值：环境变量 DEFAULT_PROVIDER > provider_metadata.json 的 default_provider（默认 mimo）
# 运行时可通过 set_default_provider() 动态更新（Web UI 切换模型时调用）
DEFAULT_PROVIDER = get_default_provider()


def set_default_provider(provider: str) -> None:
    """运行时更新 DEFAULT_PROVIDER（Web UI 切换模型时调用）。

    同时更新模块级变量 DEFAULT_PROVIDER，使所有 import 了该变量的模块
    在下次读取时获得最新值。
    """
    global DEFAULT_PROVIDER
    DEFAULT_PROVIDER = provider.strip().lower()

# ── Provider → 默认模型映射 ──
# 当 MODEL_NAME 未在 .env 中显式设置时，根据 DEFAULT_PROVIDER 从
# provider_metadata.json 动态读取默认模型（不再硬编码在代码里）
if os.getenv("MODEL_NAME"):
    MODEL_NAME = os.getenv("MODEL_NAME")
else:
    MODEL_NAME = get_default_model_for_provider(DEFAULT_PROVIDER)
PRO_MODEL_NAME = os.getenv("PRO_MODEL_NAME", "")
FLASH_MODEL_NAME = os.getenv("FLASH_MODEL_NAME", "")


def get_temperature(default: float = 0.7) -> float:
    """读取全局 temperature：优先 webui_overrides，回退 default。"""
    try:
        from web.config_service import get_config_service
        override = get_config_service().get("models.temperature")
        if override is not None:
            return float(override)
    except Exception:
        logger.debug("get_global_temperature.webui_read_failed", exc_info=True)
    return default


def get_frequency_penalty(default: float = 1.0) -> float:
    """读取全局 frequency_penalty：优先 webui_overrides，回退 default。"""
    try:
        from web.config_service import get_config_service
        override = get_config_service().get("models.frequency_penalty")
        if override is not None:
            return float(override)
    except Exception:
        logger.debug("get_frequency_penalty.webui_read_failed", exc_info=True)
    return default


def get_presence_penalty(default: float = 1.0) -> float:
    """读取全局 presence_penalty：优先 webui_overrides，回退 default。"""
    try:
        from web.config_service import get_config_service
        override = get_config_service().get("models.presence_penalty")
        if override is not None:
            return float(override)
    except Exception:
        logger.debug("get_presence_penalty.webui_read_failed", exc_info=True)
    return default


def get_reply_dedup_enabled(default: bool = True) -> bool:
    """读取跨对话回复去重开关：优先 webui_overrides，回退 default（默认开启）。"""
    try:
        from web.config_service import get_config_service
        override = get_config_service().get("models.reply_dedup_enabled")
        if override is not None:
            return bool(override)
    except Exception:
        logger.debug("get_reply_dedup_enabled.webui_read_failed", exc_info=True)
    return default


def _strip_json5_comments(text: str) -> str:
    result = []
    in_string = False
    in_block_comment = False
    i = 0
    while i < len(text):
        if in_block_comment:
            if text[i:i+2] == '*/':
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            result.append(text[i])
            if text[i] == '\\' and i + 1 < len(text):
                result.append(text[i+1])
                i += 2
                continue
            if text[i] == '"':
                in_string = False
            i += 1
            continue
        if text[i:i+2] == '/*':
            in_block_comment = True
            i += 2
            continue
        if text[i:i+2] == '//':
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        if text[i] == '"':
            in_string = True
            result.append(text[i])
            i += 1
            continue
        result.append(text[i])
        i += 1
    cleaned = ''.join(result)
    return re.sub(r',\s*([}\]])', r'\1', cleaned)


def load_agent_config() -> dict:
    """加载并解析 agent 配置文件（JSON5 风格，自动去除注释）。"""
    if not AGENT_CONFIG_PATH.exists():
        return {}
    raw = AGENT_CONFIG_PATH.read_text(encoding="utf-8")
    cleaned = _strip_json5_comments(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("config.agent_json5_parse_failed", error=str(e))
        return {}


# ── 系统提示词构建相关函数已拆分到 prompt_builder.py ──────────
# 为保持向后兼容，文件末尾通过 `from prompt_builder import *` 重新导出：
#   build_system_prompt / build_safe_system_prompt / load_workspace_file
#   load_skills / _ensure_workspace_template 等


AGENT_CONFIG = load_agent_config()

# MCP_SERVERS：使用 shutil.which() 动态解析命令路径，兼容 Windows/Linux/macOS
# 不再硬编码 Orange Pi 上的绝对路径，避免在其他设备上失效


def _resolve_command(name: str) -> str:
    """解析命令完整路径，兼容 systemd 等受限 PATH 环境。"""
    path = shutil.which(name)
    if path:
        return path
    # shutil.which 在 systemd 等环境中可能找不到 ~/.local/bin 下的命令
    # 检查常见安装路径
    for candidate in [
        Path.home() / ".local" / "bin" / name,
        Path("/usr/local/bin") / name,
        Path.home() / ".cargo" / "bin" / name,
    ]:
        if candidate.exists():
            return str(candidate)
    # Windows: 检查 npm 全局目录
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA", "")
        if appdata:
            npm_path = Path(appdata) / "npm" / f"{name}.cmd"
            if npm_path.exists():
                return str(npm_path)
    return name  # fallback: 返回命令名本身


MCP_SERVERS = {
    "git": {
        "command": _resolve_command("uvx"),
        # 根因修复：uvx 默认解析到最新 mcp 版本，但 mcp 2.0.0 移除了 Server.list_tools() 装饰器 API，
        # 导致 mcp-server-git 2026.7.10 子进程在 initialize 前瞬崩（AttributeError）。
        # 用 --with "mcp<2" 钉版本到 1.x，已实测 8s 内完成握手并返回 12 个 git 工具。
        "args": ["--with", "mcp<2", "mcp-server-git", "--repository", str(Path.home() / "Desktop")],
        "env": {"UV_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple"},
        "agents": ["xiaolang"],  # which agents can use this MCP server's tools
    },
    "github": {
        "command": _resolve_command("npx"),
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": get_secret("GITHUB_PERSONAL_ACCESS_TOKEN", "")},
        "agents": ["xiaolang"],
    },
}

__all__ = [
    "AGENTS_CONFIG_DIR",
    "AGENT_CONFIG",
    "AGENT_CONFIG_PATH",
    "CONFIG_DIR",
    "AGENT_ROUTE_KEYWORDS",
    "AGENT_STICKER_BASE",
    "AGENT_TASK_MAP",
    "AGNES_API_KEY",
    "AGNES_BASE_URL",
    "AGNES_IMAGE_MODEL",
    "AGNES_TEXT_MODEL",
    "AGNES_VIDEO_MODEL",
    "ASR_API_KEY",
    "ASR_BASE_URL",
    "ASR_MODEL",
    "CIRCUIT_BREAKER_COOLDOWN",
    "CIRCUIT_BREAKER_HALF_OPEN_PROBES",
    "CIRCUIT_BREAKER_MAX_COOLDOWN",
    "CREDENTIALS_DIR",
    "DATA_DIR",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "ERROR_RULE_STRICT_MODE",
    "FILE_DIR",
    "JINA_API_KEY",
    "LOG_DIR",
    "MAX_EPISODIC_MEMORIES",
    "MCP_SERVERS",
    "MEDIA_DIR",
    "MEMORY_COLD_MAX",
    "MEMORY_DISTILL_BATCH",
    "MEMORY_DISTILL_ENABLED",
    "MEMORY_RETRIEVAL_DIFFUSION",
    "HYDE_ENABLED",
    "MEMORY_STATE_DIR",
    "MEMORY_WARM_MAX",
    "MEMORY_WARM_VEC_WEIGHT",
    "MODEL_NAME",
    "PLUGINS_CONFIG_DIR",
    "PROMPT_CACHING_ENABLED",
    "QUERY_EXPAND_COUNT",
    "QUERY_TRANSFORM_ENABLED",
    "INTENT_LLM_CLASSIFY",
    "INTENT_CLASSIFY_TIMEOUT",
    "RAG_IMPORTANCE_WEIGHT",
    "RAG_KG_WEIGHT",
    "RAG_RECALL_LIMIT",
    "RAG_RERANK_LIMIT",
    "RAG_RERANK_WEIGHT",
    "RERANKER_API_KEY",
    "RERANKER_BASE_URL",
    "RERANKER_ENABLED",
    "RERANKER_MODEL",
    "RERANKER_OVERSAMPLE_RATIO",
    "RETRIEVAL_PARALLEL_SEARCH",
    "RETRIEVAL_PARALLEL_TRANSFORM",
    "RETRIEVAL_SMART_SKIP",
    "QUERY_CACHE_ENABLED",
    "QUERY_CACHE_THRESHOLD",
    "QUERY_CACHE_MAX_SIZE",
    "QUERY_CACHE_TTL",
    "SIMPLE_CHAT_FASTPATH",
    "STICKER_DIR",
    "STREAM_STATUS_PUSH",
    "STREAM_TEXT_PUSH",
    "STREAM_TOOL_STATUS",
    "SUB_AGENT_API_RETRY",
    "SUB_AGENT_API_TIMEOUT",
    "SUB_AGENT_TOTAL_TIMEOUT",
    "TTS_ASYNC_MODE",
    "VOICE_REF_DIR",
    "WORKSPACE_DIR",
    "XIAOLI_STICKER_DIR",
    "agent_names",
    "get_agent_display_name",
    "get_base_dir",
    "get_config_dir",
    "get_credentials_dir",
    "load_agent_config",
]

# ── 向后兼容：从 prompt_builder 重新导出已拆分的函数 ──────────
# 使用 PEP 562 模块级 __getattr__ 延迟导入, 彻底打破 config <-> prompt_builder 循环.
# 此前 `from prompt_builder import *` 是顶层 import, 会立即触发 prompt_builder 加载,
# 而 prompt_builder 在调用时 (函数内) 又会回头读 config 常量 — 虽然 prompt_builder
# 已用函数内延迟导入规避了运行时崩溃, 但顶层 `from prompt_builder import *` 仍会在
# 静态分析层面形成循环. 改为 __getattr__ 后, 只有实际访问这些名称时才触发导入.
_PROMPT_BUILDER_REEXPORTS = frozenset({
    "build_system_prompt",
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
})


def __getattr__(name: str) -> Any:
    """模块级 __getattr__ — 从 prompt_builder 延迟导入, 避免循环导入.

    只有访问 _PROMPT_BUILDER_REEXPORTS 中的名称时才触发 prompt_builder 加载.
    首次访问后将结果缓存到 globals(), 后续直接命中, 无 import 开销.
    """
    if name in _PROMPT_BUILDER_REEXPORTS:
        from importlib import import_module
        _pb = import_module("prompt_builder")
        value = getattr(_pb, name)
        globals()[name] = value  # 缓存, 下次直接访问
        return value
    raise AttributeError(f"module 'config' has no attribute {name!r}")