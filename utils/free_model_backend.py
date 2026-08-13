"""LLM 功能节点后端切换器 —— 供内在世界各 LLM 调用点复用。

backend 语义（与 web.local_deploy_nodes 的 generative 节点一致）：
- local：走本地部署的对话模型（local-ort transport → LocalChatService，全局共享一个实例）
- api/auto：走远程免费模型（硅基流动），调用方失败时降级主 LLM
- off：关闭（不调用任何后端）

设计目标：让内在世界（情绪/画像/梦境/成长/回忆/蒸馏/本能等）的 LLM 调用点
都能在 WebUI 上选择「本地模型」vs「远程免费模型」，复用同一套切换逻辑，
避免各模块重复实现 httpx 调用。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from loguru import logger

from utils.http_pool import get_shared_client

DEFAULT_FREE_MODEL = "THUDM/GLM-4-9B-0414"  # 非思考模型，避免思考碎片污染结构化输出
DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"

_VALID_BACKENDS = ("auto", "local", "api", "off")


class FreeModelBackend:
    """免费模型后端：持有 API key / 模型 / 后端选择，提供 call() 供各模块复用。"""

    def __init__(self, model: str = DEFAULT_FREE_MODEL,
                 base_url: str = DEFAULT_BASE_URL,
                 backend: str = "auto") -> None:
        self._model = model
        self._base_url = base_url
        self._api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        self._backup_key = ""
        self._router = None
        self._local_model = None
        self._backend = backend if backend in _VALID_BACKENDS else "auto"
        if self._backend in ("local", "off"):
            # 启动即 local：暂存 key，禁用免费模型，走本地模型 / 关闭
            self._backup_key = self._api_key
            self._api_key = ""

    def set_backend(self, backend: str) -> None:
        """热切换后端。local=本地模型；api/auto=免费模型；off=关闭。"""
        if backend not in _VALID_BACKENDS:
            return
        self._backend = backend
        if backend in ("local", "off"):
            if self._api_key:
                self._backup_key = self._api_key
                self._api_key = ""
        else:
            if self._backup_key and not self._api_key:
                self._api_key = self._backup_key

    def set_router(self, router: Any) -> None:
        """注入 ModelRouter，供 local 后端通过 local-ort transport 走本地模型。"""
        self._router = router

    def set_local_model(self, model_id: str | None) -> None:
        """指定本地模型（功能节点独立选模型）；为 None 时回退全局共享实例。"""
        self._local_model = model_id or None

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def api_available(self) -> bool:
        """免费模型是否可用（有 key 且非 local/off）。"""
        return bool(self._api_key) and self._backend not in ("local", "off")

    async def call(self, messages: list[dict], temperature: float = 0.6,
                   max_tokens: int = 800, timeout: float = 15.0) -> str | None:
        """按当前后端调用：local=本地模型；api/auto=免费模型。失败返回 None，由调用方兜底。"""
        if self._backend == "local":
            return await self._call_local(messages, temperature, max_tokens)
        if not self.api_available:
            return None
        try:
            client = get_shared_client()
            response = await client.post(
                f"{self._base_url}/chat/completions",
                json={
                    "model": self._model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(timeout),
            )
            response.raise_for_status()
            data = response.json()
            choices = data.get("choices", [])
            if not choices:
                return None
            return choices[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.debug("free_model.call_failed",
                         error=str(e)[:200], error_type=type(e).__name__)
            return None

    async def _call_local(self, messages: list[dict], temperature: float,
                          max_tokens: int) -> str | None:
        """调用本地对话模型（local-ort transport → LocalChatService）。"""
        return await call_local_model(
            self._router, messages, temperature, max_tokens, model_id=self._local_model
        )


async def call_local_model(router: Any, messages: list[dict], temperature: float,
                           max_tokens: int, timeout: float = 60.0,
                           model_id: str | None = None) -> str | None:
    """通过 router 的 local-ort transport 调用本地对话模型。

    model_id 指定时按该模型定位运行实例（功能节点独立选模型）；为 None 时
    回退到全局共享实例。本地模型不可用（未启动 / 未选中）时返回 None，
    由调用方按既有降级链兜底，不在此处静默切换云端。
    """
    if router is None:
        return None
    model = model_id or "local-chat"
    try:
        result = await asyncio.wait_for(
            router.route_config(
                {"client": "local-ort", "model": model},
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
            ),
            timeout=timeout,
        )
        return result if isinstance(result, str) else None
    except Exception as e:
        logger.warning("local_model.call_failed",
                       error=str(e)[:200], error_type=type(e).__name__)
        return None
