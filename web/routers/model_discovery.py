"""模型发现路由：自动发现所有已注册 provider 的可用模型，标注免费/付费。"""
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


# ── 通用 OpenAI 兼容模型获取 ──────────────────────────────────────


# 非聊天模型关键词（用于过滤 /v1/models 返回的专用模型）
_NON_CHAT_KEYWORDS = (
    "embed", "tts", "asr", "stt", "rerank",
    "image-gen", "diffusion", "ocr", "captioner",
    "mt-", "translation", "speech",
)


async def _fetch_openai_compatible_models(
    provider_id: str,
    base_url: str,
    api_key: str,
    label: str = "",
) -> list[dict]:
    """通用 OpenAI 兼容 /v1/models 获取，适用于所有自定义 provider。

    返回模型列表，每个模型包含 id/display_name/free/tool_calling/vision/provider。
    对于无法确定免费/付费的模型，默认标记为 free=True。
    """
    try:
        import httpx
        url = base_url.rstrip("/") + "/models"
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            body = resp.json()

        models = []
        for item in body.get("data", []):
            model_id = item.get("id", "")
            if not model_id:
                continue

            # 过滤非聊天模型
            lower = model_id.lower()
            if any(kw in lower for kw in _NON_CHAT_KEYWORDS):
                continue

            # 判断免费/付费
            free = _determine_free(provider_id, model_id, item)

            caps = get_capabilities(model_id, openrouter_data=item if provider_id == "openrouter" else None)
            models.append({
                "id": model_id,
                "display_name": caps.display_name,
                "free": free,
                "tool_calling": caps.tool_calling,
                "vision": caps.vision,
                "provider": provider_id,
            })

        logger.info("discover.fetched provider={} count={}", provider_id, len(models))
        return models
    except Exception as e:
        logger.warning("discover.fetch_failed provider={} error={}", provider_id, str(e))
        return []


def _determine_free(provider_id: str, model_id: str, item: dict) -> bool:
    """判断模型是否免费。

    - OpenRouter: 通过 pricing 字段判断，prompt==0 && completion==0 为免费
    - SiliconFlow: 所有模型免费
    - 其他: 默认免费（无法确定时给用户最大可用性）
    """
    if provider_id == "openrouter":
        pricing = item.get("pricing", {})
        if isinstance(pricing, dict):
            prompt_price = str(pricing.get("prompt", "1"))
            completion_price = str(pricing.get("completion", "1"))
            return prompt_price == "0" and completion_price == "0"
        # OpenRouter 模型 ID 带 :free 后缀的也是免费
        return ":free" in model_id

    if provider_id == "siliconflow":
        return True

    if provider_id == "modelscope":
        return True

    # MiMo 是付费的
    if provider_id == "mimo":
        return False

    # 其他自定义 provider 默认免费
    return True


# ── 特殊 provider 的获取逻辑 ──────────────────────────────────────


async def _fetch_openrouter_models(api_key: str) -> list[dict]:
    """OpenRouter 特殊处理：获取全部模型，同时标注免费和付费。

    与通用方法不同，OpenRouter 返回所有模型（包括付费的），
    通过 pricing 字段区分免费/付费。
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
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

            # 过滤非聊天模型
            lower = model_id.lower()
            if any(kw in lower for kw in _NON_CHAT_KEYWORDS):
                continue

            free = _determine_free("openrouter", model_id, item)
            caps = get_capabilities(model_id, openrouter_data=item)
            models.append({
                "id": model_id,
                "display_name": caps.display_name,
                "free": free,
                "tool_calling": caps.tool_calling,
                "vision": caps.vision,
                "provider": "openrouter",
            })

        logger.info("discover.fetched provider=openrouter count={}", len(models))
        return models
    except Exception as e:
        logger.warning("discover.openrouter_failed error={}", str(e))
        return []


async def _fetch_siliconflow_models(api_key: str) -> list[dict]:
    """SiliconFlow 特殊处理：使用 type/sub_type 参数过滤聊天模型。"""
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
        logger.info("discover.fetched provider=siliconflow count={}", len(models))
        return models
    except Exception as e:
        logger.warning("discover.siliconflow_failed error={}", str(e))
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


# ── 获取所有已注册 provider 信息 ──────────────────────────────────


def _get_all_providers() -> list[dict]:
    """获取所有已注册的 provider 信息（内置 + 自定义）。

    返回列表，每项包含 id/label/format/base_url/api_key。
    """
    providers = []

    # MiMo 内置
    mimo_key = os.getenv("MIMO_API_KEY", "")
    if mimo_key:
        providers.append({
            "id": "mimo",
            "label": "小米 MiMo",
            "format": "openai",
            "base_url": os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1"),
            "api_key": mimo_key,
            "builtin": True,
        })

    # 从 config_service 读取自定义 provider
    try:
        from web.config_service import get_config_service
        from web.routers.models import load_provider_key
        cfg = get_config_service()
        custom = cfg.get("models.providers", {}) or {}
        for pid, p in custom.items():
            if not p.get("enabled", True):
                continue
            key = load_provider_key(pid)
            if not key:
                continue
            providers.append({
                "id": pid,
                "label": p.get("label", pid),
                "format": p.get("format", "openai"),
                "base_url": p.get("base_url", ""),
                "api_key": key,
                "builtin": False,
            })
    except Exception as e:
        logger.warning("discover.load_providers_failed error={}", str(e))

    return providers


# ── 缓存管理 ──────────────────────────────────────────────────────


def invalidate_discovery_cache() -> None:
    """清除模型发现缓存，使下次请求重新获取。"""
    _cache["data"] = None
    _cache["ts"] = 0.0


# ── GET /models/discover ──────────────────────────────────────────


@router.get("/models/discover", response_model=Envelope[list[dict]])
async def discover_models():
    """发现所有已注册 provider 的可用模型，结果缓存 30 分钟。

    自动发现所有已注册的 provider（包括内置 MiMo 和自定义 provider），
    通过 OpenAI 兼容的 /v1/models 接口获取模型列表。
    OpenRouter 和 SiliconFlow 有特殊处理逻辑，其他 provider 使用通用获取方法。
    每个模型标注 free（免费/付费）。
    """
    now = time.time()
    if _cache["data"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        return Envelope(data=_cache["data"])

    all_providers = _get_all_providers()

    # 并发获取所有 provider 的模型
    tasks = []
    provider_ids = []
    for p in all_providers:
        pid = p["id"]
        provider_ids.append(pid)

        if pid == "mimo":
            # MiMo 不需要 API 调用，直接构建
            async def _mimo_task():
                return _build_mimo_provider()
            tasks.append(_mimo_task())
        elif pid == "openrouter":
            tasks.append(_fetch_openrouter_models(p["api_key"]))
        elif pid == "siliconflow":
            tasks.append(_fetch_siliconflow_models(p["api_key"]))
        else:
            # 通用 OpenAI 兼容 provider
            tasks.append(_fetch_openai_compatible_models(
                provider_id=pid,
                base_url=p["base_url"],
                api_key=p["api_key"],
                label=p.get("label", pid),
            ))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    result = []
    for pid, models_or_exc in zip(provider_ids, results):
        if isinstance(models_or_exc, Exception):
            logger.warning("discover.provider_failed provider={} error={}", pid, str(models_or_exc))
            continue

        # MiMo 返回的是完整的 provider dict
        if pid == "mimo" and isinstance(models_or_exc, dict):
            result.append(models_or_exc)
            continue

        # 其他 provider 返回模型列表
        if isinstance(models_or_exc, list) and models_or_exc:
            # 找到对应的 provider label
            label = ""
            for p in all_providers:
                if p["id"] == pid:
                    label = p.get("label", pid)
                    break
            result.append({
                "provider": pid,
                "label": label,
                "models": models_or_exc,
            })

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
    """确保自定义 provider 已注册到 router。

    从 config_service 动态读取 provider 配置，不再硬编码。
    """
    if hasattr(router_obj, "_custom_clients") and provider in router_obj._custom_clients:
        return

    # 先尝试从 config_service 读取
    try:
        from web.config_service import get_config_service
        from web.routers.models import load_provider_key
        cfg = get_config_service()
        record = cfg.get(f"models.providers.{provider}")
        if record:
            api_key = load_provider_key(provider)
            if api_key:
                from web.custom_providers import register_into_router
                register_into_router(
                    router_obj, provider,
                    record.get("format", "openai"),
                    record.get("base_url", ""),
                    api_key,
                )
                return
    except Exception as e:
        logger.debug("discover.config_service_lookup_failed provider={} error={}", provider, str(e))

    # 回退：从环境变量读取已知 provider
    from web.custom_providers import register_into_router

    _ENV_FALLBACK = {
        "siliconflow": ("SILICONFLOW_API_KEY", "https://api.siliconflow.cn/v1"),
        "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
        "modelscope": ("MODELSCOPE_ACCESS_TOKEN", "https://api-inference.modelscope.cn/v1"),
        "agnes": ("AGNES_API_KEY", os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")),
    }

    if provider in _ENV_FALLBACK:
        env_key, base_url = _ENV_FALLBACK[provider]
        api_key = os.getenv(env_key, "")
        if api_key:
            register_into_router(router_obj, provider, "openai", base_url, api_key)
            return

    logger.warning("discover.unknown_provider provider={}", provider)


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
