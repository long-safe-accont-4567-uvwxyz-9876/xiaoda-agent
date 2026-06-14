"""Model capability annotations for LLM models on SiliconFlow and OpenRouter."""

from __future__ import annotations

from dataclasses import dataclass


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
# Built-in capability table – covers common free models
# ---------------------------------------------------------------------------

BUILTIN_CAPABILITIES: dict[str, ModelCapabilities] = {
    # ---- SiliconFlow models ----
    "Qwen/Qwen2.5-7B-Instruct": ModelCapabilities(
        model_id="Qwen/Qwen2.5-7B-Instruct",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Qwen2.5-7B-Instruct",
        free=True,
    ),
    "Qwen/Qwen2.5-14B-Instruct": ModelCapabilities(
        model_id="Qwen/Qwen2.5-14B-Instruct",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Qwen2.5-14B-Instruct",
        free=True,
    ),
    "Qwen/Qwen2.5-32B-Instruct": ModelCapabilities(
        model_id="Qwen/Qwen2.5-32B-Instruct",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Qwen2.5-32B-Instruct",
        free=True,
    ),
    "Qwen/Qwen2.5-72B-Instruct": ModelCapabilities(
        model_id="Qwen/Qwen2.5-72B-Instruct",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Qwen2.5-72B-Instruct",
        free=True,
    ),
    "Qwen/Qwen2.5-Coder-32B-Instruct": ModelCapabilities(
        model_id="Qwen/Qwen2.5-Coder-32B-Instruct",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Qwen2.5-Coder-32B-Instruct",
        free=True,
    ),
    "Qwen/Qwen2.5-VL-7B-Instruct": ModelCapabilities(
        model_id="Qwen/Qwen2.5-VL-7B-Instruct",
        tool_calling=True,
        vision=True,
        provider="siliconflow",
        display_name="Qwen2.5-VL-7B-Instruct",
        free=True,
    ),
    "Qwen/Qwen2.5-VL-32B-Instruct": ModelCapabilities(
        model_id="Qwen/Qwen2.5-VL-32B-Instruct",
        tool_calling=True,
        vision=True,
        provider="siliconflow",
        display_name="Qwen2.5-VL-32B-Instruct",
        free=True,
    ),
    "Qwen/Qwen2.5-VL-72B-Instruct": ModelCapabilities(
        model_id="Qwen/Qwen2.5-VL-72B-Instruct",
        tool_calling=True,
        vision=True,
        provider="siliconflow",
        display_name="Qwen2.5-VL-72B-Instruct",
        free=True,
    ),
    "Qwen/Qwen3-8B": ModelCapabilities(
        model_id="Qwen/Qwen3-8B",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Qwen3-8B",
        free=True,
    ),
    "Qwen/Qwen3-14B": ModelCapabilities(
        model_id="Qwen/Qwen3-14B",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Qwen3-14B",
        free=True,
    ),
    "Qwen/Qwen3-32B": ModelCapabilities(
        model_id="Qwen/Qwen3-32B",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Qwen3-32B",
        free=True,
    ),
    "Qwen/Qwen3-235B-A22B": ModelCapabilities(
        model_id="Qwen/Qwen3-235B-A22B",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Qwen3-235B-A22B",
        free=True,
    ),
    "Qwen/Qwen3-Coder-30B-A3B-Instruct": ModelCapabilities(
        model_id="Qwen/Qwen3-Coder-30B-A3B-Instruct",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Qwen3-Coder-30B-A3B-Instruct",
        free=True,
    ),
    "deepseek-ai/DeepSeek-V3": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-V3",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="DeepSeek-V3",
        free=True,
    ),
    "deepseek-ai/DeepSeek-R1": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-R1",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="DeepSeek-R1",
        free=True,
    ),
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="DeepSeek-R1-Distill-Qwen-7B",
        free=True,
    ),
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-14B": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-R1-Distill-Qwen-14B",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="DeepSeek-R1-Distill-Qwen-14B",
        free=True,
    ),
    "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="DeepSeek-R1-Distill-Qwen-32B",
        free=True,
    ),
    "deepseek-ai/DeepSeek-VL2": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-VL2",
        tool_calling=False,
        vision=True,
        provider="siliconflow",
        display_name="DeepSeek-VL2",
        free=True,
    ),
    "THUDM/glm-4-9b-chat": ModelCapabilities(
        model_id="THUDM/glm-4-9b-chat",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="glm-4-9b-chat",
        free=True,
    ),
    "meta-llama/Meta-Llama-3.1-8B-Instruct": ModelCapabilities(
        model_id="meta-llama/Meta-Llama-3.1-8B-Instruct",
        tool_calling=True,
        vision=False,
        provider="siliconflow",
        display_name="Meta-Llama-3.1-8B-Instruct",
        free=True,
    ),
    # ---- SiliconFlow 新模型 (2025-2026) ----
    "deepseek-ai/DeepSeek-V4-Pro": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-V4-Pro",
        tool_calling=False, vision=False,
        provider="siliconflow", display_name="DeepSeek-V4-Pro", free=True,
    ),
    "deepseek-ai/DeepSeek-V4-Flash": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-V4-Flash",
        tool_calling=False, vision=False,
        provider="siliconflow", display_name="DeepSeek-V4-Flash", free=True,
    ),
    "Pro/moonshotai/Kimi-K2.6": ModelCapabilities(
        model_id="Pro/moonshotai/Kimi-K2.6",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Kimi-K2.6", free=True,
    ),
    "Pro/zai-org/GLM-5.1": ModelCapabilities(
        model_id="Pro/zai-org/GLM-5.1",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="GLM-5.1", free=True,
    ),
    "nex-agi/Nex-N2-Pro": ModelCapabilities(
        model_id="nex-agi/Nex-N2-Pro",
        tool_calling=True, vision=True,
        provider="siliconflow", display_name="Nex-N2-Pro", free=True,
    ),
    "MiniMaxAI/MiniMax-M2.5": ModelCapabilities(
        model_id="MiniMaxAI/MiniMax-M2.5",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="MiniMax-M2.5", free=True,
    ),
    "Pro/MiniMaxAI/MiniMax-M2.5": ModelCapabilities(
        model_id="Pro/MiniMaxAI/MiniMax-M2.5",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="MiniMax-M2.5 (Pro)", free=True,
    ),
    "deepseek-ai/DeepSeek-V3.2": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-V3.2",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="DeepSeek-V3.2", free=True,
    ),
    "Pro/deepseek-ai/DeepSeek-V3.2": ModelCapabilities(
        model_id="Pro/deepseek-ai/DeepSeek-V3.2",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="DeepSeek-V3.2 (Pro)", free=True,
    ),
    "deepseek-ai/DeepSeek-V3.1-Terminus": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-V3.1-Terminus",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="DeepSeek-V3.1-Terminus", free=True,
    ),
    "Pro/deepseek-ai/DeepSeek-V3.1-Terminus": ModelCapabilities(
        model_id="Pro/deepseek-ai/DeepSeek-V3.1-Terminus",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="DeepSeek-V3.1-Terminus (Pro)", free=True,
    ),
    "Qwen/Qwen3.6-35B-A3B": ModelCapabilities(
        model_id="Qwen/Qwen3.6-35B-A3B",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Qwen3.6-35B-A3B", free=True,
    ),
    "Qwen/Qwen3.6-27B": ModelCapabilities(
        model_id="Qwen/Qwen3.6-27B",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Qwen3.6-27B", free=True,
    ),
    "Qwen/Qwen3.5-397B-A17B": ModelCapabilities(
        model_id="Qwen/Qwen3.5-397B-A17B",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Qwen3.5-397B-A17B", free=True,
    ),
    "Qwen/Qwen3.5-122B-A10B": ModelCapabilities(
        model_id="Qwen/Qwen3.5-122B-A10B",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Qwen3.5-122B-A10B", free=True,
    ),
    "Qwen/Qwen3.5-35B-A3B": ModelCapabilities(
        model_id="Qwen/Qwen3.5-35B-A3B",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Qwen3.5-35B-A3B", free=True,
    ),
    "Qwen/Qwen3.5-27B": ModelCapabilities(
        model_id="Qwen/Qwen3.5-27B",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Qwen3.5-27B", free=True,
    ),
    "Qwen/Qwen3.5-9B": ModelCapabilities(
        model_id="Qwen/Qwen3.5-9B",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Qwen3.5-9B", free=True,
    ),
    "Qwen/Qwen3.5-4B": ModelCapabilities(
        model_id="Qwen/Qwen3.5-4B",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Qwen3.5-4B", free=True,
    ),
    "PaddlePaddle/PaddleOCR-VL-1.5": ModelCapabilities(
        model_id="PaddlePaddle/PaddleOCR-VL-1.5",
        tool_calling=False, vision=True,
        provider="siliconflow", display_name="PaddleOCR-VL-1.5", free=True,
    ),
    "Pro/deepseek-ai/DeepSeek-R1": ModelCapabilities(
        model_id="Pro/deepseek-ai/DeepSeek-R1",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="DeepSeek-R1 (Pro)", free=True,
    ),
    "Pro/deepseek-ai/DeepSeek-V3": ModelCapabilities(
        model_id="Pro/deepseek-ai/DeepSeek-V3",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="DeepSeek-V3 (Pro)", free=True,
    ),
    "stepfun-ai/Step-3.5-Flash": ModelCapabilities(
        model_id="stepfun-ai/Step-3.5-Flash",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Step-3.5-Flash", free=True,
    ),
    "Qwen/Qwen3-VL-32B-Thinking": ModelCapabilities(
        model_id="Qwen/Qwen3-VL-32B-Thinking",
        tool_calling=False, vision=True,
        provider="siliconflow", display_name="Qwen3-VL-32B-Thinking", free=True,
    ),
    "Qwen/Qwen3-VL-8B-Thinking": ModelCapabilities(
        model_id="Qwen/Qwen3-VL-8B-Thinking",
        tool_calling=False, vision=True,
        provider="siliconflow", display_name="Qwen3-VL-8B-Thinking", free=True,
    ),
    "Qwen/Qwen3-VL-30B-A3B-Thinking": ModelCapabilities(
        model_id="Qwen/Qwen3-VL-30B-A3B-Thinking",
        tool_calling=False, vision=True,
        provider="siliconflow", display_name="Qwen3-VL-30B-A3B-Thinking", free=True,
    ),
    "Qwen/Qwen3-Omni-30B-A3B-Instruct": ModelCapabilities(
        model_id="Qwen/Qwen3-Omni-30B-A3B-Instruct",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Qwen3-Omni-30B-A3B-Instruct", free=True,
    ),
    "Qwen/Qwen3-Omni-30B-A3B-Thinking": ModelCapabilities(
        model_id="Qwen/Qwen3-Omni-30B-A3B-Thinking",
        tool_calling=False, vision=False,
        provider="siliconflow", display_name="Qwen3-Omni-30B-A3B-Thinking", free=True,
    ),
    "Qwen/Qwen3-Omni-30B-A3B-Captioner": ModelCapabilities(
        model_id="Qwen/Qwen3-Omni-30B-A3B-Captioner",
        tool_calling=False, vision=False,
        provider="siliconflow", display_name="Qwen3-Omni-30B-A3B-Captioner", free=True,
    ),
    "deepseek-ai/DeepSeek-OCR": ModelCapabilities(
        model_id="deepseek-ai/DeepSeek-OCR",
        tool_calling=False, vision=True,
        provider="siliconflow", display_name="DeepSeek-OCR", free=True,
    ),
    "inclusionAI/Ling-flash-2.0": ModelCapabilities(
        model_id="inclusionAI/Ling-flash-2.0",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Ling-flash-2.0", free=True,
    ),
    "inclusionAI/Ling-mini-2.0": ModelCapabilities(
        model_id="inclusionAI/Ling-mini-2.0",
        tool_calling=True, vision=False,
        provider="siliconflow", display_name="Ling-mini-2.0", free=True,
    ),
    "tencent/Hunyuan-MT-7B": ModelCapabilities(
        model_id="tencent/Hunyuan-MT-7B",
        tool_calling=False, vision=False,
        provider="siliconflow", display_name="Hunyuan-MT-7B", free=True,
    ),
    # ---- OpenRouter models ----
    "meta-llama/llama-4-maverick:free": ModelCapabilities(
        model_id="meta-llama/llama-4-maverick:free",
        tool_calling=True,
        vision=True,
        provider="openrouter",
        display_name="llama-4-maverick:free",
        free=True,
    ),
    "meta-llama/llama-4-scout:free": ModelCapabilities(
        model_id="meta-llama/llama-4-scout:free",
        tool_calling=True,
        vision=True,
        provider="openrouter",
        display_name="llama-4-scout:free",
        free=True,
    ),
    "qwen/qwen3-235b-a22b:free": ModelCapabilities(
        model_id="qwen/qwen3-235b-a22b:free",
        tool_calling=True,
        vision=False,
        provider="openrouter",
        display_name="qwen3-235b-a22b:free",
        free=True,
    ),
    "deepseek/deepseek-r1:free": ModelCapabilities(
        model_id="deepseek/deepseek-r1:free",
        tool_calling=True,
        vision=False,
        provider="openrouter",
        display_name="deepseek-r1:free",
        free=True,
    ),
    "deepseek/deepseek-chat:free": ModelCapabilities(
        model_id="deepseek/deepseek-chat:free",
        tool_calling=True,
        vision=False,
        provider="openrouter",
        display_name="deepseek-chat:free",
        free=True,
    ),
    "google/gemma-3-27b-it:free": ModelCapabilities(
        model_id="google/gemma-3-27b-it:free",
        tool_calling=True,
        vision=True,
        provider="openrouter",
        display_name="gemma-3-27b-it:free",
        free=True,
    ),
    "nvidia/nemotron-3-super-120b-a12b:free": ModelCapabilities(
        model_id="nvidia/nemotron-3-super-120b-a12b:free",
        tool_calling=True,
        vision=False,
        provider="openrouter",
        display_name="nemotron-3-super-120b-a12b:free",
        free=True,
    ),
    "openai/gpt-oss-120b:free": ModelCapabilities(
        model_id="openai/gpt-oss-120b:free",
        tool_calling=True,
        vision=False,
        provider="openrouter",
        display_name="gpt-oss-120b:free",
        free=True,
    ),
    # ---- MiMo models ----
    "mimo-v2.5": ModelCapabilities(
        model_id="mimo-v2.5",
        tool_calling=True,
        vision=True,
        provider="mimo",
        display_name="mimo-v2.5",
        free=False,
    ),
    "mimo-v2.5-pro": ModelCapabilities(
        model_id="mimo-v2.5-pro",
        tool_calling=True,
        vision=False,
        provider="mimo",
        display_name="mimo-v2.5-pro",
        free=False,
    ),
}


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
        "tts", "asr", "stt", "speech", "image-gen", "diffusion",
    )
    # Thinking/reasoning-only models often have limited tool support
    _THINKING_ONLY = "thinking" in lower and "instruct" not in lower

    if any(kw in lower for kw in _NO_TOOL_KEYWORDS):
        tool_calling = False
    elif _THINKING_ONLY:
        tool_calling = False
    else:
        # 保守策略：只有明确包含工具调用关键词的才标注支持
        _TOOL_KEYWORDS = (
            "instruct", "chat", "coder", "agent", "function",
        )
        tool_calling = any(kw in lower for kw in _TOOL_KEYWORDS)

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
        tool_calling = False
        vision = False

        arch = openrouter_data.get("architecture", {})
        if isinstance(arch, dict):
            inst_type = arch.get("instruction_type", "")
            if inst_type and inst_type != "none":
                tool_calling = True

        modality = openrouter_data.get("modality", "")
        if isinstance(modality, str):
            # modality can be e.g. "text+image->text"
            vision = "image" in modality.lower()
        elif isinstance(modality, dict):
            # some endpoints return modality as an object
            vision = "image" in str(modality).lower()

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
