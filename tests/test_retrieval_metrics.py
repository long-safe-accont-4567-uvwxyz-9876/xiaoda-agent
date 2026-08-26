# tests/test_retrieval_metrics.py — 检索测试/评测端点的指标口径与校验
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers.auth import get_current_user
from web.routers.retrieval import router

_DEFAULT_SCOPE = {"user_id": "default", "agent_id": "xiaoda"}


class _Cache:
    def __init__(self):
        self.calls = []

    async def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


class _Mem:
    """Fake memory：记录 full/hybrid 调用并按查询返回预制 dict。"""

    def __init__(self, by_query=None, fail_queries=()):
        self.by_query = by_query or {}
        self.fail_queries = set(fail_queries)
        self.full_calls = []
        self.hybrid_calls = []
        self._query_cache = _Cache()

    async def _hybrid_fts_search_scoped(self, query, k, scope, is_raw):
        self.hybrid_calls.append((query, {
            "k": k, "scope": scope, "channel": "fts", "is_raw": is_raw,
        }))
        return self.by_query.get(query, [])[:k]

    async def retrieve_memories(self, query, **kwargs):
        self.full_calls.append((query, kwargs))
        if query in self.fail_queries:
            raise RuntimeError("engine down")
        return self.by_query.get(query, [])[:kwargs.get("k", 5)]

    async def retrieve_memories_hybrid(self, query, k, use_kg=True, scope=None):
        self.hybrid_calls.append((query, {"k": k, "use_kg": use_kg, "scope": scope}))
        if query in self.fail_queries:
            raise RuntimeError("engine down")
        return self.by_query.get(query, [])[:k]


def _r(i, summary, score=0.5):
    return {"id": i, "summary": summary, "score": score,
            "importance": 1, "emotion_label": "", "source": "rag"}


class _Router:
    def __init__(self):
        self.calls = []

    async def route(self, task_type, messages, **kwargs):
        self.calls.append((task_type, messages, kwargs))
        evidence_id = next(
            line[1:line.index("]")]
            for line in messages[-1]["content"].splitlines()
            if line.startswith("[") and ":" in line
        )
        return f"用户住在上海 [{evidence_id}]。"


def _client(mem, router_instance=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.state.core = SimpleNamespace(
        memory=mem, router=router_instance or _Router()
    )
    return TestClient(app)


async def test_channel_metrics_record_fixed_name_latency_candidates_and_errors():
    from memory.retrieval.pipeline import RetrievalEngine
    from utils.metrics import metrics

    metrics._counters.clear()
    metrics._timers.clear()
    metrics._histograms.clear()
    engine = RetrievalEngine(SimpleNamespace())

    assert await engine._timed("fts", _async_value([{"id": 1}]), "private query") == [
        {"id": 1}
    ]
    try:
        await engine._timed("kg", _async_error(RuntimeError("down")), "private query")
    except RuntimeError:
        pass

    snapshot = metrics.get_snapshot()
    assert snapshot["counters"]["retrieval.channel.fts.success"] == 1
    assert snapshot["counters"]["retrieval.channel.kg.error"] == 1
    assert snapshot["hist.retrieval.channel.fts.candidates"]["max"] == 1
    assert "private query" not in json.dumps(snapshot, ensure_ascii=False)


async def _async_value(value):
    return value


async def _async_error(error):
    raise error


def test_evaluation_requires_explicit_scope():
    with _client(_Mem()) as c:
        assert c.post("/retrieval/test", json={"query": "q"}).status_code == 400
        assert c.post("/retrieval/evaluate", json={
            "cases": [{"query": "q"}]
        }).status_code == 400


def test_update_config_persists_and_syncs_runtime(tmp_path, monkeypatch):
    from web import config_service as config_service_module
    from web.config_service import ConfigService

    service = ConfigService(tmp_path / "webui_overrides.json")
    monkeypatch.setattr(config_service_module, "_instance", service)
    mem = _Mem()
    with _client(mem) as c:
        resp = c.put("/retrieval/config", json={
            "updates": {"RAG_RERANK_WEIGHT": 0.42, "HYDE_ENABLED": True}
        })

    assert resp.status_code == 200
    assert service.get("retrieval.RAG_RERANK_WEIGHT") == 0.42
    assert service.get("retrieval.HYDE_ENABLED") is True
    import config
    import config_constants
    assert config.RAG_RERANK_WEIGHT == 0.42
    assert config_constants.RAG_RERANK_WEIGHT == 0.42
    assert config.HYDE_ENABLED is True
    assert config_constants.HYDE_ENABLED is True
    data = resp.json()["data"]
    assert data["hot_applied"] == ["HYDE_ENABLED", "RAG_RERANK_WEIGHT"]
    assert data["restart_required"] == []
    assert mem._query_cache.calls == []


def test_query_cache_config_hot_reconfigures_existing_instance(tmp_path, monkeypatch):
    from web import config_service as config_service_module
    from web.config_service import ConfigService

    service = ConfigService(tmp_path / "webui_overrides.json")
    monkeypatch.setattr(config_service_module, "_instance", service)
    mem = _Mem()
    with _client(mem) as c:
        resp = c.put("/retrieval/config", json={"updates": {
            "QUERY_CACHE_THRESHOLD": 0.91,
            "QUERY_CACHE_MAX_SIZE": 99,
            "QUERY_CACHE_TTL": 123,
        }})

    assert resp.status_code == 200
    assert mem._query_cache.calls == [{
        "threshold": 0.91, "max_size": 99, "ttl": 123,
    }]


def test_constructor_only_config_reports_restart_required(tmp_path, monkeypatch):
    from web import config_service as config_service_module
    from web.config_service import ConfigService

    service = ConfigService(tmp_path / "webui_overrides.json")
    monkeypatch.setattr(config_service_module, "_instance", service)
    with _client(_Mem()) as c:
        data = c.put("/retrieval/config", json={
            "updates": {"RERANKER_ENABLED": False}
        }).json()["data"]

    assert data["hot_applied"] == []
    assert data["restart_required"] == ["RERANKER_ENABLED"]


def test_reset_removes_retrieval_overrides(tmp_path, monkeypatch):
    from web import config_service as config_service_module
    from web.config_service import ConfigService

    service = ConfigService(tmp_path / "webui_overrides.json")
    service.set("retrieval.HYDE_ENABLED", True)
    monkeypatch.setattr(config_service_module, "_instance", service)
    with _client(_Mem()) as c:
        resp = c.post("/retrieval/config/reset", json={})

    assert resp.status_code == 200
    assert service.get("retrieval") == {}


def test_update_config_validation_is_atomic(tmp_path, monkeypatch):
    from web import config_service as config_service_module
    from web.config_service import ConfigService

    service = ConfigService(tmp_path / "webui_overrides.json")
    monkeypatch.setattr(config_service_module, "_instance", service)
    with _client(_Mem()) as c:
        resp = c.put("/retrieval/config", json={
            "updates": {"HYDE_ENABLED": True, "RAG_RECALL_LIMIT": "bad"}
        })

    assert resp.status_code == 400
    assert service.get("retrieval.HYDE_ENABLED") is None
    assert service.get("retrieval.RAG_RECALL_LIMIT") is None


def test_single_query_defaults_to_full_pipeline_with_scope():
    mem = _Mem({"q": [_r(1, "结果", 0.8)]})
    with _client(mem) as c:
        resp = c.post("/retrieval/test", json={
            "query": "q",
            "scope": {"user_id": "alice", "agent_id": "xiaoda", "session_id": "web-eval"},
        })

    assert resp.status_code == 200
    assert len(mem.full_calls) == 1
    assert mem.hybrid_calls == []
    query, kwargs = mem.full_calls[0]
    assert query == "q"
    assert kwargs["scope"].user_id == "alice"
    assert kwargs["scope"].agent_id == "xiaoda"
    assert kwargs["conv_user_id"] == "alice"


def test_prompt_mode_returns_evidence_trace_budget_and_citation_validation():
    mem = _Mem({"q": [_r(1, "用户住在上海", 0.9)]})
    with _client(mem) as c:
        response = c.post("/retrieval/test", json={
            "query": "q",
            "mode": "prompt",
            "scope": _DEFAULT_SCOPE,
            "evidence_token_budget": 500,
        })

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == "prompt"
    assert data["evidence_bundle"]["schema_version"] == "evidence-bundle-v1"
    evidence_id = data["evidence_bundle"]["evidence"][0]["evidence_id"]
    assert evidence_id.startswith("M:episodic:1:v")
    assert "<retrieved_evidence" in data["prompt_preview"]
    assert data["generated_answer"] == f"用户住在上海 [{evidence_id}]。"
    assert data["citation_validation"]["valid"] is True
    assert data["input_tokens"] <= data["input_token_budget"]


def test_prompt_mode_does_not_generate_when_fixed_prompt_exceeds_budget():
    mem = _Mem({"q": [_r(1, "用户住在上海", 0.9)]})
    router_instance = _Router()
    with _client(mem, router_instance) as client:
        data = client.post("/retrieval/test", json={
            "query": "q", "mode": "prompt", "scope": _DEFAULT_SCOPE,
            "evidence_token_budget": 1,
        }).json()["data"]

    assert router_instance.calls == []
    assert data["input_tokens"] == 0
    assert data["evidence_bundle"]["prompt_enabled"] is False


def test_single_query_channel_mode_uses_fixed_diagnostic_channel():
    mem = _Mem({"q": [_r(1, "结果", 0.8)]})
    with _client(mem) as c:
        resp = c.post("/retrieval/test", json={
            "query": "q", "mode": "channel", "channel": "fts",
            "scope": _DEFAULT_SCOPE,
        })

    assert resp.status_code == 200
    assert mem.hybrid_calls[0][1]["channel"] == "fts"
    assert resp.json()["data"]["mode"] == "channel"


def test_single_query_hybrid_mode_is_explicit_diagnostic():
    mem = _Mem({"q": [_r(1, "结果", 0.8)]})
    with _client(mem) as c:
        resp = c.post("/retrieval/test", json={
            "query": "q", "mode": "hybrid",
            "scope": {"user_id": "alice", "agent_id": "xiaoda"},
        })

    assert resp.status_code == 200
    assert mem.full_calls == []
    assert len(mem.hybrid_calls) == 1
    assert resp.json()["data"]["mode"] == "hybrid"


def test_single_query_metrics_with_expect():
    mem = _Mem({"q": [
        _r(1, "巴黎是法国首都", 0.9),
        _r(2, "法国的首都巴黎", 0.7),
        _r(3, "东京是日本首都", 0.2),
    ]})
    with _client(mem) as c:
        resp = c.post("/retrieval/test", json={
            "query": "q", "top_k": 3, "expect_keywords": ["巴黎", "埃菲尔"],
            "scope": _DEFAULT_SCOPE,
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        m = data["metrics"]
        # 期望 2 项，覆盖 1 项（巴黎）→ recall 0.5；3 条返回 2 条命中 → precision 2/3
        assert m["has_expect"] is True
        assert m["expect_total"] == 2 and m["expect_covered"] == 1
        assert m["recall"] == 0.5
        assert m["precision"] == round(2 / 3, 4)
        assert m["matched_results"] == 2
        assert m["first_hit_rank"] == 1 and m["mrr"] == 1.0
        assert m["hit"] is True
        assert m["returned"] == 3 and m["above_threshold"] >= 1
        assert m["latency_ms"] >= 0
        # 每条结果带命中标注
        assert data["results"][0]["matched"] is True
        assert data["results"][0]["matched_keywords"] == ["巴黎"]
        assert data["results"][2]["matched"] is False


def test_single_query_expect_ids():
    mem = _Mem({"q": [_r(11, "甲", 0.8), _r(22, "乙", 0.6)]})
    with _client(mem) as c:
        resp = c.post("/retrieval/test", json={
            "query": "q", "expect_ids": [22, 99], "scope": _DEFAULT_SCOPE,
        })
        m = resp.json()["data"]["metrics"]
        assert m["expect_total"] == 2 and m["expect_covered"] == 1
        assert m["recall"] == 0.5 and m["precision"] == 0.5
        assert m["first_hit_rank"] == 2 and m["mrr"] == 0.5


def test_single_query_without_expect_only_stats():
    mem = _Mem({"q": [_r(1, "无基准", 0.3)]})
    with _client(mem) as c:
        resp = c.post("/retrieval/test", json={
            "query": "q", "scope": _DEFAULT_SCOPE,
        })
        m = resp.json()["data"]["metrics"]
        assert m["has_expect"] is False
        assert "recall" not in m and "precision" not in m
        assert m["returned"] == 1 and m["score_min"] == m["score_max"] == 0.3
        assert "threshold" in m and "latency_ms" in m


def test_single_query_error_returns_error_field():
    with _client(_Mem(fail_queries=["bad"])) as c:
        resp = c.post("/retrieval/test", json={
            "query": "bad", "scope": _DEFAULT_SCOPE,
        })
        data = resp.json()["data"]
        assert data["count"] == 0 and data["results"] == []
        assert "engine down" in data["error"]


def test_single_query_validation():
    with _client(_Mem()) as c:
        assert c.post("/retrieval/test", json={"query": ""}).status_code == 400
        resp = c.post("/retrieval/test", json={"query": "q", "expect_keywords": "巴黎"})
        assert resp.status_code == 400
        resp = c.post("/retrieval/test", json={"query": "q", "expect_ids": [{}] })
        assert resp.status_code == 400


def test_graded_relevance_reports_ndcg_and_unanswerable_false_positive():
    mem = _Mem({
        "graded": [_r(2, "次相关", 0.8), _r(1, "最相关", 0.7)],
        "partial": [_r(1, "只返回最相关", 0.9)],
        "unknown": [_r(9, "错误召回", 0.2)],
    })
    with _client(mem) as c:
        graded = c.post("/retrieval/test", json={
            "query": "graded", "expect_relevance": {"1": 3, "2": 1},
            "scope": _DEFAULT_SCOPE,
        }).json()["data"]["metrics"]
        unknown = c.post("/retrieval/test", json={
            "query": "unknown", "unanswerable": True,
            "scope": {"user_id": "default", "agent_id": "xiaoda"},
        }).json()["data"]["metrics"]
        partial = c.post("/retrieval/test", json={
            "query": "partial", "top_k": 2,
            "expect_relevance": {"1": 3, "2": 2},
            "scope": {"user_id": "default", "agent_id": "xiaoda"},
        }).json()["data"]["metrics"]

    assert 0 < graded["ndcg"] < 1
    assert graded["graded_relevance"] is True
    assert partial["ndcg"] < 1
    assert 0 <= partial["ndcg"] <= 1
    assert unknown["unanswerable"] is True
    assert unknown["false_positive"] is True


def test_evaluate_aggregates_and_failed_cases():
    mem = _Mem({
        "hit": [_r(1, "巴黎", 0.9)],
        "miss": [_r(2, "无关", 0.1)],
        "boom": [],
    }, fail_queries=["boom"])
    with _client(mem) as c:
        resp = c.post("/retrieval/evaluate", json={
            "scope": _DEFAULT_SCOPE,
            "cases": [
                {"query": "hit", "expect_keywords": ["巴黎"]},
                {"query": "miss", "expect_keywords": ["巴黎"]},
                {"query": "boom", "expect_keywords": ["x"]},
            ]})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["cases_total"] == 3 and data["cases_ok"] == 2
        assert data["cases_failed"] == 1 and data["cases_with_expect"] == 2
        agg = data["aggregate"]
        # 宏平均：recall (1.0+0)/2，precision (1.0+0)/2
        assert agg["recall_macro"] == 0.5 and agg["precision_macro"] == 0.5
        assert agg["f1_macro"] == 0.5 and agg["mrr_macro"] == 0.5
        assert agg["hit_rate"] == 0.5
        assert agg["latency_avg_ms"] >= 0 and agg["latency_p95_ms"] >= agg["latency_avg_ms"] or \
            agg["latency_p95_ms"] >= 0
        cases = {c["query"]: c for c in data["cases"]}
        assert cases["boom"]["metrics"] is None and "error" in cases["boom"]
        assert cases["hit"]["metrics"]["recall"] == 1.0


def test_evaluate_without_expect_counts_latency_only():
    mem = _Mem({"a": [_r(1, "x", 0.5)]})
    with _client(mem) as c:
        data = c.post("/retrieval/evaluate", json={
            "scope": _DEFAULT_SCOPE,
            "cases": [{"query": "a"}],
        }).json()["data"]
        assert data["cases_with_expect"] == 0
        assert data["aggregate"]["recall_macro"] == 0.0  # 无基准用例不进宏平均
        assert data["aggregate"]["latency_avg_ms"] >= 0


def test_evaluate_validation():
    with _client(_Mem()) as c:
        assert c.post("/retrieval/evaluate", json={"cases": []}).status_code == 400
        assert c.post("/retrieval/evaluate", json={"cases": "x"}).status_code == 400
        resp = c.post("/retrieval/evaluate", json={
            "cases": [{"query": ""}]})
        assert resp.status_code == 400
        resp = c.post("/retrieval/evaluate", json={
            "cases": [{"query": "q", "expect_keywords": 1}]})
        assert resp.status_code == 400
        resp = c.post("/retrieval/evaluate", json={
            "cases": [{"query": f"q{i}"} for i in range(51)]})
        assert resp.status_code == 400
