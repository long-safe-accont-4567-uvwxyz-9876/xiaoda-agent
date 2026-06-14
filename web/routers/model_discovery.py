"""模型发现路由：从 SiliconFlow / OpenRouter / ModelScope 发现免费模型，切换聊天模型。"""
from __future__ import annotations

import asyncio
import os
import time

from fastapi import APIRouter, Depends, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user
from web.model_capabilities import get_capabilities

router = APIRouter(tags=["model-discovery"], dependencies=[Depends(get_current_user)])

# ── 简易 30 分钟缓存 ──────────────────────────────────────────────

_cache: dict = {"data": None, "ts": 0.0}
_CACHE_TTL = 30 * 60  # 30 minutes


# ── GET /models/discover ──────────────────────────────────────────


async def _fetch_siliconflow() -> list[dict]:
    """从 SiliconFlow 获取免费聊天模型列表。"""
    api_key = os.getenv("SILICONFLOW_API_KEY", "")
    if not api_key:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://api.siliconflow.cn/v1/models",
                params={"type": "text", "sub_type": "chat"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            body = resp.json()
        models = []
        for item in body.get("data", []):
            model_id = item.get("id", "")
            if not model_id:
                continue
            caps = get_capabilities(model_id)
            models.append({
                "id": model_id,
                "display_name": caps.display_name,
                "free": True,
                "tool_calling": caps.tool_calling,
                "vision": caps.vision,
                "provider": "siliconflow",
            })
        return models
    except Exception as e:
        logger.warning("discover.siliconflow_failed error={}", str(e))
        return []


async def _fetch_openrouter() -> list[dict]:
    """从 OpenRouter 获取免费模型列表。"""
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            body = resp.json()
        models = []
        for item in body.get("data", []):
            model_id = item.get("id", "")
            if not model_id:
                continue
            pricing = item.get("pricing", {})
            if not isinstance(pricing, dict):
                continue
            prompt_price = pricing.get("prompt", "1")
            completion_price = pricing.get("completion", "1")
            if str(prompt_price) != "0" or str(completion_price) != "0":
                continue
            caps = get_capabilities(model_id, openrouter_data=item)
            models.append({
                "id": model_id,
                "display_name": caps.display_name,
                "free": True,
                "tool_calling": caps.tool_calling,
                "vision": caps.vision,
                "provider": "openrouter",
            })
        return models
    except Exception as e:
        logger.warning("discover.openrouter_failed error={}", str(e))
        return []


async def _fetch_modelscope() -> list[dict]:
    """从魔搭 ModelScope 获取支持 API-Inference 的免费模型列表。"""
    api_key = os.getenv("MODELSCOPE_ACCESS_TOKEN", "")
    if not api_key:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            # 魔搭 API-Inference 兼容 OpenAI 接口，/v1/models 列出可用模型
            resp = await client.get(
                "https://api-inference.modelscope.cn/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            body = resp.json()
        models = []
        for item in body.get("data", []):
            model_id = item.get("id", "")
            if not model_id:
                continue
            # 只保留 LLM 聊天模型（过滤掉 embedding/tts 等专用模型）
            caps = get_capabilities(model_id)
            # 跳过明确的非聊天模型
            lower = model_id.lower()
            if any(kw in lower for kw in ("embed", "tts", "asr", "stt", "rerank",
                                           "image-gen", "diffusion", "ocr", "captioner",
                                           "mt-", "translation")):
                continue
            models.append({
                "id": model_id,
                "display_name": caps.display_name,
                "free": True,
                "tool_calling": caps.tool_calling,
                "vision": caps.vision,
                "provider": "modelscope",
            })
        return models
    except Exception as e:
        logger.warning("discover.modelscope_failed error={}", str(e))
        return []


def _build_mimo_provider() -> dict:
    """构建 MiMo 内置 provider 的模型列表。"""
    from model_router import MIMO_MODEL, MIMO_PRO_MODEL
    models = []
    seen = set()
    for model_id in (MIMO_MODEL, MIMO_PRO_MODEL):
        if model_id in seen:
            continue
        seen.add(model_id)
        caps = get_capabilities(model_id)
        models.append({
            "id": model_id,
            "display_name": caps.display_name,
            "free": caps.free,
            "tool_calling": caps.tool_calling,
            "vision": caps.vision,
            "provider": "mimo",
        })
    return {"provider": "mimo", "models": models}


@router.get("/models/discover", response_model=Envelope[list[dict]])
async def discover_models():
    """发现各 provider 的可用模型，结果缓存 30 分钟。"""
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        return Envelope(data=_cache["data"])

    sf_models, or_models, ms_models = await asyncio.gather(
        _fetch_siliconflow(),
        _fetch_openrouter(),
        _fetch_modelscope(),
    )

    result = []

    # MiMo 内置
    try:
        result.append(_build_mimo_provider())
    except Exception as e:
        logger.warning("discover.mimo_failed error={}", str(e))

    # SiliconFlow
    if sf_models is not None:
        result.append({"provider": "siliconflow", "models": sf_models})

    # OpenRouter
    if or_models is not None:
        result.append({"provider": "openrouter", "models": or_models})

    # ModelScope
    if ms_models is not None:
        result.append({"provider": "modelscope", "models": ms_models})

    _cache["data"] = result
    _cache["ts"] = now
    return Envelope(data=result)


# ── POST /models/chat-model ──────────────────────────────────────


@router.post("/models/chat-model", response_model=Envelope[dict])
async def set_chat_model(body: dict, request: Request):
    """切换当前聊天模型。"""
    provider = (body.get("provider") or "").strip()
    model_id = (body.get("model_id") or "").strip()
    if not provider or not model_id:
        return Envelope(ok=False, error={"code": "invalid_input", "message": "provider 和 model_id 不能为空"})

    router_obj = request.app.state.core.router

    # 非 mimo 的 provider 需要自动注册为自定义 provider
    if provider not in ("mimo",):
        _ensure_custom_provider(provider, router_obj)

    try:
        info = router_obj.set_chat_model(provider, model_id)
        logger.info("discover.chat_model_set provider={} model={}", provider, model_id)
        return Envelope(data=info)
    except Exception as e:
        logger.error("discover.set_chat_model_failed error={}", str(e))
        return Envelope(ok=False, error={"code": "set_failed", "message": str(e)})


def _ensure_custom_provider(provider: str, router_obj) -> None:
    """确保自定义 provider 已注册到 router。"""
    if hasattr(router_obj, "_custom_clients") and provider in router_obj._custom_clients:
        return
    from web.custom_providers import register_into_router

    if provider == "siliconflow":
        api_key = os.getenv("SILICONFLOW_API_KEY", "")
        base_url = "https://api.siliconflow.cn/v1"
    elif provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        base_url = "https://openrouter.ai/api/v1"
    elif provider == "modelscope":
        api_key = os.getenv("MODELSCOPE_ACCESS_TOKEN", "")
        base_url = "https://api-inference.modelscope.cn/v1"
    elif provider == "agnes":
        api_key = os.getenv("AGNES_API_KEY", "")
        base_url = os.getenv("AGNES_BASE_URL", "https://api.agnes-ai.com/v1")
    else:
        logger.warning("discover.unknown_provider provider={}", provider)
        return

    if not api_key:
        logger.warning("discover.no_api_key provider={}", provider)
        return

    register_into_router(router_obj, provider, "openai", base_url, api_key)


# ── GET /models/chat-model ───────────────────────────────────────


@router.get("/models/chat-model", response_model=Envelope[dict])
async def get_chat_model(request: Request):
    """获取当前聊天模型信息。"""
    try:
        router_obj = request.app.state.core.router
        info = router_obj.get_current_chat_model()
        return Envelope(data=info or {})
    except Exception as e:
        logger.error("discover.get_chat_model_failed error={}", str(e))
        return Envelope(data={})
