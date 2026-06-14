from __future__ import annotations

import shutil
from typing import Any

from fastapi import APIRouter
from loguru import logger

from web.schemas import Envelope

router = APIRouter(tags=["setup"])


@router.get("/setup/first-run", response_model=Envelope[dict])
async def get_first_run():
    """检测是否首次运行（.env 不存在或 MIMO_API_KEY 为空）。"""
    from setup_wizard import is_first_run
    return Envelope(data={"first_run": is_first_run()})


@router.get("/setup/keys", response_model=Envelope[dict])
async def get_keys():
    """返回所有 Key 的配置状态（脱敏）。"""
    from setup_wizard import REQUIRED_KEYS, OPTIONAL_KEYS, _load_env_values, _mask_value

    current = _load_env_values()
    keys: list[dict[str, Any]] = []

    for item in REQUIRED_KEYS:
        key = item["key"]
        val = current.get(key, "")
        keys.append({
            "key": key,
            "label": item["label"],
            "desc": item["desc"],
            "url": item.get("url", ""),
            "url_desc": item.get("url_desc", ""),
            "required": True,
            "configured": bool(val.strip()),
            "masked_value": _mask_value(val) if val else "",
        })

    for item in OPTIONAL_KEYS:
        key = item["key"]
        val = current.get(key, "")
        keys.append({
            "key": key,
            "label": item["label"],
            "desc": item["desc"],
            "url": item.get("url", ""),
            "url_desc": item.get("url_desc", ""),
            "required": False,
            "configured": bool(val.strip()),
            "masked_value": _mask_value(val) if val else "",
        })

    return Envelope(data={"keys": keys})


@router.post("/setup/keys", response_model=Envelope[dict])
async def save_keys(body: dict):
    """将提供的 Key-Value 写入 .env 文件。"""
    from setup_wizard import (
        ENV_PATH,
        ENV_EXAMPLE_PATH,
        _parse_env_lines,
        _write_env,
        _load_env_values,
    )

    updates = body.get("keys")
    if not updates or not isinstance(updates, dict):
        return Envelope(ok=False, error={"code": "INVALID_BODY", "message": "需要提供 keys 字段（dict）"})

    # 如果 .env 不存在，从 .env.example 复制
    import os
    if not os.path.exists(ENV_PATH):
        if os.path.exists(ENV_EXAMPLE_PATH):
            shutil.copy2(ENV_EXAMPLE_PATH, ENV_PATH)
            logger.info("setup.copied_env_example")
        else:
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write("")
            logger.info("setup.created_empty_env")

    existing_lines = _parse_env_lines(ENV_PATH)
    current = _load_env_values()
    merged = dict(current)
    merged.update(updates)
    _write_env(existing_lines, merged)

    # 自动注册免费模型平台为自定义 Provider
    _auto_register_providers(updates)

    logger.info("setup.keys_saved count={}", len(updates))
    return Envelope(data={"saved": list(updates.keys())})


# 已知免费模型平台 → Provider 映射
_KNOWN_PROVIDERS = {
    "SILICONFLOW_API_KEY": {
        "id": "siliconflow",
        "label": "SiliconFlow 硅基流动",
        "format": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
    },
    "OPENROUTER_API_KEY": {
        "id": "openrouter",
        "label": "OpenRouter",
        "format": "openai",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "MODELSCOPE_ACCESS_TOKEN": {
        "id": "modelscope",
        "label": "ModelScope 魔搭",
        "format": "openai",
        "base_url": "https://api-inference.modelscope.cn/v1",
    },
    "AGNES_API_KEY": {
        "id": "agnes",
        "label": "Agnes AI",
        "format": "openai",
        "base_url": "https://api.agnes-ai.com/v1",
    },
}


def _auto_register_providers(updates: dict) -> None:
    """当用户配置了免费模型平台的 Key，自动注册为自定义 Provider。"""
    import os
    from web.config_service import get_config_service
    from web.custom_providers import register_into_router

    cfg = get_config_service()
    existing = cfg.get("models.providers", {}) or {}

    for env_key, provider_info in _KNOWN_PROVIDERS.items():
        api_key = updates.get(env_key, "").strip()
        if not api_key:
            continue

        pid = provider_info["id"]

        # 写入凭证文件
        from web.routers.models import _key_file
        from pathlib import Path
        cred_dir = Path(__file__).resolve().parent.parent.parent / "credentials"
        cred_dir.mkdir(parents=True, exist_ok=True)
        fp = cred_dir / f"provider_{pid}.key"
        fp.write_text(api_key, encoding="utf-8")
        try:
            os.chmod(fp, 0o600)
        except OSError:
            pass

        # 注册到配置（如果尚未存在）
        if pid not in existing:
            record = {
                "label": provider_info["label"],
                "format": provider_info["format"],
                "base_url": provider_info["base_url"],
                "default_model": "",
                "enabled": True,
            }
            cfg.set(f"models.providers.{pid}", record)
            logger.info("setup.auto_provider_registered id={}", pid)

        # 注册到运行时 router
        try:
            from model_router import ModelRouter
            # 尝试获取 router 实例
            import web.server as srv
            # 延迟导入，server 可能还在初始化
        except Exception:
            pass

        # 通过 app.state 注册（如果 app 已启动）
        try:
            from web.server import app
            if hasattr(app, "state") and hasattr(app.state, "core"):
                router_obj = app.state.core.router
                register_into_router(
                    router_obj, pid,
                    provider_info["format"],
                    provider_info["base_url"],
                    api_key,
                )
                logger.info("setup.auto_provider_runtime id={}", pid)
        except Exception as e:
            logger.debug("setup.auto_provider_runtime_skip error={}", str(e))
