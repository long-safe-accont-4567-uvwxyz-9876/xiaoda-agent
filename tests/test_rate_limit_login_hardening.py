"""登录防爆破加固测试（VULN-28）。

背景：
    1. 限流中间件对所有内网 IP 自动放行（_is_private_ip 白名单）——公网部署时
       同 VPS/容器网段的攻击者可无限速爆破 WEBUI_PASSWORD；反代场景下所有
       客户端对端均为 127.0.0.1，爆破完全绕过限流。
    2. /api/v1/auth/login 无独立严格限流——即使配置了常规限流，
       60 req/min 对密码爆破仍然过于宽松。

修复：
    - 内网放行改为显式配置（RATE_LIMIT_TRUSTED_NETWORKS，默认仅回环）
    - login 端点使用独立严格桶（RATE_LIMIT_LOGIN，默认 10/min），
      且可信主机（含回环）不豁免 login 限流
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from web.middleware.rate_limit import RateLimitMiddleware


def _make_app(**mw_kwargs) -> FastAPI:
    app = FastAPI()
    defaults = dict(global_limit=600, user_limit=60, write_limit=30,
                    whitelist=set())
    defaults.update(mw_kwargs)
    app.add_middleware(RateLimitMiddleware, **defaults)

    @app.get("/api/v1/ping")
    async def ping():
        return {"ok": True}

    @app.post("/api/v1/auth/login")
    async def login():
        return {"ok": True}

    return app


# ── 1. 内网 IP 默认不再放行 ──

def test_private_ip_not_whitelisted_by_default():
    """192.168.x 等内网 IP 默认受限流约束（仅回环默认可信）"""
    app = _make_app(user_limit=2)
    client = TestClient(app, client=("192.168.1.100", 0))
    assert client.get("/api/v1/ping").status_code == 200
    assert client.get("/api/v1/ping").status_code == 200
    assert client.get("/api/v1/ping").status_code == 429


def test_loopback_still_whitelisted_by_default():
    """回环地址默认仍可信（本机 splash/健康检查不受影响）"""
    app = _make_app(user_limit=2)
    client = TestClient(app, client=("127.0.0.1", 0))
    for _ in range(10):
        assert client.get("/api/v1/ping").status_code == 200


# ── 2. 内网放行改为显式配置 ──

def test_trusted_networks_explicit_config(monkeypatch):
    """RATE_LIMIT_TRUSTED_NETWORKS 显式配置的内网段放行"""
    app = _make_app(user_limit=2,
                    trusted_networks=["192.168.1.0/24"])
    client = TestClient(app, client=("192.168.1.100", 0))
    for _ in range(10):
        assert client.get("/api/v1/ping").status_code == 200
    # 配置段外的内网 IP 仍受限
    client2 = TestClient(app, client=("192.168.2.100", 0))
    assert client2.get("/api/v1/ping").status_code == 200
    assert client2.get("/api/v1/ping").status_code == 200
    assert client2.get("/api/v1/ping").status_code == 429


def test_trusted_networks_env_config(monkeypatch):
    """环境变量 RATE_LIMIT_TRUSTED_NETWORKS 同样生效"""
    monkeypatch.setenv("RATE_LIMIT_TRUSTED_NETWORKS", "10.0.0.0/8")
    app = _make_app(user_limit=2)
    client = TestClient(app, client=("10.1.2.3", 0))
    for _ in range(10):
        assert client.get("/api/v1/ping").status_code == 200


# ── 3. login 端点独立严格限流，可信主机不豁免 ──

def test_login_rate_limited_for_loopback():
    """回环（反代场景所有客户端对端）对 login 仍受独立严格限流"""
    app = _make_app(login_limit=3)
    client = TestClient(app, client=("127.0.0.1", 0))
    for _ in range(3):
        assert client.post("/api/v1/auth/login").status_code == 200
    assert client.post("/api/v1/auth/login").status_code == 429


def test_login_rate_limited_for_trusted_network():
    """显式可信网段的 login 请求同样受限（防内网爆破）"""
    app = _make_app(login_limit=3, trusted_networks=["192.168.1.0/24"])
    client = TestClient(app, client=("192.168.1.100", 0))
    for _ in range(3):
        assert client.post("/api/v1/auth/login").status_code == 200
    assert client.post("/api/v1/auth/login").status_code == 429


def test_login_limit_independent_from_general_paths():
    """login 桶独立：login 被限后其它端点不受影响；其它端点耗尽不影响 login"""
    app = _make_app(user_limit=2, login_limit=5)
    client = TestClient(app)
    # 耗尽普通用户桶
    client.get("/api/v1/ping")
    client.get("/api/v1/ping")
    assert client.get("/api/v1/ping").status_code == 429
    # login 走独立桶，仍可用
    assert client.post("/api/v1/auth/login").status_code == 200
