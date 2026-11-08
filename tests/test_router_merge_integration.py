"""合并语义回归（2026-11-08）：b4cc1daf 的内置客户端缓存与
7aaa64b7 的客户端物化/探活必须共用同一份客户端，且兜底矩阵判定
不能再把可解析凭据的内置 provider（deepseek 等）当成未配置。
"""
from __future__ import annotations

import pytest

from llm_gateway import client_lifecycle as cl_module
from llm_gateway.client_lifecycle import ClientLifecycleMixin
from model_router import ModelRouter


class _CacheRouter(ClientLifecycleMixin):
    """只装配客户端缓存所需最小状态的路由替身。"""

    def __init__(self):
        self._client = None
        self._agnes_client = None
        self._custom_clients = {}

    def has_custom_client(self, provider_id: str) -> bool:
        return provider_id in self._custom_clients


def test_moved_methods_are_the_ones_in_effect():
    assert issubclass(ModelRouter, ClientLifecycleMixin)
    assert ModelRouter.bind_builtin is ClientLifecycleMixin.bind_builtin
    assert ModelRouter.get_builtin_client is ClientLifecycleMixin.get_builtin_client
    assert ModelRouter._is_client_configured is ClientLifecycleMixin._is_client_configured


def test_builtin_bind_and_get_share_one_cache():
    router = _CacheRouter()
    client = object()
    assert router.bind_builtin("deepseek", client) is None
    assert router.get_builtin_client("deepseek") is client
    assert router._builtin_client_cache()["deepseek"] is client
    with pytest.raises(ValueError):
        router.bind_builtin("not-a-builtin", client)
    with pytest.raises(ValueError):
        router.get_builtin_client("not-a-builtin")


def test_builtin_provider_configured_follows_catalog_credentials(monkeypatch):
    router = _CacheRouter()
    monkeypatch.setattr(
        cl_module, "_provider_cred_env",
        lambda provider: ("DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-merge-test")
    assert router._is_client_configured("deepseek") is True
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert router._is_client_configured("deepseek") is False


def test_mimo_and_agnes_keep_legacy_fields():
    router = _CacheRouter()
    assert router._is_client_configured("mimo") is False
    client = object()
    router.bind_builtin("mimo", client)
    assert router.get_builtin_client("mimo") is client
    assert router._is_client_configured("mimo") is True
