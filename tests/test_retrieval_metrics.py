# tests/test_retrieval_metrics.py — 检索测试/评测端点的指标口径与校验
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.routers.auth import get_current_user
from web.routers.retrieval import router


class _Mem:
    """Fake memory：按查询返回预制结果；fail_queries 中的查询抛错。"""

    def __init__(self, by_query=None, fail_queries=()):
        self.by_query = by_query or {}
        self.fail_queries = set(fail_queries)

    async def retrieve_memories_hybrid(self, query, k, use_kg=True):
        if query in self.fail_queries:
            raise RuntimeError("engine down")
        return self.by_query.get(query, [])[:k]


def _r(i, summary, score=0.5):
    return SimpleNamespace(id=i, summary=summary, score=score,
                           importance=1, emotion_label="", source="rag")


def _client(mem):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: "test-user"
    app.state.core = SimpleNamespace(memory=mem)
    return TestClient(app)


def test_single_query_metrics_with_expect():
    mem = _Mem({"q": [
        _r(1, "巴黎是法国首都", 0.9),
        _r(2, "法国的首都巴黎", 0.7),
        _r(3, "东京是日本首都", 0.2),
    ]})
    with _client(mem) as c:
        resp = c.post("/retrieval/test", json={
            "query": "q", "top_k": 3, "expect_keywords": ["巴黎", "埃菲尔"]})
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
            "query": "q", "expect_ids": [22, 99]})
        m = resp.json()["data"]["metrics"]
        assert m["expect_total"] == 2 and m["expect_covered"] == 1
        assert m["recall"] == 0.5 and m["precision"] == 0.5
        assert m["first_hit_rank"] == 2 and m["mrr"] == 0.5


def test_single_query_without_expect_only_stats():
    mem = _Mem({"q": [_r(1, "无基准", 0.3)]})
    with _client(mem) as c:
        resp = c.post("/retrieval/test", json={"query": "q"})
        m = resp.json()["data"]["metrics"]
        assert m["has_expect"] is False
        assert "recall" not in m and "precision" not in m
        assert m["returned"] == 1 and m["score_min"] == m["score_max"] == 0.3
        assert "threshold" in m and "latency_ms" in m


def test_single_query_error_returns_error_field():
    with _client(_Mem(fail_queries=["bad"])) as c:
        resp = c.post("/retrieval/test", json={"query": "bad"})
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


def test_evaluate_aggregates_and_failed_cases():
    mem = _Mem({
        "hit": [_r(1, "巴黎", 0.9)],
        "miss": [_r(2, "无关", 0.1)],
        "boom": [],
    }, fail_queries=["boom"])
    with _client(mem) as c:
        resp = c.post("/retrieval/evaluate", json={
            "top_k": 5,
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
            "cases": [{"query": "a"}]}).json()["data"]
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
