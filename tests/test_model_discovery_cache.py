"""discover_models 缓存路径守卫：local-ort 实时注入 + 后台刷新去重。

根因案例（2026-08-25 review 发现）：_fetch_and_cache_discovered 先写缓存
再注入 local 组，导致 TTL 内所有缓存命中响应丢失 provider=local-ort 组，
违背"每次请求实时注入"承诺；stale 分支无去重，刷新窗口内 N 个请求
spawn N 个后台全量抓取任务。
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

import web.routers.model_discovery as md
from web._discovery_cache import _cache


@pytest.fixture(autouse=True)
def _restore_discovery_cache():
    """_cache 是模块级全局：用例前后快照恢复，防跨用例状态泄漏。"""
    snapshot = dict(_cache)
    yield
    _cache.clear()
    _cache.update(snapshot)


class _FakeRegistry:
    def __init__(self, items):
        self._items = items

    async def list(self):
        return self._items


def _make_request(registry_items):
    state = types.SimpleNamespace()
    state.core = types.SimpleNamespace(
        local_ai_instances=types.SimpleNamespace(
            _model_registry=_FakeRegistry(registry_items)))
    app = types.SimpleNamespace(state=state)
    return types.SimpleNamespace(app=app)


def _chat_item(catalog_id="phi3-mini", purpose="chat"):
    return types.SimpleNamespace(purpose=purpose, catalog_id=catalog_id,
                                 id=catalog_id)


_REMOTE_GROUP = {"provider": "agnes", "label": "Agnes AI",
                 "models": [{"id": "m1"}]}


async def test_fresh_cache_hit_injects_local_ort_group_without_polluting_cache():
    _cache.update({"data": [dict(_REMOTE_GROUP)], "ts": time.time(),
                   "refreshing": False})
    resp = await md.discover_models(_make_request([_chat_item()]))

    providers = [g["provider"] for g in resp.data]
    assert providers == ["agnes", "local-ort"]
    assert resp.data[-1]["models"][0]["id"] == "local:phi3-mini"
    # 注入只发生在响应拷贝上，缓存本体不得被污染
    assert len(_cache["data"]) == 1
    assert _cache["data"][0]["provider"] == "agnes"


async def test_no_local_chat_models_means_no_empty_group():
    _cache.update({"data": [dict(_REMOTE_GROUP)], "ts": time.time(),
                   "refreshing": False})

    resp = await md.discover_models(_make_request([_chat_item(purpose="embed")]))

    assert [g["provider"] for g in resp.data] == ["agnes"]


async def test_stale_spawns_single_background_refresh(monkeypatch):
    _cache.update({"data": [dict(_REMOTE_GROUP)],
                   "ts": time.time() - 10_000, "refreshing": False})

    spawned: list[int] = []
    gate = asyncio.Event()

    async def fake_refresh(request):
        spawned.append(id(request))
        await gate.wait()

    monkeypatch.setattr(md, "_refresh_cache_background", fake_refresh)

    req = _make_request([])
    first = await md.discover_models(req)
    # create_task 仅调度不执行，让出一次循环让后台任务真正启动
    await asyncio.sleep(0)
    second = await md.discover_models(req)
    await asyncio.sleep(0)

    # 刷新在飞期间，第二个 stale 请求不得重复 spawn
    assert len(spawned) == 1
    assert [g["provider"] for g in first.data] == ["agnes"]
    assert [g["provider"] for g in second.data] == ["agnes"]

    gate.set()
    await asyncio.sleep(0)
