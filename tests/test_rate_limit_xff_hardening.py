"""XFF 伪造绕过测试（VULN-28 补充）。

背景：TRUST_FORWARDED_FOR=1 时若无条件解析 X-Forwarded-For，攻击者直连
（无反代）即可伪造任意来源 IP，轮换绕过 per-IP 的 login 限流与 auth 的
失败锁定。修复：仅当 socket 对端是可信代理（回环/显式可信网段）时才解析 XFF。
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from web.middleware.rate_limit import RateLimitMiddleware


def _make_app(**mw_kwargs) -> FastAPI:
    app = FastAPI()
    defaults = dict(global_limit=600, user_limit=60, write_limit=30,
                    whitelist=set())
    defaults.update(mw_kwargs)
    app.add_middleware(RateLimitMiddleware, **defaults)

    @app.post("/api/v1/auth/login")
    async def login():
        return {"ok": True}

    return app


# ── 1. 直连公网对端：伪造 XFF 无效，分桶基于 socket 对端 ──

def test_xff_ignored_for_direct_public_peer(monkeypatch):
    """攻击者直连（公网 peer）伪造 XFF 轮换，无法绕过 login 限流"""
    monkeypatch.setenv("TRUST_FORWARDED_FOR", "1")
    app = _make_app(login_limit=2)
    client = TestClient(app, client=("203.0.113.5", 0))

    # 每个请求伪造不同 XFF，若 XFF 被信任则各入不同桶、永不 429
    for i in range(2):
        r = client.post("/api/v1/auth/login", headers={"X-Forwarded-For": f"8.8.8.{i}"})
        assert r.status_code == 200
    # 第 3 次：无论伪造什么 XFF，都应命中同一 peer 桶 → 429
    r = client.post("/api/v1/auth/login", headers={"X-Forwarded-For": "8.8.8.9"})
    assert r.status_code == 429


def test_xff_ignored_for_direct_private_peer(monkeypatch):
    """直连内网 peer（未配置可信网段）同样忽略伪造 XFF"""
    monkeypatch.setenv("TRUST_FORWARDED_FOR", "1")
    app = _make_app(login_limit=2)
    client = TestClient(app, client=("192.168.1.50", 0))

    for i in range(2):
        assert client.post(
            "/api/v1/auth/login", headers={"X-Forwarded-For": f"10.0.0.{i}"}
        ).status_code == 200
    assert client.post(
        "/api/v1/auth/login", headers={"X-Forwarded-For": "10.0.0.9"}
    ).status_code == 429


# ── 2. 可信代理对端：正常解析 XFF（反代场景） ──

def test_xff_used_for_trusted_proxy_peer(monkeypatch):
    """对端为回环（反代场景）时，XFF 正常解析并分桶"""
    monkeypatch.setenv("TRUST_FORWARDED_FOR", "1")
    app = _make_app(login_limit=2)
    client = TestClient(app, client=("127.0.0.1", 0))

    # 两个不同真实客户端（经 XFF）各自独立计数
    for _ in range(2):
        assert client.post(
            "/api/v1/auth/login", headers={"X-Forwarded-For": "1.1.1.1"}
        ).status_code == 200
    assert client.post(
        "/api/v1/auth/login", headers={"X-Forwarded-For": "1.1.1.1"}
    ).status_code == 429
    # 不同 XFF 客户端不受影响
    assert client.post(
        "/api/v1/auth/login", headers={"X-Forwarded-For": "2.2.2.2"}
    ).status_code == 200


# ── 3. auth._get_client_ip 对端可信逻辑 ──

def _make_request(peer: str, xff: str) -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/auth/login",
        "raw_path": b"/api/v1/auth/login",
        "query_string": b"",
        "headers": [(b"x-forwarded-for", xff.encode())],
        "client": (peer, 54321),
        "server": ("127.0.0.1", 8082),
    }
    return Request(scope)


def test_get_client_ip_ignores_xff_for_direct_peer(monkeypatch):
    """直连公网对端时 _get_client_ip 忽略伪造 XFF，返回 socket 对端"""
    from web.routers.auth import _get_client_ip
    monkeypatch.setenv("TRUST_FORWARDED_FOR", "1")
    assert _get_client_ip(_make_request("203.0.113.5", "8.8.8.8")) == "203.0.113.5"


def test_get_client_ip_uses_xff_for_trusted_peer(monkeypatch):
    """对端为回环（反代）时 _get_client_ip 解析 XFF 真实客户端"""
    from web.routers.auth import _get_client_ip
    monkeypatch.setenv("TRUST_FORWARDED_FOR", "1")
    assert _get_client_ip(_make_request("127.0.0.1", "1.2.3.4")) == "1.2.3.4"


def test_get_client_ip_default_no_xff():
    """默认不信任 XFF（未开 TRUST_FORWARDED_FOR），直连返回对端"""
    from web.routers.auth import _get_client_ip
    assert _get_client_ip(_make_request("203.0.113.5", "8.8.8.8")) == "203.0.113.5"
