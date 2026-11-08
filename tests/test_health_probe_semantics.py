"""探活三态与限流豁免语义（/health 探针）。

覆盖：数据库正常/异常 × 嵌入就绪/降级 四组组合；degraded 必须 200 + JSON，
unhealthy 必须 503 + JSON，响应恒带 Cache-Control: no-store；/health 与
/health/ 的限流豁免等价，非只读方法仍受限流。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.middleware.rate_limit import RateLimitMiddleware
from web.routers import health as health_mod


class _FakeDB:
    def __init__(self, fail: bool = False, db_path=None):
        self.fail = fail
        if db_path is not None:
            self.db_path = db_path

    async def fetch_one(self, sql, *args, **kwargs):
        if self.fail:
            raise RuntimeError("database is locked")
        return {"ok": 1}


class _FakeVec:
    def __init__(self, status=None, exc=None):
        self._status = status or {}
        self._exc = exc

    def embed_engine_status(self):
        if self._exc is not None:
            raise self._exc
        return dict(self._status)


class _FakeMemory:
    def __init__(self, vec):
        self.vec = vec


class _FakeCore:
    def __init__(self, db, memory):
        self.db = db
        self.memory = memory


def _vec_ready():
    return _FakeVec({"mode": "local", "engine_running": True, "api_configured": True,
                     "backend": "npu", "dimensions": 1024})


def _vec_degraded():
    return _FakeVec({"mode": "local", "engine_running": False, "api_configured": True,
                     "backend": "npu", "dimensions": 1024})


def _client(core):
    app = FastAPI()
    app.state.core = core
    app.include_router(health_mod.probe_router)
    return TestClient(app)


def _core(db_fail, vec, tmp_path):
    db = _FakeDB(fail=db_fail, db_path=(tmp_path / "agent.db"))
    return _FakeCore(db, _FakeMemory(vec))


def _reset_cache():
    health_mod._write_probe_cache.clear()


def _assert_json(r):
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["cache-control"] == "no-store"
    return r.json()


def test_db_ok_embedding_ready_returns_200_ok(tmp_path):
    _reset_cache()
    r = _client(_core(False, _vec_ready(), tmp_path)).get("/health")
    assert r.status_code == 200
    body = _assert_json(r)
    assert body["status"] == "ok"
    assert body["healthy"] is True
    assert body["reasons"] == []
    assert body["checks"]["database"]["state"] == "ok"
    assert body["checks"]["database"]["writable"] is True
    assert body["checks"]["embedding"]["state"] == "ok"


def test_db_ok_embedding_degraded_returns_200_degraded(tmp_path):
    _reset_cache()
    r = _client(_core(False, _vec_degraded(), tmp_path)).get("/health")
    assert r.status_code == 200
    body = _assert_json(r)
    assert body["status"] == "degraded"
    assert body["healthy"] is False
    assert body["checks"]["embedding"]["state"] == "degraded"
    assert body["checks"]["embedding"]["reason"] == "local embedding engine not ready"
    assert any("embedding" in reason for reason in body["reasons"])


def test_db_error_embedding_ready_returns_503_unhealthy(tmp_path):
    _reset_cache()
    r = _client(_core(True, _vec_ready(), tmp_path)).get("/health")
    assert r.status_code == 503
    body = _assert_json(r)
    assert body["status"] == "unhealthy"
    assert body["checks"]["database"]["state"] == "unhealthy"
    assert "RuntimeError" in body["checks"]["database"]["reason"]
    assert body["checks"]["embedding"]["state"] == "ok"


def test_db_error_and_embedding_degraded_returns_503(tmp_path):
    _reset_cache()
    r = _client(_core(True, _vec_degraded(), tmp_path)).get("/health")
    assert r.status_code == 503
    body = _assert_json(r)
    assert body["status"] == "unhealthy"
    assert len(body["reasons"]) == 2


def test_embedding_probe_exception_degrades_not_500(tmp_path):
    _reset_cache()
    vec = _FakeVec(exc=TypeError("mock embed failure"))
    r = _client(_core(False, vec, tmp_path)).get("/health")
    assert r.status_code == 200
    body = _assert_json(r)
    assert body["status"] == "degraded"
    assert "TypeError" in body["checks"]["embedding"]["reason"]


def test_vector_store_missing_degrades(tmp_path):
    _reset_cache()
    core = _FakeCore(_FakeDB(db_path=(tmp_path / "agent.db")), None)
    r = _client(core).get("/health")
    assert r.status_code == 200
    body = _assert_json(r)
    assert body["status"] == "degraded"
    assert body["checks"]["embedding"]["reason"] == "vector store not initialized"


def test_db_path_missing_marks_writability_skipped(tmp_path):
    _reset_cache()
    db = _FakeDB()          # 没有 db_path 属性：不得静默判成可写
    core = _FakeCore(db, _FakeMemory(_vec_ready()))
    r = _client(core).get("/health")
    assert r.status_code == 200
    body = _assert_json(r)
    assert body["checks"]["database"]["writable_state"] == "skipped"
    assert body["checks"]["database"]["writable"] is None
    assert body["status"] == "degraded"


def test_unexpected_exception_becomes_json_503(monkeypatch, tmp_path):
    _reset_cache()

    async def _boom(core):
        raise TypeError("unexpected probe crash")

    monkeypatch.setattr(health_mod, "_probe_database", _boom)
    r = _client(_core(False, _vec_ready(), tmp_path)).get("/health")
    assert r.status_code == 503
    body = _assert_json(r)
    assert body["status"] == "unhealthy"
    assert "TypeError" in body["checks"]["database"]["reason"]


def test_healthz_alias_has_same_semantics(tmp_path):
    _reset_cache()
    r = _client(_core(False, _vec_degraded(), tmp_path)).get("/healthz")
    assert r.status_code == 200
    body = _assert_json(r)
    assert body["status"] == "degraded"


def _rate_limited_client():
    app = FastAPI()

    @app.get("/health")
    def _health():
        return {"ok": True}

    @app.get("/healthz")
    def _healthz():
        return {"ok": True}

    @app.post("/health")
    def _health_post():
        return {"ok": True}

    @app.get("/other")
    def _other():
        return {"ok": True}

    app.add_middleware(RateLimitMiddleware, user_limit=5, write_limit=5,
                       global_limit=1000)
    return TestClient(app)


def test_health_paths_exempt_from_rate_limit_including_trailing_slash():
    client = _rate_limited_client()
    codes = [client.get("/health").status_code for _ in range(10)]
    codes += [client.get("/health/").status_code for _ in range(10)]
    codes += [client.get("/healthz").status_code for _ in range(10)]
    assert codes == [200] * 30, codes


def test_non_read_methods_on_health_still_limited():
    client = _rate_limited_client()
    codes = [client.post("/health").status_code for _ in range(12)]
    assert 429 in codes, codes


def test_other_paths_still_limited():
    client = _rate_limited_client()
    codes = [client.get("/other").status_code for _ in range(12)]
    assert 429 in codes, codes
