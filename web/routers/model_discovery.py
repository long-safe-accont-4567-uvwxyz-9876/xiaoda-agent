"""模型发现路由：自动发现所有已注册 provider 的可用模型，标注免费/付费。"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from loguru import logger

# 缓存抽到 web._discovery_cache, 避免与 web.routers.models 互相导入
from web._discovery_cache import _CACHE_TTL, _cache, _cache_lock
from web.model_capabilities import get_capabilities
from web.routers.auth import get_current_user
from web.schemas import Envelope

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
        # 修复 P2 Bug 12: ollama 等本地服务连接失败导致 discover.fetch_failed 告警风暴
        # 根因：本地服务未启动时，httpx 仍等待 15s 超时，且每次刷新模型列表都告警
        # 策略：本地 provider（127.0.0.1/localhost）用 3s 短超时；连接拒绝降级 debug
        # P1-8: 改用 httpx.Timeout 分别配置 connect（短）和 read（长，本地服务响应慢）超时
        is_local = "127.0.0.1" in base_url or "localhost" in base_url
        if is_local:
            timeout = httpx.Timeout(connect=3.0, read=10.0, write=3.0, pool=3.0)
        else:
            timeout = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                resp = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except (httpx.ConnectError, ConnectionRefusedError) as e:
                # 本地服务未启动：降级 debug，避免告警风暴
                logger.debug("discover.connect_failed provider={} error={} (local={})",
                             provider_id, str(e)[:100], is_local)
                return []
            except Exception:
                logger.exception("model_discovery._fetch_openai_compatible_models.unexpected_error")
                return []
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
            if isinstance(item, str):
                model_id = item
                item_dict: dict = {}
            elif isinstance(item, dict):
                model_id = item.get("id", "") or item.get("name", "") or item.get("model", "")
                item_dict = item
            else:
                continue

            if not model_id:
                continue

            # 过滤非聊天模型
            lower = model_id.lower()
            if any(kw in lower for kw in _NON_CHAT_KEYWORDS):
                continue

            # 判断免费/付费
            free = await _determine_free(provider_id, model_id, item_dict)

            caps = get_capabilities(model_id, openrouter_data=item_dict if provider_id == "openrouter" else None)
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
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        logger.warning("discover.fetch_failed provider={} error={}", provider_id, str(e))
        return []
    except Exception:
        logger.exception("model_discovery.unknown.unexpected_error")
        return []


async def _determine_free(provider_id: str, model_id: str, item: dict) -> bool:
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
        sf_pricing = await _get_siliconflow_pricing()
        if sf_pricing:
            prices = sf_pricing.get(model_id, {})
            return prices.get("input", 1) == 0 and prices.get("output", 1) == 0
        # 定价数据获取失败时，无法确认 → 付费
        return False

    # Ollama / llama.cpp 本地部署，永远免费
    if provider_id in ("ollama", "llama.cpp"):
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
_SF_PRICING_TTL = 6 * 3600
_sf_pricing_lock = asyncio.Lock()


async def _get_siliconflow_pricing() -> dict[str, dict] | None:
    global _sf_pricing_cache, _sf_pricing_ts
    async with _sf_pricing_lock:
        if _sf_pricing_cache and time.time() - _sf_pricing_ts < _SF_PRICING_TTL:
            return _sf_pricing_cache

    try:
        import re as _re

        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("https://siliconflow.cn/models",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        html = resp.text

        pricing_map: dict[str, dict] = {}
        for block_m in _re.finditer(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, _re.DOTALL):
            block = block_m.group(1)
            if "inputPrice" not in block or "modelName" not in block:
                continue
            s = block.replace('\\\\', '\x00').replace('\\"', '"').replace('\x00', '\\')
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
            break

        if pricing_map:
            async with _sf_pricing_lock:
                _sf_pricing_cache = pricing_map
                _sf_pricing_ts = time.time()
            free_count = sum(1 for v in pricing_map.values() if v["input"] == 0 and v["output"] == 0)
            logger.info("siliconflow.pricing_loaded total={} free={}", len(pricing_map), free_count)
            return pricing_map
        logger.warning("siliconflow.pricing_parse_empty")
        return None

    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        logger.warning("siliconflow.pricing_fetch_failed error={}", str(e))
        return None
    except Exception:
        logger.exception("model_discovery.unknown.unexpected_error")
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

            free = await _determine_free("openrouter", model_id, item)
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
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        logger.warning("discover.openrouter_failed error={}", str(e))
        return []
    except Exception:
        logger.exception("model_discovery.unknown.unexpected_error")
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
        sf_pricing = await _get_siliconflow_pricing()

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
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
        logger.warning("discover.siliconflow_failed error={}", str(e))
        return []
    except Exception:
        logger.exception("model_discovery._fetch_siliconflow_models.unexpected_error")
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


async def _build_local_ort_group(request: Request) -> dict:
    """构建本地 ORT GenAI chat 模型组（provider=local-ort）。

    已安装的本地 chat 模型（如 Phi-3 系列，经 ORT GenAI 在 CPU 运行）
    作为主模型选择之一展示。未安装任何 chat 模型时返回空组，
    由调用方决定是否注入结果。
    """
    core = getattr(request.app.state, "core", None)
    instances = getattr(core, "local_ai_instances", None) if core is not None else None
    registry = getattr(instances, "_model_registry", None)
    models = []
    if registry is not None:
        try:
            installed = await registry.list()
        except (OSError, RuntimeError, ValueError, AttributeError) as e:  # noqa: BLE001
            logger.warning("discover.local_ort_list_failed error={}", str(e))
            installed = []
        except Exception:
            logger.exception("model_discovery._build_local_ort_group.unexpected_error")
        for item in installed:
            purpose = getattr(item, "purpose", None)
            if purpose is None or str(purpose) != "chat":
                continue
            catalog_id = getattr(item, "catalog_id", "") or getattr(item, "id", "")
            if not catalog_id:
                continue
            models.append({
                "id": f"local:{catalog_id}",
                "display_name": catalog_id,
                "free": True,
                "tool_calling": False,
                "vision": False,
                "provider": "local-ort",
            })
    return {"provider": "local-ort", "label": "本地模型", "models": models}


# ── 获取所有已注册 provider 信息 ──────────────────────────────────


def _get_all_providers() -> list[dict]:
    """获取所有已注册的 provider 信息（从 config_service 动态读取）。

    返回列表，每项包含 id/label/format/base_url/api_key。
    """
    providers = []

    # 从 config_service 读取所有 provider
    try:
        from web._provider_keys import load_provider_key
        from web.config_service import get_config_service
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
    except (OSError, KeyError, ValueError, RuntimeError, ImportError) as e:
        logger.warning("discover.load_providers_failed error={}", str(e))
    except Exception:
        logger.exception("model_discovery._get_all_providers.unexpected_error")
    return providers


# ── GET /models/discover ──────────────────────────────────────────
# 注: invalidate_discovery_cache 已抽到 web._discovery_cache


@router.get("/models/discover", response_model=Envelope[list[dict]])
async def discover_models(request: Request) -> Any:
    """发现所有已注册 provider 的可用模型，结果缓存 30 分钟。

    自动发现所有已注册的 provider（包括内置 MiMo 和自定义 provider），
    通过 OpenAI 兼容的 /v1/models 接口获取模型列表。
    OpenRouter 和 SiliconFlow 有特殊处理逻辑，其他 provider 使用通用获取方法。
    每个模型标注 free（免费/付费）。
    已安装的本地 ORT GenAI chat 模型（provider=local-ort）不缓存，
    每次请求实时注入，安装/删除后立即生效。
    """
    now = time.time()
    spawn_refresh = False
    async with _cache_lock:
        cached = _cache["data"]
        fresh = cached is not None and (now - _cache["ts"]) < _CACHE_TTL
        stale = cached is not None  # 过期但存在的旧数据
        # 后台刷新去重：已有刷新在飞时不再 spawn，
        # 防 stale 窗口内 N 个请求触发 N 次外部 provider 全量抓取
        if stale and not fresh and not _cache["refreshing"]:
            _cache["refreshing"] = True
            spawn_refresh = True
    if fresh or stale:
        # 本地 ORT chat 组实时注入（不进缓存）：安装/删除后立即生效，
        # 与冷路径 _fetch_and_cache_discovered 尾部行为保持一致。
        # list() 拷贝防止把 local 组污染进 _cache["data"]。
        result = list(cached)
        local_group = await _build_local_ort_group(request)
        if local_group["models"]:
            result.append(local_group)
        if spawn_refresh:
            asyncio.get_running_loop().create_task(_refresh_cache_background(request))
        return Envelope(data=result)

    return await _fetch_and_cache_discovered(request)


async def _fetch_and_cache_discovered(request: Request) -> Any:
    """并发拉取全部 provider 的模型列表并写入缓存（首次冷启动路径）。"""
    now = time.time()
    if True:
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
        for i, (pid, models_or_exc) in enumerate(zip(provider_ids, results, strict=False)):
            if pid in BUILTIN_FALLBACK_MODELS:
                if isinstance(models_or_exc, Exception) or (isinstance(models_or_exc, list) and not models_or_exc):
                    results[i] = BUILTIN_FALLBACK_MODELS[pid]

        result = []
        for pid, models_or_exc in zip(provider_ids, results, strict=False):
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

        async with _cache_lock:
            _cache["data"] = result
            _cache["ts"] = now

    # 本地 ORT GenAI chat 组实时注入（不缓存，避免安装/删除后 30 分钟内不出现）
    local_group = await _build_local_ort_group(request)
    if local_group["models"]:
        result = [*result, local_group]
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

    try:
        validation = request.app.state.provider_service.validate_route(provider, model_id)
        if validation == "missing":
            raise ValueError(f"provider {provider} 不存在")
        if validation in {"disabled", "unavailable"}:
            raise ValueError(f"provider {provider} 当前不可用于路由")
        if validation == "model":
            raise ValueError(f"模型 {model_id} 不属于 provider {provider}")
        info = router_obj.set_chat_model(provider, model_id)
        logger.info("discover.chat_model_set provider={} model={}", provider, model_id)
        # 广播 config_changed WS 事件，通知前端刷新 Agent 模型选项
        try:
            from web.ws_hub import manager
            await manager.broadcast({
                "type": "config_changed",
                "payload": {"type": "chat_model", "provider": provider, "model_id": model_id},
            })
        except (OSError, RuntimeError, ValueError, ConnectionError) as e:
            logger.warning("discover.chat_model_broadcast_failed error={}", str(e))
        except Exception:
            logger.exception("model_discovery.set_chat_model.unexpected_error")
        return Envelope(data=info)
    except (ValueError, KeyError, RuntimeError, OSError) as e:
        logger.error("discover.set_chat_model_failed error={}", str(e))
        return Envelope(ok=False, error={"code": "set_failed", "message": str(e)})
    except Exception as e:
        logger.exception("model_discovery.set_chat_model.unexpected_error")
        return Envelope(ok=False, error={"code": "set_failed", "message": str(e)})

async def _refresh_cache_background(request: Request) -> None:
    """后台静默刷新 discover 缓存（stale-while-revalidate 的 revalidate 半边）。

    request 在 spawn 点捕获（仅用于取 app.state 单例，请求体早已读完，
    fire-and-forget 生命周期内 app 引用有效）。
    """
    try:
        await _fetch_and_cache_discovered(request)
    except Exception as e:  # noqa: BLE001
        from loguru import logger as _lg
        _lg.debug("models.discover_bg_refresh_failed error={}", str(e)[:150])
    finally:
        async with _cache_lock:
            _cache["refreshing"] = False
