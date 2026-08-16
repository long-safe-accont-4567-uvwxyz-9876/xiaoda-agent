"""ModelRouter 的客户端生命周期/凭证轮换 Mixin — 自 model_router.py 拆分（Phase 3）。

内容：per-provider 凭证锁、凭证池主动注册、自定义 provider 懒注册、
refresh_client（Setup 保存新 Key 后重建客户端）、_ensure_credential_in_pool、
_select_client_for_provider（客户端选择 + 懒恢复）、_rotate_credential_on_error
（错误驱动的凭证轮换）。方法体自 model_router.py 逐字节搬移，仅缩进调整。

兼容契约（tests/test_client_lifecycle_mixin.py）：
    - 本模块不得 import model_router（防循环依赖）
    - ModelRouter(ClientLifecycleMixin) 继承保持 self 语义，
      `patch("model_router.ModelRouter.refresh_client")` 等用法不受影响
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from loguru import logger
from openai import AsyncOpenAI

from config import AGNES_BASE_URL
from config import get_builtin_providers as _get_builtin_providers
from transports.agnes_transport import _get_agnes_http_client, AGNES_HTTP_TIMEOUT
from utils.common import mask_api_key as _mask_api_key
from core.app_exception import LLMError
from core.error_codes import ErrorCodeEnum
from model_router_config import (
    MIMO_BASE_URL,
    _resolve_provider_key,
)
from security.ssrf_guard import validate_url as _ssrf_validate_url


def _ssrf_check(url: str) -> None:
    """SSRF 防护：5步法校验 base_url 安全性（best-effort，本地 provider 如 Ollama 校验失败仅告警不阻塞）"""
    try:
        ok, reason = _ssrf_validate_url(url)
        if not ok:
            logger.warning("router.ssrf_blocked url={} reason={}", url, reason)
    except (ValueError, OSError) as e:
        logger.debug("router.ssrf_check_skip url={} error={}", url, str(e))


class ClientLifecycleMixin:
    """凭证轮换与客户端生命周期管理（ModelRouter 组合此 Mixin）。"""

    def _get_credential_lock(self, provider: str) -> asyncio.Lock:
        """返回指定 provider 的凭证锁，按需创建。

        不同 provider 之间不再互相阻塞，相同 provider 仍然串行化以保护凭证轮换。
        """
        return self._credential_locks.setdefault(provider, asyncio.Lock())

    def _register_credential_pool_providers(self) -> None:
        """从凭证池主动注册非 mimo/agnes 的 Provider 到 _custom_clients。

        确保本地 Provider（如 Ollama）和免费平台（如 SiliconFlow）在路由器
        初始化时即被注册，不依赖 Web 服务的 _register_env_providers 流程。
        """
        try:
            from web.custom_providers import register_into_router
        except ImportError:
            logger.debug("router.credential_pool_register_skip web module unavailable")
            return
        _BUILTIN_PROVIDERS = set(_get_builtin_providers())
        _PROVIDER_FORMAT = {
            "ollama": "openai",
            "llama.cpp": "openai",
            "siliconflow": "openai",
            "openrouter": "openai",
            "modelscope": "openai",
        }
        pool = self._credential_pool
        for provider, creds in pool._pool.items():
            if provider in _BUILTIN_PROVIDERS:
                continue
            if self.has_custom_client(provider):
                continue
            if not creds:
                continue
            cred = creds[0]
            fmt = _PROVIDER_FORMAT.get(provider, "openai")
            if cred.base_url and cred.api_key:
                register_into_router(self, provider, fmt, cred.base_url, cred.api_key)
                logger.info("router.credential_pool_registered provider={} format={}", provider, fmt)

    def _lazy_register_provider(self, provider: str) -> None:
        """懒注册：从 config_service 恢复未注册的自定义 provider。"""
        try:
            from web.config_service import get_config_service
            from web._provider_keys import load_provider_key
            from web.custom_providers import register_into_router
            cfg = get_config_service()
            record = cfg.get(f"models.providers.{provider}")
            if record:
                api_key = load_provider_key(provider)
                if api_key:
                    register_into_router(
                        self, provider,
                        record.get("format", "openai"),
                        record.get("base_url", ""),
                        api_key,
                    )
                    logger.info("router.lazy_registered provider={}", provider)
        except (ImportError, AttributeError, KeyError, ValueError) as e:
            logger.warning("router.lazy_register_failed provider={} error={}", provider, str(e))

    def refresh_client(self) -> None:
        """重建 MiMo / Agnes 客户端（Setup 保存新 Key 后调用）。

        ModelRouter.__init__ 只在启动时读取一次环境变量创建客户端，
        后续通过 Setup 页面保存的新 Key 不会自动生效。此方法从当前
        os.environ 重新读取 Key 并重建客户端，使新配置立即生效。
        """
        old_mimo = self._client  # 旧 MiMo 客户端（独立 httpx，替换后 close 释放连接）

        new_mimo_key = _resolve_provider_key("MIMO_API_KEY")
        new_mimo_url = os.getenv("MIMO_BASE_URL", MIMO_BASE_URL)
        if new_mimo_key:
            _ssrf_check(new_mimo_url)  # SSRF 防护：校验 base_url
            self._client = AsyncOpenAI(api_key=new_mimo_key, base_url=new_mimo_url)
            logger.info("router.mimo_client_refreshed",
                        key_len=len(new_mimo_key),
                        key_hash=_mask_api_key(new_mimo_key))
        else:
            self._client = None

        new_agnes_key = os.getenv("AGNES_API_KEY", "")
        new_agnes_url = os.getenv("AGNES_BASE_URL", AGNES_BASE_URL)
        if new_agnes_key:
            _ssrf_check(new_agnes_url)  # SSRF 防护：校验 base_url
            self._agnes_client = AsyncOpenAI(
                api_key=new_agnes_key,
                base_url=new_agnes_url,
                http_client=_get_agnes_http_client(),
                timeout=AGNES_HTTP_TIMEOUT,
                max_retries=0,
            )
            logger.info("router.agnes_client_refreshed",
                        key_len=len(new_agnes_key),
                        key_hash=_mask_api_key(new_agnes_key))
        else:
            self._agnes_client = None

        # 关闭旧客户端释放连接
        # CodeRabbit 修复：old_agnes 注入了共享 httpx client（_get_agnes_http_client），
        # 调用其 .close() 会连带关闭共享 client，影响新 self._agnes_client（同样复用共享
        # client）。共享 client 生命周期归 agnes_transport 模块统一管理（close_agnes_shared_client），
        # 这里只关闭独立的 old_mimo（未注入共享 httpx，SDK 自建 client）。
        _old_clients: list = []
        if old_mimo is not None and old_mimo is not self._client:
            _old_clients.append(old_mimo)
        if _old_clients:
            try:
                import asyncio
                loop = asyncio.get_running_loop()

                async def _close_old() -> None:
                    await asyncio.gather(
                        *[c.close() for c in _old_clients],
                        return_exceptions=True,
                    )

                # 同类副作用修复：裸 create_task 无强引用会被 GC 回收导致
                # 旧客户端未关闭（连接泄漏）。用 _spawn 跟踪。
                from core.background_tasks import _spawn
                _spawn(_close_old())
            except RuntimeError:
                # 同步调用路径无运行事件循环，跳过异步关闭旧客户端（正常降级）
                logger.debug("router.close_old_clients_skipped_no_loop", exc_info=True)

        # 同步更新凭证池：确保 MiMo/Agnes 凭证与当前环境变量一致
        try:
            from utils.credential_pool import get_credential_pool
            pool = get_credential_pool()
            # 补充/更新 MiMo 凭证
            if new_mimo_key:
                self._ensure_credential_in_pool(pool, "mimo", new_mimo_key, new_mimo_url)
            # 补充/更新 Agnes 凭证
            if new_agnes_key:
                self._ensure_credential_in_pool(pool, "agnes", new_agnes_key, new_agnes_url)
        except (KeyError, ValueError, AttributeError) as e:
            logger.warning("router.credential_pool_sync_failed error={}", str(e))

    @staticmethod
    def _ensure_credential_in_pool(pool: Any, provider: str, api_key: str, base_url: str) -> None:
        """确保凭证池中有该 provider 的最新凭证。"""
        from utils.credential_pool import Credential
        existing = pool._pool.get(provider, [])
        already_exists = any(c.api_key == api_key for c in existing)
        if not already_exists:
            pool.add_credential(Credential(
                api_key=api_key,
                provider=provider,
                base_url=base_url,
            ))

    async def _select_client_for_provider(self, provider: str) -> Any:
        """选择指定 provider 的客户端（含懒注册和凭证锁）。无可用客户端时 raise LLMError。

        P0 修复（cannot read image 根因）：
        - refresh_client() 在凭证轮换时可能把 self._client / self._agnes_client 置 None
          （例如 Setup 页面保存空 Key、或并发刷新时 env var 暂时为空）。
        - 原实现直接 raise E_LLM006，导致 _describe_images 拿不到 client → "cannot read image"。
        - 修复：在锁内做"懒恢复"——若 client 为 None，从当前 os.environ 重新读取 Key 重建。
          仍无 Key 才 raise。这样凭证池/环境变量恢复后无需重启即可自愈。
        """
        lock = self._get_credential_lock(provider)
        async with lock:
            client = self._client
            if provider == "agnes":
                client = self._agnes_client
                # P0：agnes client 懒恢复（防止 refresh_client 把它置 None 后无法自愈）
                if client is None:
                    # 优先检查 _custom_clients["agnes"]（用户通过 WebUI 注册的 agnes 客户端）
                    # 根因：旧实现直接走 env var 懒恢复，会绕过 _custom_clients["agnes"]
                    # 导致用户通过 WebUI 添加 agnes 后，调用仍走 env var 创建的新客户端，
                    # 而非用户注册的客户端（用户配置的 base_url/api_key 不生效）。
                    _custom_agnes = self.get_custom_client("agnes")
                    if _custom_agnes is not None:
                        client = _custom_agnes
                    else:
                        _agnes_key = os.getenv("AGNES_API_KEY", "")
                        _agnes_url = os.getenv("AGNES_BASE_URL", AGNES_BASE_URL)
                        if _agnes_key:
                            try:
                                _ssrf_check(_agnes_url)
                                self._agnes_client = AsyncOpenAI(
                                    api_key=_agnes_key,
                                    base_url=_agnes_url,
                                    http_client=_get_agnes_http_client(),
                                    timeout=AGNES_HTTP_TIMEOUT,
                                    max_retries=0,
                                )
                                client = self._agnes_client
                                logger.info("router.agnes_client_lazy_recovered",
                                            key_hash=_mask_api_key(_agnes_key))
                            except (ValueError, OSError) as ce:
                                logger.warning("router.agnes_client_lazy_recover_failed",
                                               error=str(ce))
            # N-2 修复收尾：内置 provider 集合从 provider_metadata.json 派生，不硬编码
            # （line 20 已 import _get_builtin_providers，line 686/1319 同款用法）
            elif provider not in _get_builtin_providers():
                custom = self.get_custom_client(provider)
                if custom is None:
                    # 懒注册：从 config_service 恢复未注册的自定义 provider
                    self._lazy_register_provider(provider)
                    custom = self.get_custom_client(provider)
                if custom is None:
                    raise LLMError(
                        f"自定义 provider {provider} 未注册或缺少 API Key",
                        error_code=ErrorCodeEnum.E_LLM006,
                    )
                client = custom
            else:
                # provider == "mimo"
                # P0：mimo client 懒恢复（防止 refresh_client 把它置 None 后 vision API 全挂）
                if client is None:
                    _mimo_key = _resolve_provider_key("MIMO_API_KEY")
                    _mimo_url = os.getenv("MIMO_BASE_URL", MIMO_BASE_URL)
                    if _mimo_key:
                        try:
                            _ssrf_check(_mimo_url)
                            self._client = AsyncOpenAI(
                                api_key=_mimo_key, base_url=_mimo_url)
                            client = self._client
                            logger.info("router.mimo_client_lazy_recovered",
                                        key_hash=_mask_api_key(_mimo_key))
                        except (ValueError, OSError) as ce:
                            logger.warning("router.mimo_client_lazy_recover_failed",
                                           error=str(ce))
        if not client:
            raise LLMError(
                f"{provider} client not initialized, check API_KEY env var",
                error_code=ErrorCodeEnum.E_LLM006,
            )
        return client

    async def _rotate_credential_on_error(self, provider: str, classified: Any) -> None:
        """当 ErrorClassifier 建议轮换凭证时，尝试获取新凭证并更新客户端。"""
        new_cred = await self._credential_pool.get_credential(provider)
        rotate_lock = self._get_credential_lock(provider)
        async with rotate_lock:
            current_key = ""
            if provider == "mimo" and self._client:
                current_key = self._client.api_key or ""
            elif provider == "agnes" and self._agnes_client:
                current_key = self._agnes_client.api_key or ""
            if new_cred and new_cred.api_key != current_key:
                logger.info("router.credential_rotated",
                            provider=provider,
                            key_len=len(new_cred.api_key),
                            key_hash=_mask_api_key(new_cred.api_key))
                # 更新客户端使用新凭证
                # agnes 复用共享 httpx client + connect=15s 配置（根因修复）；
                # mimo 保持默认（不在本次 APIConnectionError 根因范围）
                _new_base = new_cred.base_url or (MIMO_BASE_URL if provider == "mimo" else AGNES_BASE_URL)
                if provider == "agnes":
                    new_client = AsyncOpenAI(
                        api_key=new_cred.api_key,
                        base_url=_new_base,
                        http_client=_get_agnes_http_client(),
                        timeout=AGNES_HTTP_TIMEOUT,
                        max_retries=0,
                    )
                else:
                    new_client = AsyncOpenAI(
                        api_key=new_cred.api_key,
                        base_url=_new_base,
                    )
                if provider == "mimo":
                    self._client = new_client
                else:
                    self._agnes_client = new_client
