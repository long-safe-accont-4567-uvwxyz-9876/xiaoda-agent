"""模型发现路由：自动发现所有已注册 provider 的可用模型，标注免费/付费。"""
from __future__ import annotations
from typing import Any

import asyncio
import os
import time

from fastapi import APIRouter, Depends, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user
from web.model_capabilities import get_capabilities
# 缓存抽到 web._discovery_cache, 避免与 web.routers.models 互相导入
from web._discovery_cache import _cache, _CACHE_TTL, invalidate_discovery_cache

router = APIRouter(tags=["model-discovery"], dependencies=[Depends(get_current_user)])


# ── 通用 OpenAI 兼容模型获取 ──────────────────────────────────────


# 非聊天模型关键词（用于过滤 /v1/models 返回的专用模型）
_NON_CHAT_KEYWORDS = (
    "embed", "tts", "asr", "stt", "rerank",
    "image-gen", "image", "diffusion", "ocr", "captioner",
    "mt-", "translation", "speech", "video",
    "whisper", "parakeet", "bge", "kolor", "voice",
)

# 不支持 /models 端点的 provider，用内置已知模型列表作为降级
# Agnes AI 没有 /v1/models 列表端点，只有一个文本模型
BUILTIN_FALLBACK_MODELS = {
    "agnes": [
        {"id": "agnes-2.0-flash", "display_name": "Agnes Flash 2.0", "free": True, "tool_calling": True, "vision": False},
    ],
}


async def _fetch_openai_compatible_models(
    provider_id: str,
    base_url: str,
    api_key: str,
    label: str = "",
) -> list[dict]:
    """通用 OpenAI 兼容 /v1/models 获取，适用于所有自定义 provider。

    返回模型列表，每个模型包含 id/display_name/free/tool_calling/vision/provider。
    对于无法确定免费/付费的模型，默认标记为 free=False。

    支持多种响应格式：标准 {"data": [...]}、备用 {"models": [...]}、根数组 [...]，
    以及列表元素为字符串或含 id/name/model 字段的 dict。
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

        # 从多种响应格式中提取模型列表
        raw_items = None
        if isinstance(body, list):
            raw_items = body
        elif isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list):
                raw_items = data
            elif isinstance(data, dict) and isinstance(data.get("models"), list):
                raw_items = data["models"]
            elif isinstance(body.get("models"), list):
                raw_items = body["models"]

        if raw_items is None:
            body_preview = str(body)[:500]
            logger.warning(
                "discover.unsupported_format provider={} body_preview={}",
                provider_id, body_preview,
            )
            return []

        models = []
        for item in raw_items:
            # item 可能是字符串或 dict
            if isinstance(item, str):
                model_id = item
                item = {}
            elif isinstance(item, dict):
                model_id = item.get("id", "") or item.get("name", "") or item.get("model", "")
            else:
                continue

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

    基于真实定价数据判断，无法确认的一律标记为付费。

    - OpenRouter: API 返回 pricing 字段，prompt==0 && completion==0 为免费
    - SiliconFlow: 抓取官网定价页面，inputPrice==0 && outputPrice==0 为免费
    - Ollama: 本地部署，永远免费
    - Agnes: 免费平台
    - ModelScope: 推理 API 有免费额度
    - 其他 provider: 默认付费
    """
    # OpenRouter 有完整的 pricing 字段
    if provider_id == "openrouter":
        pricing = item.get("pricing", {})
        if isinstance(pricing, dict):
            prompt_price = str(pricing.get("prompt", "1"))
            completion_price = str(pricing.get("completion", "1"))
            return prompt_price == "0" and completion_price == "0"
        return ":free" in model_id

    # SiliconFlow: 从官网定价页面获取真实价格
    if provider_id == "siliconflow":
        sf_pricing = _get_siliconflow_pricing()
        if sf_pricing:
            prices = sf_pricing.get(model_id, {})
            return prices.get("input", 1) == 0 and prices.get("output", 1) == 0
        # 定价数据获取失败时，无法确认 → 付费
        return False

    # Ollama 本地部署，永远免费
    if provider_id == "ollama":
        return True

    # Agnes 免费平台
    if provider_id == "agnes":
        return True

    # ModelScope 推理 API 有免费额度
    if provider_id == "modelscope":
        return True

    # DeepSeek / MiMo / 其他 → 付费
    return False


# ── SiliconFlow 定价抓取（缓存 6 小时）──────────────────────────

_sf_pricing_cache: dict[str, dict] | None = None
_sf_pricing_ts: float = 0
_SF_PRICING_TTL = 6 * 3600  # 6 小时


def _get_siliconflow_pricing() -> dict[str, dict] | None:
    """获取 SiliconFlow 模型定价（从官网 SSR 页面解析，内存缓存 6 小时）。

    返回 {model_id: {"input": price, "output": price}} 字典。
    price 单位为 ￥/M Tokens，0 表示免费。
    """
    global _sf_pricing_cache, _sf_pricing_ts
    if _sf_pricing_cache and time.time() - _sf_pricing_ts < _SF_PRICING_TTL:
        return _sf_pricing_cache

    try:
        import re as _re
        import httpx
        resp = httpx.get("https://siliconflow.cn/models",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        html = resp.text

        # 从 Next.js RSC push 块中提取模型定价数据
        pricing_map: dict[str, dict] = {}
        for block_m in _re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, _re.DOTALL):
            block = block_m.group(1)
            if "inputPrice" not in block or "modelName" not in block:
                continue
            # 反转义: \\" → "，\\\\ → \\
            s = block.replace('\\\\', '\x00').replace('\\"', '"').replace('\x00', '\\')
            # 按 {"modelId" 分割，每段包含一个模型的完整信息
            parts = _re.split(r'\{"modelId"', s)
            for part in parts[1:]:
                name_m = _re.search(r'"modelName"\s*:\s*"([^"]+)"', part)
                input_m = _re.search(r'"inputPrice"\s*:\s*(\d+)', part)
                output_m = _re.search(r'"outputPrice"\s*:\s*(\d+)', part)
                if name_m and input_m and output_m:
                    pricing_map[name_m.group(1)] = {
                        "input": int(input_m.group(1)),
                        "output": int(output_m.group(1)),
                    }
            break  # 只需要一个包含定价的 block

        if pricing_map:
            _sf_pricing_cache = pricing_map
            _sf_pricing_ts = time.time()
            free_count = sum(1 for v in pricing_map.values() if v["input"] == 0 and v["output"] == 0)
            logger.info("siliconflow.pricing_loaded total={} free={}", len(pricing_map), free_count)
            return pricing_map
        else:
            logger.warning("siliconflow.pricing_parse_empty")
            return None

    except Exception as e:
        logger.warning("siliconflow.pricing_fetch_failed error={}", str(e))
        return None


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

        # 获取定价数据用于判断免费/付费
        sf_pricing = _get_siliconflow_pricing()

        models = []
        for item in body.get("data", []):
            model_id = item.get("id", "")
            if not model_id:
                continue
            # 用真实定价数据判断免费/付费
            if sf_pricing:
                prices = sf_pricing.get(model_id, {})
                free = prices.get("input", 1) == 0 and prices.get("output", 1) == 0
            else:
                free = False
            caps = get_capabilities(model_id)
            models.append({
                "id": model_id,
                "display_name": caps.display_name,
                "free": free,
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
    """获取所有已注册的 provider 信息（从 config_service 动态读取）。

    返回列表，每项包含 id/label/format/base_url/api_key。
    """
    providers = []

    # 从 config_service 读取所有 provider
    try:
        from web.config_service import get_config_service
        from web._provider_keys import load_provider_key
        cfg = get_config_service()
        custom = cfg.get("models.providers", {}) or {}
        # 按 order 字段升序排列；未设置 order 的排在已设置之后，按字典插入顺序
        keys_order = list(custom.keys())
        sorted_custom = sorted(
            custom.items(),
            key=lambda kv: (kv[1].get("order", 9999), keys_order.index(kv[0]))
        )
        for pid, p in sorted_custom:
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
                "builtin": p.get("builtin", False),
                "order": p.get("order", 9999),
            })
    except Exception as e:
        logger.warning("discover.load_providers_failed error={}", str(e))

    return providers


# ── GET /models/discover ──────────────────────────────────────────
# 注: invalidate_discovery_cache 已抽到 web._discovery_cache


@router.get("/models/discover", response_model=Envelope[list[dict]])
async def discover_models() -> Any:
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
            async def _mimo_task() -> Any:
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

    # 对标注为内置降级的 provider，若 results 中返回异常或空列表，使用 fallback
    for i, (pid, models_or_exc) in enumerate(zip(provider_ids, results)):
        if pid in BUILTIN_FALLBACK_MODELS:
            if isinstance(models_or_exc, Exception) or (isinstance(models_or_exc, list) and not models_or_exc):
                results[i] = BUILTIN_FALLBACK_MODELS[pid]

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
async def set_chat_model(body: dict, request: Request) -> Any:
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
        # 广播 config_changed WS 事件，通知前端刷新 Agent 模型选项
        try:
            from web.ws_hub import manager
            await manager.broadcast({
                "type": "config_changed",
                "payload": {"type": "chat_model", "provider": provider, "model_id": model_id},
            })
        except Exception as e:
            logger.warning("discover.chat_model_broadcast_failed error={}", str(e))
        return Envelope(data=info)
    except Exception as e:
        logger.error("discover.set_chat_model_failed error={}", str(e))
        return Envelope(ok=False, error={"code": "set_failed", "message": str(e)})


def _ensure_custom_provider(provider: str, router_obj: Any) -> None:
    """确保自定义 provider 已注册到 router。

    从 config_service 动态读取 provider 配置，不再硬编码。
    """
    if hasattr(router_obj, "_custom_clients") and provider in router_obj._custom_clients:
        return

    # 先尝试从 config_service 读取
    try:
        from web.config_service import get_config_service
        from web._provider_keys import load_provider_key
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
