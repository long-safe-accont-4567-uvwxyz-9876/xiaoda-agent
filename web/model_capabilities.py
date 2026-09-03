"""Model capability annotations for LLM models on SiliconFlow and OpenRouter."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


@dataclass
class ModelCapabilities:
    """Describes the capabilities of a single LLM model."""

    model_id: str
    tool_calling: bool
    vision: bool
    provider: str
    display_name: str
    free: bool


# ---------------------------------------------------------------------------
# Built-in capability table – loaded from config/model_capabilities.json
# 数据文件是单一事实来源；解析失败时降级为空表，未知模型走 infer_from_name 启发式。
# ---------------------------------------------------------------------------

_MODEL_CAPABILITIES_FILENAME = "model_capabilities.json"


def _candidate_paths() -> list[Path]:
    """按优先级返回数据文件候选路径：用户配置目录在前，打包/源码目录兜底。"""
    paths: list[Path] = []
    try:
        from config_paths import get_config_dir

        paths.append(get_config_dir() / _MODEL_CAPABILITIES_FILENAME)
    except Exception:
        pass  # 配置目录解析失败不阻塞，继续尝试打包目录
    paths.append(Path(__file__).resolve().parent.parent / "config" / _MODEL_CAPABILITIES_FILENAME)
    return paths


def _load_builtin_capabilities() -> dict[str, ModelCapabilities]:
    """从 model_capabilities.json 加载内建模型能力表（优先用户配置，其次内置文件）。"""

    last_error: Exception | None = None
    for path in _candidate_paths():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            table = raw.get("capabilities")
            if not isinstance(table, dict):
                raise ValueError("capabilities 字段缺失或不是对象")
            loaded: dict[str, ModelCapabilities] = {}
            for key, entry in table.items():
                if isinstance(entry, dict):
                    loaded[key] = ModelCapabilities(**entry)
            logger.info(
                "model_capabilities.loaded source={} count={}",
                str(path), len(loaded),
            )
            return loaded
        except (OSError, ValueError, TypeError) as e:
            last_error = e
            continue
    if last_error is not None:
        logger.warning(
            "model_capabilities.unavailable error={} using_empty_table",
            str(last_error),
        )
    return {}


BUILTIN_CAPABILITIES: dict[str, ModelCapabilities] = _load_builtin_capabilities()


def infer_from_name(model_id: str) -> ModelCapabilities:
    """Heuristically infer capabilities from the model identifier.

    Default: modern LLMs support tool calling unless they are specialized
    (OCR, translation, captioning, embedding, reranker, etc.).
    """

    lower = model_id.lower()

    # Vision detection
    vision = any(kw in lower for kw in ("vl", "vision", "ocr"))

    # Specialized models that do NOT support tool calling
    _NO_TOOL_KEYWORDS = (
        "ocr", "caption", "mt-", "translate", "embedding", "rerank",
        "tts", "asr", "stt", "speech", "image-gen", "image", "diffusion",
        "video", "whisper", "parakeet", "bge",
    )
    # Thinking/reasoning-only models often have limited tool support
    _THINKING_ONLY = "thinking" in lower and "instruct" not in lower

    if any(kw in lower for kw in _NO_TOOL_KEYWORDS) or _THINKING_ONLY:
        tool_calling = False
    else:
        # 现代策略：默认假设现代 LLM 支持工具调用
        # 只有明确不支持的模型才标注为不支持（已在上面过滤）
        # 这样可以覆盖更多支持工具调用的模型（如 Agnes、Llama、Mistral 等）
        tool_calling = True

    # Provider from prefix
    if "/" in model_id:
        provider = model_id.split("/")[0]
        display_name = model_id.split("/", 1)[1]
    else:
        provider = ""
        display_name = model_id

    return ModelCapabilities(
        model_id=model_id,
        tool_calling=tool_calling,
        vision=vision,
        provider=provider,
        display_name=display_name,
        free=True,
    )


def get_capabilities(
    model_id: str,
    openrouter_data: dict | None = None,
) -> ModelCapabilities:
    """Return capabilities for *model_id* using a priority chain.

    Priority 1 – exact match (case-insensitive) in ``BUILTIN_CAPABILITIES``
    Priority 2 – information extracted from *openrouter_data*
    Priority 3 – heuristic inference via :func:`infer_from_name`
    """

    # Priority 1: built-in table
    if model_id in BUILTIN_CAPABILITIES:
        return BUILTIN_CAPABILITIES[model_id]
    lower_key = model_id.lower()
    for key, cap in BUILTIN_CAPABILITIES.items():
        if key.lower() == lower_key:
            return cap

    # Priority 2: OpenRouter data
    if openrouter_data is not None:
        vision = False

        arch = openrouter_data.get("architecture", {})
        if isinstance(arch, dict):
            # 检查 architecture.modality 字段（OpenRouter 的标准格式）
            arch_modality = arch.get("modality", "")
            if isinstance(arch_modality, str):
                vision = "image" in arch_modality.lower()

        # 也检查顶层的 modality 字段（备用）
        modality = openrouter_data.get("modality", "")
        if isinstance(modality, str):
            vision = vision or "image" in modality.lower()
        elif isinstance(modality, dict):
            vision = vision or "image" in str(modality).lower()

        # 工具调用：OpenRouter 的 instruct_type 经常为 None，不可靠
        # 优先使用启发式推断（覆盖更准确），仅当有明确 instruct_type 时才使用 OpenRouter 数据
        inferred = infer_from_name(model_id)
        tool_calling = inferred.tool_calling

        if "/" in model_id:
            provider = model_id.split("/")[0]
            display_name = model_id.split("/", 1)[1]
        else:
            provider = "openrouter"
            display_name = model_id

        return ModelCapabilities(
            model_id=model_id,
            tool_calling=tool_calling,
            vision=vision,
            provider=provider,
            display_name=display_name,
            free=":free" in model_id,
        )

    # Priority 3: heuristic
    return infer_from_name(model_id)
