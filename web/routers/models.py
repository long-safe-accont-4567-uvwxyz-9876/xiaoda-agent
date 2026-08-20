"""模型与凭证路由（R4/R13）：provider CRUD、路由表热改、凭证池状态、用量统计。"""
from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger

# 缓存与凭证读写抽到独立模块, 避免与 web.routers.model_discovery / model_router 互相导入
from web._discovery_cache import invalidate_discovery_cache
from web._provider_keys import (
    _mask,
    load_provider_key,
)
from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(tags=["models"], dependencies=[Depends(get_current_user)])


def _cfg(request: Request) -> Any:
    from web.config_service import get_config_service
    return get_config_service()


def _router_of(request: Request) -> Any:
    return request.app.state.core.router


async def _audit(request: Request, action: str, detail: str) -> None:
    core = request.app.state.core
    try:
        await core.db.insert_audit_log(f"webui.models.{action}", "webui", detail)
        await core.db.commit()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.debug("models.audit_failed: {}", exc, exc_info=True)
    except Exception:
        logger.exception("models._audit.unexpected_error")


async def _broadcast_changed() -> None:
    try:
        from web.ws_hub import manager
        await manager.broadcast({"type": "config_changed", "domain": "models"})
    except (OSError, RuntimeError, ValueError, ConnectionError) as exc:
        logger.debug("models.broadcast_failed: {}", exc, exc_info=True)
    except Exception:
        logger.exception("models._broadcast_changed.unexpected_error")
# ── providers ────────────────────────────────────────────────────


def list_providers_data(cfg: Any) -> list[dict]:
    out = []
    custom = cfg.get("models.providers", {}) or {}
    # 按 order 字段升序排列；未设置 order 的排在已设置之后，按字典插入顺序
    keys_order = list(custom.keys())
    sorted_custom = sorted(
        custom.items(),
        key=lambda kv: (kv[1].get("order", 9999), keys_order.index(kv[0]))
    )
    for pid, p in sorted_custom:
        key = load_provider_key(pid)
        # 没有 API key 的自定义 provider 不显示
        if not key:
            continue
        out.append({
            "id": pid,
            "label": p.get("label", pid),
            "format": p.get("format", "openai"),
            "base_url": p.get("base_url", ""),
            "builtin": p.get("builtin", False),
            "key_masked": _mask(key),
            "enabled": p.get("enabled", True),
            "default_model": p.get("default_model", ""),
            "order": p.get("order", 9999),
        })
    return out


@router.get("/models/providers", response_model=Envelope[list[dict]])
async def list_providers(request: Request) -> Any:
    return Envelope(data=list_providers_data(_cfg(request)))


@router.post("/models/providers", response_model=Envelope[dict])
async def create_provider(body: dict, request: Request) -> Any:
    pid = (body.get("id") or "").strip()
    fmt = body.get("format", "openai")
    base_url = (body.get("base_url") or "").strip()
    if not pid or not pid.replace("-", "_").isidentifier():
        raise HTTPException(400, "id 必须是合法标识符（字母/数字/-/_）")
    if pid in ("mimo",):
        raise HTTPException(400, "不能覆盖内置 provider")
    if fmt not in ("openai", "anthropic"):
        raise HTTPException(400, "format 必须是 openai 或 anthropic")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(400, "base_url 必须是 http(s) URL")
    from security.ssrf_guard import validate_url
    allowed, reason = validate_url(base_url)
    if not allowed:
        raise HTTPException(400, f"base_url 安全检查失败: {reason}")
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key 不能为空")
    try:
        definition = await request.app.state.provider_service.create(body, {"api_key": api_key})
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except (OSError, RuntimeError, ConnectionError) as e:
        from llm_gateway.provider_service import ProviderConnectionError
        if isinstance(e, ProviderConnectionError):
            raise HTTPException(422, str(e)) from None
        raise
    except Exception as e:
        logger.exception("models.create_provider.unexpected_error")
        raise HTTPException(422, str(e)) from None
    await _audit(request, "provider.create", pid)
    await invalidate_discovery_cache()
    await _broadcast_changed()
    record = request.app.state.provider_service._record(definition)
    return Envelope(data=dict(record, key_masked=_mask(api_key), builtin=False))


@router.put("/models/providers/{pid}", response_model=Envelope[dict])
async def update_provider(pid: str, body: dict, request: Request) -> Any:
    record = _cfg(request).get(f"models.providers.{pid}")
    if pid in ("mimo",) or not record:
        raise HTTPException(404 if not record else 400,
                            "内置 provider 不可修改" if record else f"provider {pid} 不存在")
    # base_url 变更时同样做 SSRF 校验（无通用 localhost 豁免，Item 3）
    if "base_url" in body and body["base_url"]:
        from security.ssrf_guard import validate_url
        _burl = str(body["base_url"]).strip()
        if not _burl.startswith(("http://", "https://")):
            raise HTTPException(400, "base_url 必须是 http(s) URL")
        allowed, reason = validate_url(_burl)
        if not allowed:
            raise HTTPException(400, f"base_url 安全检查失败: {reason}")
    credentials = {"api_key": body["api_key"]} if body.get("api_key") else None
    try:
        definition = await request.app.state.provider_service.update(pid, body, credentials)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except (OSError, RuntimeError, ConnectionError) as e:
        from llm_gateway.provider_service import ProviderConnectionError
        if isinstance(e, ProviderConnectionError):
            raise HTTPException(422, str(e)) from None
        raise
    except Exception as e:
        logger.exception("models.update_provider.unexpected_error")
        raise HTTPException(422, str(e)) from None
    key = load_provider_key(pid)
    await _audit(request, "provider.update", pid)
    await invalidate_discovery_cache()
    await _broadcast_changed()
    updated = request.app.state.provider_service._record(definition)
    return Envelope(data=dict(updated, key_masked=_mask(key), builtin=False))


@router.delete("/models/providers/{pid}", response_model=Envelope[dict])
async def delete_provider(pid: str, request: Request) -> Any:
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "缺少 X-Confirm: yes 确认头")
    try:
        await request.app.state.provider_service.delete(pid)
    except KeyError:
        raise HTTPException(404, f"provider {pid} 不存在") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except (OSError, RuntimeError, ConnectionError) as e:
        from llm_gateway.provider_service import ProviderInUseError
        if isinstance(e, ProviderInUseError):
            raise HTTPException(409, f"provider 正被路由使用: {e}") from None
        raise
    except Exception as e:
        logger.exception("models.delete_provider.unexpected_error")
        raise HTTPException(409, f"provider 正被路由使用: {e}") from None
    await _audit(request, "provider.delete", pid)
    await invalidate_discovery_cache()
    await _broadcast_changed()
    return Envelope(data={"deleted": pid})


@router.post("/models/providers/{pid}/key", response_model=Envelope[dict])
async def set_provider_key(pid: str, body: dict, request: Request) -> Any:
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key 不能为空")
    cfg = _cfg(request)
    record = cfg.get(f"models.providers.{pid}")
    if not record:
        raise HTTPException(404, f"provider {pid} 不存在（内置 provider 的 key 走 .env）")
    try:
        await request.app.state.provider_service.update(pid, record, {"api_key": api_key})
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except (OSError, RuntimeError, ConnectionError) as e:
        from llm_gateway.provider_service import ProviderConnectionError
        if isinstance(e, ProviderConnectionError):
            raise HTTPException(422, str(e)) from None
        raise
    except Exception as e:
        logger.exception("models.set_provider_key.unexpected_error")
        raise HTTPException(422, str(e)) from None
    await _audit(request, "provider.key", pid)
    return Envelope(data={"id": pid, "key_masked": _mask(api_key)})


@router.post("/models/providers/reorder", response_model=Envelope[dict])
async def reorder_providers(body: dict, request: Request) -> Any:
    order_list = body.get("order")
    if not isinstance(order_list, list):
        raise HTTPException(400, "order 必须是字符串数组")
    cfg = _cfg(request)
    custom = cfg.get("models.providers", {}) or {}
    # 忽略 mimo（内置 provider 不可重排序）
    filtered = [pid for pid in order_list if pid != "mimo"]
    # 仅更新列表中且实际存在的 provider；不在列表中的 provider 保留原 order 值
    for idx, pid in enumerate(filtered):
        if pid in custom:
            record = dict(custom[pid])
            record["order"] = idx
            cfg.set(f"models.providers.{pid}", record)
    await _audit(request, "provider.reorder", json.dumps(filtered, ensure_ascii=False))
    await invalidate_discovery_cache()
    await _broadcast_changed()
    logger.info("providers.reordered count={}", len(filtered))
    return Envelope(data={"ok": True})


# ── routes（任务路由表）──────────────────────────────────────────


@router.get("/models/routes", response_model=Envelope[dict])
async def list_routes(request: Request) -> Any:
    from model_router import FALLBACK_ROUTE, ROUTE_TABLE
    routes = {}
    for task, c in ROUTE_TABLE.items():
        routes[task] = {
            "model": c.get("model", ""),
            "provider": c.get("client", "mimo"),
            "max_tokens": c.get("max_tokens", 1500),
            "thinking": bool(c.get("thinking") and c["thinking"].get("type") == "enabled"),
            "timeout": _router_of(request).TASK_TIMEOUTS.get(task),
        }
    return Envelope(data={"routes": routes, "fallback": dict(FALLBACK_ROUTE)})


@router.put("/models/routes/{task}", response_model=Envelope[dict])
async def update_route(task: str, body: dict, request: Request) -> Any:
    from model_router import ROUTE_TABLE, ModelRouter
    if task not in ROUTE_TABLE:
        raise HTTPException(404, f"未知路由任务 {task}")
    cfg = _cfg(request)
    provider = body.get("provider")
    model_id = str(body["model"]) if body.get("model") else ""
    effective_model_id = model_id or ROUTE_TABLE[task].get("model", "")
    provider_service = getattr(request.app.state, "provider_service", None)
    if provider and provider_service is not None:
        validation = provider_service.validate_route(provider, effective_model_id)
        if validation == "missing":
            raise HTTPException(404, f"provider {provider} 不存在")
        if validation in {"disabled", "unavailable"}:
            raise HTTPException(409, f"provider {provider} 当前不可用于路由")
        if validation == "model":
            raise HTTPException(409, f"模型 {model_id} 不属于 provider {provider}")
    elif provider and provider not in ("mimo",) \
            and not cfg.get(f"models.providers.{provider}"):
        raise HTTPException(400, f"provider {provider} 不存在")

    router_obj = _router_of(request)
    registry = router_obj._registry
    current_entry = registry.get_task_ref(task) or {}

    # CodeRabbit#5 + m8 修复：走 registry.get_task_ref 而非直接读 ROUTE_TABLE[task]，
    # 保证 replace_table 后语义一致（虽然 replace_table 已保持对象身份，仍统一入口）。
    model_id = effective_model_id
    final_provider = provider or current_entry.get("client", "mimo")

    # CodeRabbit Nit: int 转换加 try/except 返回 400 而非让 ValueError 变成 500
    try:
        max_tokens = int(body["max_tokens"]) if body.get("max_tokens") else None
        timeout = int(body["timeout"]) if body.get("timeout") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "max_tokens/timeout 必须为整数") from None

    # CodeRabbit#14 + C3 修复：max_tokens clamp 用 PROVIDER_MAX_TOKENS_CAP 动态裁剪，
    # 不再硬编码 32768。旧实现把 chat 路由的 131072 压到 32768，严重退化为默认值。
    # _cap_max_tokens(provider) 返回该 provider 的上限（无 cap 时返回原值），下限保留 64。
    if max_tokens is not None:
        max_tokens = max(64, ModelRouter._cap_max_tokens(max_tokens, final_provider))

    thinking = None
    if "thinking" in body:
        # CR-Major-3 修复：budget_tokens 保留原 entry 的值，不硬编码 2048。
        # 旧实现恢复时硬编码 2048，导致重启后 thinking budget 减半。
        _orig_thinking = current_entry.get("thinking") or {}
        _orig_budget = (_orig_thinking.get("budget_tokens", 4096)
                        if isinstance(_orig_thinking, dict) else 4096)
        if body["thinking"]:
            thinking = {"type": "enabled", "budget_tokens": _orig_budget}
        else:
            thinking = {"type": "disabled"}
        import structlog
        structlog.get_logger().info("route.thinking_updated", task=task, thinking=thinking)
    # Qodo#4 修复：timeout 先 clamp 再传 registry，保证运行时与持久化用同一个验证值
    if timeout is not None:
        timeout = max(5, min(timeout, 600))

    extra_persist = None
    if task == "chat":
        extra_persist = {"models.chat_model": {"provider": final_provider, "model_id": model_id}}

    # 通过 Registry 原子化更新（内存 + 持久化，失败回滚）
    try:
        registry.update_route(
            task, model_id=model_id, provider=final_provider,
            max_tokens=max_tokens, thinking=thinking, timeout=timeout,
            extra_persist=extra_persist,
        )
    except KeyError as e:
        raise HTTPException(404, f"未知路由任务 {task}: {e}") from None
    except (OSError, ValueError, RuntimeError) as e:
        raise HTTPException(500, f"路由更新失败: {e}") from None
    except Exception as e:
        logger.exception("models.unknown.unexpected_error")
        raise HTTPException(500, f"路由更新失败: {e}") from None
    # Qodo#5 修复：TASK_TIMEOUTS 在 registry 持久化成功后才修改，
    # 失败时（上面抛 HTTPException）不修改运行时 timeout，保持原值
    if timeout is not None:
        router_obj.TASK_TIMEOUTS[task] = timeout

    await _audit(request, "route.update", json.dumps({task: body}, ensure_ascii=False))
    await _broadcast_changed()
    final_entry = registry.get_task_ref(task) or {}
    return Envelope(data={"task": task, "model": final_entry["model"],
                          "provider": final_entry.get("client", "mimo")})


@router.get("/models/chat-model", response_model=Envelope[dict])
async def get_chat_model(request: Request) -> Any:
    cfg = _cfg(request)
    # 优先从 config_service 的 models.chat_model 读取（如果存在）
    chat_model = cfg.get("models.chat_model")
    if isinstance(chat_model, dict) and chat_model.get("provider") \
            and chat_model.get("model_id"):
        return Envelope(data={"provider": chat_model["provider"],
                              "model_id": chat_model["model_id"]})
    # 否则从 model_router.ROUTE_TABLE["chat"] 读取
    from model_router import ROUTE_TABLE
    chat_route = ROUTE_TABLE.get("chat", {})
    return Envelope(data={
        "provider": chat_route.get("client", "mimo"),
        "model_id": chat_route.get("model", ""),
    })


# ── 凭证池状态 ───────────────────────────────────────────────────


@router.get("/models/credentials/status", response_model=Envelope[list[dict]])
async def credentials_status() -> Any:
    from utils.credential_pool import get_credential_pool
    pool = get_credential_pool()
    out = []
    for provider, creds in getattr(pool, "_pool", {}).items():
        for i, c in enumerate(creds):
            out.append({
                "provider": provider,
                "index": i,
                "key_masked": _mask(c.api_key),
                "state": c.state.value,
                "last_error": c.last_error,
                "use_count": c.use_count,
                "error_count": c.error_count,
                "last_used_at": c.last_used_at,
            })
    # 也包含自定义 provider 的 key 状态
    from web.config_service import get_config_service as _get_cfg
    try:
        cfg = _get_cfg()
        custom_providers = cfg.get("models.providers", {}) or {}
        for pid in custom_providers:
            try:
                key = load_provider_key(pid)
                if not key:
                    continue
                # 避免和 credential_pool 中已有的重复
                if any(o["provider"] == pid for o in out):
                    continue
                out.append({
                    "provider": pid,
                    "index": 0,
                    "key_masked": _mask(key),
                    "state": "ok",
                    "last_error": None,
                    "use_count": 0,
                    "error_count": 0,
                    "last_used_at": None,
                })
            except (OSError, ValueError, RuntimeError) as e:
                logger.error("[credentials_status] pid={} error: {}", pid, e)
            except Exception:
                logger.exception("models.credentials_status.unexpected_error")
    except (OSError, ValueError, RuntimeError) as e:
        logger.error("[credentials_status] custom providers block error: {}", e, exc_info=True)
    except Exception:
        logger.exception("models.unknown.unexpected_error")
    return Envelope(data=out)


# ── Temperature 配置 ─────────────────────────────────────────────


@router.get("/models/temperature", response_model=Envelope[dict])
async def get_temperature(request: Request) -> Any:
    """获取当前 temperature 设置（优先 webui_overrides，回退 agent.json5）。"""
    cfg = _cfg(request)
    override = cfg.get("models.temperature")
    from config import AGENT_CONFIG
    default = AGENT_CONFIG.get("model", {}).get("temperature", 0.7)
    value = override if override is not None else default
    return Envelope(data={"temperature": value, "source": "override" if override is not None else "config"})


@router.put("/models/temperature", response_model=Envelope[dict])
async def set_temperature(request: Request) -> Any:
    """设置 temperature（0.0-2.0），写入 webui_overrides.json 热生效。"""
    body = await request.json()
    value = body.get("temperature")
    if value is None or not isinstance(value, (int, float)):
        raise HTTPException(400, "temperature must be a number")
    value = round(float(value), 2)
    if not (0.0 <= value <= 2.0):
        raise HTTPException(400, "temperature must be between 0.0 and 2.0")
    cfg = _cfg(request)
    cfg.set("models.temperature", value)
    await _audit(request, "temperature.set", f"temperature={value}")
    await _broadcast_changed()
    return Envelope(data={"temperature": value})


@router.get("/models/frequency_penalty", response_model=Envelope[dict])
async def get_frequency_penalty(request: Request) -> Any:
    """获取当前 frequency_penalty 设置（优先 webui_overrides，回退默认 1.0）。"""
    cfg = _cfg(request)
    override = cfg.get("models.frequency_penalty")
    default = 1.0
    value = override if override is not None else default
    return Envelope(data={"frequency_penalty": value, "source": "override" if override is not None else "default"})


@router.put("/models/frequency_penalty", response_model=Envelope[dict])
async def set_frequency_penalty(request: Request) -> Any:
    """设置 frequency_penalty（0.0-2.0），写入 webui_overrides.json 热生效。"""
    body = await request.json()
    value = body.get("frequency_penalty")
    if value is None or not isinstance(value, (int, float)):
        raise HTTPException(400, "frequency_penalty must be a number")
    value = round(float(value), 2)
    if not (0.0 <= value <= 2.0):
        raise HTTPException(400, "frequency_penalty must be between 0.0 and 2.0")
    cfg = _cfg(request)
    cfg.set("models.frequency_penalty", value)
    await _audit(request, "frequency_penalty.set", f"frequency_penalty={value}")
    await _broadcast_changed()
    return Envelope(data={"frequency_penalty": value})


@router.get("/models/presence_penalty", response_model=Envelope[dict])
async def get_presence_penalty_api(request: Request) -> Any:
    """获取当前 presence_penalty 设置（优先 webui_overrides，回退默认 1.0）。"""
    cfg = _cfg(request)
    override = cfg.get("models.presence_penalty")
    default = 1.0
    value = override if override is not None else default
    return Envelope(data={"presence_penalty": value, "source": "override" if override is not None else "default"})


@router.put("/models/presence_penalty", response_model=Envelope[dict])
async def set_presence_penalty_api(request: Request) -> Any:
    """设置 presence_penalty（0.0-2.0），写入 webui_overrides.json 热生效。"""
    body = await request.json()
    value = body.get("presence_penalty")
    if value is None or not isinstance(value, (int, float)):
        raise HTTPException(400, "presence_penalty must be a number")
    value = round(float(value), 2)
    if not (0.0 <= value <= 2.0):
        raise HTTPException(400, "presence_penalty must be between 0.0 and 2.0")
    cfg = _cfg(request)
    cfg.set("models.presence_penalty", value)
    await _audit(request, "presence_penalty.set", f"presence_penalty={value}")
    await _broadcast_changed()
    return Envelope(data={"presence_penalty": value})


# ── 用量统计 ─────────────────────────────────────────────────────


@router.get("/models/usage", response_model=Envelope[dict])
async def usage(request: Request, days: int = Query(default=7, ge=1, le=90)) -> Any:
    core = request.app.state.core
    since = time.time() - days * 86400
    rows = await core.db.fetch_all(
        "SELECT date(created_at, 'unixepoch', 'localtime') AS day, model, "
        "SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens, "
        "SUM(cost_usd) AS cost_usd, COUNT(*) AS calls "
        "FROM api_usage WHERE created_at > ? GROUP BY day, model ORDER BY day",
        (since,))
    total = await core.db.fetch_one(
        "SELECT SUM(cost_usd) AS cost, SUM(prompt_tokens + completion_tokens) AS tokens, "
        "COUNT(*) AS calls FROM api_usage WHERE created_at > ?", (since,))
    return Envelope(data={"days": days, "series": rows, "total": total or {}})