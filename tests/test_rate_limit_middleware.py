"""web/middleware/rate_limit.py — 三级速率限制中间件测试

覆盖:
    1. 限制内请求正常通过
    2. 超限返回 429
    3. localhost 白名单放行
    4. 写操作端点限制更严
    5. 429 响应包含 Retry-After header
    6. 不同用户独立计数 (隔离)
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.middleware.rate_limit import RateLimitMiddleware


def _make_app(
    global_limit: float = 600,
    user_limit: float = 60,
    write_limit: float = 30,
    whitelist=None,
) -> FastAPI:
    """构造带速率限制中间件的测试 app。

    TestClient 默认 client host 为 "testclient" (非内网 IP), 会被限流。
    """
    app = FastAPI()
    app.add_middleware(
        RateLimitMiddleware,
        global_limit=global_limit,
        user_limit=user_limit,
        write_limit=write_limit,
        whitelist=whitelist or set(),
    )

    @app.get("/api/v1/ping")
    async def ping():
        return {"ok": True}

    @app.post("/api/v1/write")
    async def write():
        return {"ok": True}

    @app.delete("/api/v1/del")
    async def delete():
        return {"ok": True}

    return app


# ── 1. 限制内请求正常通过 ──

def test_within_limit_passes():
    app = _make_app(user_limit=5)
    client = TestClient(app)
    for _ in range(5):
        r = client.get("/api/v1/ping")
        assert r.status_code == 200, r.text


# ── 2. 超限返回 429 ──

def test_exceed_limit_returns_429():
    app = _make_app(user_limit=2)
    client = TestClient(app)
    assert client.get("/api/v1/ping").status_code == 200
    assert client.get("/api/v1/ping").status_code == 200
    r = client.get("/api/v1/ping")
    assert r.status_code == 429
    body = r.json()
    assert body["detail"] == "Rate limit exceeded"
    assert body["retry_after"] >= 1


# ── 3. localhost 白名单放行 ──

def test_whitelist_localhost():
    app = _make_app(user_limit=2)
    # 用 client=("127.0.0.1", 0) 让 request.client.host 为 localhost
    client = TestClient(app, client=("127.0.0.1", 0))
    # 10 次请求远超 user_limit=2, 但 localhost 在白名单, 不应受限
    for _ in range(10):
        r = client.get("/api/v1/ping")
        assert r.status_code == 200, r.text


# ── 4. 写操作端点限制更严 ──

def test_write_endpoint_stricter():
    app = _make_app(user_limit=60, write_limit=2)
    client = TestClient(app)
    # GET 不受写端点限制, 连续多次通过
    for _ in range(10):
        assert client.get("/api/v1/ping").status_code == 200
    # POST 受写端点限制 (2/min), 前两次通过
    assert client.post("/api/v1/write").status_code == 200
    assert client.post("/api/v1/write").status_code == 200
    # 第三次 POST 超出写端点限制 -> 429
    r = client.post("/api/v1/write")
    assert r.status_code == 429, r.text


# ── 5. 429 响应包含 Retry-After header ──

def test_retry_after_header():
    app = _make_app(user_limit=1)
    client = TestClient(app)
    assert client.get("/api/v1/ping").status_code == 200  # 消费唯一令牌
    r = client.get("/api/v1/ping")
    assert r.status_code == 429
    # Retry-After header 存在且为正整数
    lower_headers = {k.lower(): v for k, v in r.headers.items()}
    assert "retry-after" in lower_headers, f"headers={lower_headers}"
    retry_after = int(lower_headers["retry-after"])
    assert retry_after >= 1
    # body 中 retry_after 与 header 一致
    assert r.json()["retry_after"] == retry_after


# ── 6. 不同用户独立计数 (隔离) — 仅使用 request.state.user_id ──

def test_per_user_isolation_via_state():
    """Per-user isolation uses server-side request.state.user_id, not client header."""
    app = _make_app(user_limit=2)

    @app.middleware("http")
    async def inject_user_id(request, call_next):
        # Simulate server-side user identity injection (auth middleware sets this)
        uid = request.query_params.get("user", "")
        if uid:
            request.state.user_id = uid
        return await call_next(request)

    client = TestClient(app)
    # alice 用完自己的 2 次配额
    assert client.get("/api/v1/ping?user=alice").status_code == 200
    assert client.get("/api/v1/ping?user=alice").status_code == 200
    # alice 第 3 次超限
    assert client.get("/api/v1/ping?user=alice").status_code == 429
    # bob 独立计数, 不受 alice 影响, 仍可正常请求
    assert client.get("/api/v1/ping?user=bob").status_code == 200
    assert client.get("/api/v1/ping?user=bob").status_code == 200


# ── 7. X-User-ID header 伪造不再绕过限流 ──

def test_x_user_id_header_ignored():
    """X-User-ID header is untrusted and does NOT create separate buckets.

    修复前: 攻击者可伪造 X-User-ID 绕过用户级限流
    修复后: 仅使用服务端 request.state.user_id
    """
    app = _make_app(user_limit=2)
    client = TestClient(app)
    # 用完同一 IP 的 2 次配额
    assert client.get("/api/v1/ping").status_code == 200
    assert client.get("/api/v1/ping").status_code == 200
    # 超限
    assert client.get("/api/v1/ping").status_code == 429
    # 伪造 X-User-ID 也不能绕过 — 仍然 429
    assert client.get("/api/v1/ping", headers={"X-User-ID": "fake_user"}).status_code == 429
