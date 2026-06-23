"""模型与凭证路由（R4/R13）：provider CRUD、路由表热改、凭证池状态、用量统计。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user
from web.routers.model_discovery import invalidate_discovery_cache

router = APIRouter(tags=["models"], dependencies=[Depends(get_current_user)])


def _get_cred_dir() -> Path:
    from config import get_credentials_dir
    return get_credentials_dir()

ROUTE_EDITABLE_FIELDS = {"model", "client", "max_tokens", "thinking", "timeout"}


def _mask(key: str) -> str:
    if not key:
        return ""
    return f"{key[:3]}***{key[-4:]}" if len(key) > 8 else "***"


def _key_file(provider_id: str) -> Path:
    safe = "".join(c for c in provider_id if c.isalnum() or c in "-_")
    return _get_cred_dir() / f"provider_{safe}.key"


def load_provider_key(provider_id: str) -> str:
    fp = _key_file(provider_id)
    if fp.exists():
        return fp.read_text(encoding="utf-8").strip()
    return ""


def _cfg(request: Request):
    from web.config_service import get_config_service
    return get_config_service()


def _router_of(request: Request):
    return request.app.state.core.router


async def _audit(request: Request, action: str, detail: str):
    core = request.app.state.core
    try:
        await core.db.insert_audit_log(f"webui.models.{action}", "webui", detail)
        await core.db.commit()
    except Exception:
        pass


async def _broadcast_changed():
    try:
        from web.ws_hub import manager
        await manager.broadcast({"type": "config_changed", "domain": "models"})
    except Exception:
        pass


# ── providers ────────────────────────────────────────────────────


def list_providers_data(cfg) -> list[dict]:
    out = []
    # MiMo 内置 provider — 只在有 API key 时显示（始终第一位，order=-1）
    mimo_key = os.getenv("MIMO_API_KEY", "")
    if mimo_key:
        out.append({"id": "mimo", "label": "小米 MiMo", "format": "openai",
                     "base_url": os.getenv("MIMO_BASE_URL", ""), "builtin": True,
                     "key_masked": _mask(mimo_key), "enabled": True})
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
            "builtin": False,
            "key_masked": _mask(key),
            "enabled": p.get("enabled", True),
            "default_model": p.get("default_model", ""),
            "order": p.get("order", 9999),
        })
    return out


@router.get("/models/providers", response_model=Envelope[list[dict]])
async def list_providers(request: Request):
    return Envelope(data=list_providers_data(_cfg(request)))


@router.post("/models/providers", response_model=Envelope[dict])
async def create_provider(body: dict, request: Request):
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
    cfg = _cfg(request)
    if pid in (cfg.get("models.providers", {}) or {}):
        raise HTTPException(400, f"provider {pid} 已存在")
    record = {
        "label": body.get("label", pid),
        "format": fmt,
        "base_url": base_url,
        "default_model": body.get("default_model", ""),
        "enabled": True,
    }
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key 不能为空")
    # 先注册客户端，成功后再持久化配置（避免部分失败状态）
    try:
        _save_key_and_register(request, pid, fmt, base_url, api_key)
    except Exception as e:
        logger.error("provider.register_failed id={} error={}", pid, str(e))
        raise HTTPException(500, f"provider 注册失败: {e}")
    cfg.set(f"models.providers.{pid}", record)
    await _audit(request, "provider.create", pid)
    invalidate_discovery_cache()
    await _broadcast_changed()
    return Envelope(data=dict(record, id=pid, key_masked=_mask(api_key), builtin=False))


@router.put("/models/providers/{pid}", response_model=Envelope[dict])
async def update_provider(pid: str, body: dict, request: Request):
    cfg = _cfg(request)
    record = cfg.get(f"models.providers.{pid}")
    if pid in ("mimo",) or not record:
        raise HTTPException(404 if not record else 400,
                            "内置 provider 不可修改" if record else f"provider {pid} 不存在")
    for f in ("label", "format", "base_url", "default_model", "enabled"):
        if f in body and body[f] is not None:
            record[f] = body[f]
    cfg.set(f"models.providers.{pid}", record)
    key = load_provider_key(pid)
    if key:
        _save_key_and_register(request, pid, record["format"], record["base_url"], key)
    await _audit(request, "provider.update", pid)
    invalidate_discovery_cache()
    await _broadcast_changed()
    return Envelope(data=dict(record, id=pid, key_masked=_mask(key), builtin=False))


@router.delete("/models/providers/{pid}", response_model=Envelope[dict])
async def delete_provider(pid: str, request: Request):
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "缺少 X-Confirm: yes 确认头")
    cfg = _cfg(request)
    if not cfg.get(f"models.providers.{pid}"):
        raise HTTPException(404, f"provider {pid} 不存在")
    # 检查是否有路由仍指向它
    from model_router import ROUTE_TABLE
    used_by = [t for t, c in ROUTE_TABLE.items() if c.get("client") == pid]
    if used_by:
        raise HTTPException(400, f"路由 {', '.join(used_by)} 仍指向该 provider，请先改路由")
    cfg.delete(f"models.providers.{pid}")
    _key_file(pid).unlink(missing_ok=True)
    from web.custom_providers import unregister_from_router
    unregister_from_router(_router_of(request), pid)
    await _audit(request, "provider.delete", pid)
    invalidate_discovery_cache()
    await _broadcast_changed()
    return Envelope(data={"deleted": pid})


def _save_key_and_register(request: Request, pid: str, fmt: str,
                           base_url: str, api_key: str):
    _get_cred_dir().mkdir(parents=True, exist_ok=True)
    fp = _key_file(pid)
    fp.write_text(api_key, encoding="utf-8")
    try:
        os.chmod(fp, 0o600)
    except OSError:
        pass
    from web.custom_providers import register_into_router
    register_into_router(_router_of(request), pid, fmt, base_url, api_key)


@router.post("/models/providers/{pid}/key", response_model=Envelope[dict])
async def set_provider_key(pid: str, body: dict, request: Request):
    api_key = (body.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "api_key 不能为空")
    cfg = _cfg(request)
    record = cfg.get(f"models.providers.{pid}")
    if not record:
        raise HTTPException(404, f"provider {pid} 不存在（内置 provider 的 key 走 .env）")
    _save_key_and_register(request, pid, record.get("format", "openai"),
                           record.get("base_url", ""), api_key)
    await _audit(request, "provider.key", pid)
    return Envelope(data={"id": pid, "key_masked": _mask(api_key)})


@router.post("/models/providers/reorder", response_model=Envelope[dict])
async def reorder_providers(body: dict, request: Request):
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
    invalidate_discovery_cache()
    await _broadcast_changed()
    logger.info("providers.reordered count={}", len(filtered))
    return Envelope(data={"ok": True})


# ── routes（任务路由表）──────────────────────────────────────────


@router.get("/models/routes", response_model=Envelope[dict])
async def list_routes(request: Request):
    from model_router import ROUTE_TABLE, FALLBACK_ROUTE
    routes = {}
    for task, c in ROUTE_TABLE.items():
        routes[task] = {
            "model": c.get("model", ""),
            "provider": c.get("client", "mimo"),
            "max_tokens": c.get("max_tokens", 1500),
            "thinking": bool(c.get("thinking")),
            "timeout": _router_of(request).TASK_TIMEOUTS.get(task),
        }
    return Envelope(data={"routes": routes, "fallback": dict(FALLBACK_ROUTE)})


@router.put("/models/routes/{task}", response_model=Envelope[dict])
async def update_route(task: str, body: dict, request: Request):
    from model_router import ROUTE_TABLE
    if task not in ROUTE_TABLE:
        raise HTTPException(404, f"未知路由任务 {task}")
    cfg = _cfg(request)
    provider = body.get("provider")
    if provider and provider not in ("mimo",) \
            and not cfg.get(f"models.providers.{provider}"):
        raise HTTPException(400, f"provider {provider} 不存在")
    entry = ROUTE_TABLE[task]
    if body.get("model"):
        entry["model"] = str(body["model"])
    if provider:
        entry["client"] = provider
    if body.get("max_tokens"):
        entry["max_tokens"] = max(64, min(int(body["max_tokens"]), 32768))
    if "thinking" in body:
        if body["thinking"]:
            entry["thinking"] = {"type": "enabled", "budget_tokens": 2048}
        else:
            entry.pop("thinking", None)
    if body.get("timeout"):
        _router_of(request).TASK_TIMEOUTS[task] = max(5, min(int(body["timeout"]), 600))
    # 持久化覆盖（重启后由 apply_model_overrides 恢复）
    cfg.set(f"models.routes.{task}", {
        "model": entry["model"], "client": entry.get("client", "mimo"),
        "max_tokens": entry.get("max_tokens"),
        "thinking": bool(entry.get("thinking")),
        "timeout": _router_of(request).TASK_TIMEOUTS.get(task),
    })
    await _audit(request, "route.update", json.dumps({task: body}, ensure_ascii=False))
    await _broadcast_changed()
    return Envelope(data={"task": task, "model": entry["model"],
                          "provider": entry.get("client", "mimo")})


@router.get("/models/chat-model", response_model=Envelope[dict])
async def get_chat_model(request: Request):
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
async def credentials_status():
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
        for pid, p in custom_providers.items():
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
            except Exception as e:
                logger.error(f"[credentials_status] pid={pid} error: {e}")
    except Exception as e:
        logger.error(f"[credentials_status] custom providers block error: {e}")
    return Envelope(data=out)


# ── 用量统计 ─────────────────────────────────────────────────────


@router.get("/models/usage", response_model=Envelope[dict])
async def usage(request: Request, days: int = Query(default=7, ge=1, le=90)):
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
