# tests/test_search_engines_router.py — 搜索引擎配置页后端：配置/保存/测试
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import web.routers.search_engines as se
from web.routers.auth import get_current_user


def _client():
    app = FastAPI()
    app.include_router(se.router)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("SEARCH_ENGINE_PRIMARY", raising=False)
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    monkeypatch.delenv("ANYSEARCH_ENABLED", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    yield


def test_get_config_reports_engines_and_primary(monkeypatch):
    monkeypatch.setattr(se, "_engine_available", lambda eid: eid == "bing")
    monkeypatch.setenv("SEARCH_ENGINE_PRIMARY", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-abc123def456")
    with _client() as c:
        data = c.get("/search-engines/config").json()["data"]
    assert data["primary"] == "tavily"
    by_id = {e["id"]: e for e in data["engines"]}
    assert set(by_id) == {"anysearch", "tavily", "bing"}
    assert by_id["bing"]["available"] is True
    assert by_id["anysearch"]["available"] is False
    assert by_id["tavily"]["key_configured"] is True
    assert "tvly-abc123def456" not in by_id["tavily"]["masked_key"]  # 脱敏


def test_put_config_validates_primary():
    with _client() as c:
        assert c.put("/search-engines/config", json={"primary": "google"}).status_code == 400
        assert c.put("/search-engines/config", json={"primary": "anysearch", "keys": "x"}).status_code == 400
        bad = c.put("/search-engines/config",
                    json={"primary": "anysearch", "keys": {"EVIL_KEY": "x"}})
        assert bad.status_code == 400


def test_put_config_persists_and_hot_reloads(monkeypatch):
    captured = {}

    def _fake_persist(updates):
        captured.update(updates)
        import os
        for k, v in updates.items():
            os.environ[k] = v
        return list(updates.keys())

    monkeypatch.setattr(se, "_persist_env", _fake_persist)
    with _client() as c:
        resp = c.put("/search-engines/config", json={
            "primary": "anysearch",
            "keys": {"ANYSEARCH_API_KEY": " as_sk_test ", "TAVILY_API_KEY": "tvly-x"},
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
    assert data["primary"] == "anysearch"
    assert captured["SEARCH_ENGINE_PRIMARY"] == "anysearch"
    assert captured["ANYSEARCH_API_KEY"] == "as_sk_test"  # strip
    assert captured["TAVILY_API_KEY"] == "tvly-x"
    import os
    assert os.environ["SEARCH_ENGINE_PRIMARY"] == "anysearch"


def test_post_test_single_engine(monkeypatch):
    def _fake_run(engine_id, query, top_k):
        return {"engine": engine_id, "latency_ms": 12.5, "count": 2,
                "results": [{"title": f"{engine_id}:{query}", "url": "https://x",
                             "snippet": "s"}]}
    monkeypatch.setattr(se, "_engine_available", lambda eid: True)
    monkeypatch.setattr(se, "_run_engine", _fake_run)
    with _client() as c:
        data = c.post("/search-engines/test",
                      json={"query": "q", "engine": "bing", "top_k": 3}).json()["data"]
    assert data["mode"] == "single" and data["engine"] == "bing"
    assert data["latency_ms"] == 12.5 and data["count"] == 2


def test_post_test_unavailable_engine():
    with _client() as c:
        resp = c.post("/search-engines/test", json={"query": "q", "engine": "tavily"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False and body["error"]["code"] == "ENGINE_UNAVAILABLE"


def test_post_test_compare_runs_three(monkeypatch):
    calls = []

    def _fake_run(engine_id, query, top_k):
        calls.append(engine_id)
        return {"engine": engine_id, "latency_ms": 1.0, "count": 0,
                "results": [], "error": f"boom-{engine_id}"}

    monkeypatch.setattr(se, "_run_engine", _fake_run)
    with _client() as c:
        data = c.post("/search-engines/test",
                      json={"query": "q", "engine": "compare"}).json()["data"]
    assert data["mode"] == "compare"
    assert sorted(e["engine"] for e in data["engines"]) == ["anysearch", "bing", "tavily"]
    assert all("error" in e for e in data["engines"])  # 单引擎失败不阻塞对比


def test_post_test_validation():
    with _client() as c:
        assert c.post("/search-engines/test", json={"query": ""}).status_code == 400
        assert c.post("/search-engines/test",
                      json={"query": "q", "engine": "google"}).status_code == 400


def test_engine_order_respects_primary(monkeypatch):
    import tools.web_tools_v2 as wt
    monkeypatch.setenv("SEARCH_ENGINE_PRIMARY", "bing")
    assert wt._engine_order() == ["bing", "anysearch", "tavily"]
    monkeypatch.setenv("SEARCH_ENGINE_PRIMARY", "tavily")
    assert wt._engine_order() == ["tavily", "anysearch", "bing"]
    monkeypatch.delenv("SEARCH_ENGINE_PRIMARY")
    assert wt._engine_order() == ["anysearch", "tavily", "bing"]


def test_do_search_primary_bing_first(monkeypatch):
    """主引擎=bing 时降级序首位为 Bing（mock 三引擎观察调用序）。"""
    import tools.web_tools_v2 as wt
    order_seen = []
    monkeypatch.setenv("SEARCH_ENGINE_PRIMARY", "bing")
    monkeypatch.setattr(wt, "anysearch_available", lambda: True)

    async def _fake_as(q, m):
        order_seen.append("anysearch")
        return [], ""

    def _fake_tav(q, max_results=6, search_depth="basic", news=False):
        order_seen.append("tavily")
        return [], ""

    def _fake_bing(q, max_results=8):
        order_seen.append("bing")
        return [{"title": "B", "url": "https://b", "content": "c"}]

    monkeypatch.setattr(wt, "anysearch_search_sync", _fake_as)
    monkeypatch.setattr(wt, "_tavily_available", lambda: True)
    monkeypatch.setattr(wt, "_tavily_search_sync", _fake_tav)
    monkeypatch.setattr(wt, "_bing_search_sync", _fake_bing)
    wt._search_cache.clear()
    results, engine, _ = asyncio.run(wt._do_search("查询", max_results=5))
    assert engine == "Bing"
    assert order_seen == ["bing"]  # bing 首位直接命中，其余引擎零调用
