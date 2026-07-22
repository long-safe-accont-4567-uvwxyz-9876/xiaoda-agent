"""web/routers/metrics.py — Prometheus /metrics 端点测试 (TDD)

覆盖:
    1. 默认情况下 GET /metrics 返回 200 + Prometheus 文本格式 (来自 localhost)
    2. METRICS_ENABLED=false 时不注册路由 -> 404
    3. 响应包含核心指标 (tool_exec_success_total / model_router_latency_seconds / memory_count)
    4. 响应包含进程级默认指标 (process_cpu_seconds / process_resident_memory_bytes / python_info)
    5. 端点响应时间 < 50ms (轻量, 无锁)
    6. 桥接 utils/metrics.py 的 4 类指标
    7. localhost 访问控制: 仅允许 127.0.0.1 / ::1 / localhost, 其他来源 403
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 模拟本机回环客户端 (host, port) — 与原 web/server.py 安全约束一致
# httpx ASGITransport 接受 client 参数指定请求来源
_LOCALHOST_CLIENT = ("127.0.0.1", 50000)
_LAN_CLIENT = ("192.168.1.100", 50000)


def _make_app_with_metrics_router() -> FastAPI:
    """构造一个仅注册 metrics router 的测试 app (不依赖 AgentCore).

    模拟 web/server.py 中 `METRICS_ENABLED=true` 时 include_router(metrics_router)
    的行为，便于在隔离环境中测试端点。
    """
    app = FastAPI()
    from web.routers.metrics import router as metrics_router
    app.include_router(metrics_router)
    return app


def _make_app_without_metrics_router() -> FastAPI:
    """构造一个不注册 metrics router 的测试 app.

    模拟 web/server.py 中 `METRICS_ENABLED=false` 时不 include_router 的行为。
    """
    app = FastAPI()
    return app


def _make_local_client(app: FastAPI) -> TestClient:
    """构造一个模拟本机回环访问的 TestClient.

    httpx ASGITransport 的 client 参数决定 request.client.host 的值.
    """
    return TestClient(app, client=_LOCALHOST_CLIENT)


def _make_lan_client(app: FastAPI) -> TestClient:
    """构造一个模拟局域网访问的 TestClient (非 localhost)."""
    return TestClient(app, client=_LAN_CLIENT)


def _metrics_enabled_from_env() -> bool:
    """模拟 web/server.py 中读取 METRICS_ENABLED 环境变量的逻辑."""
    return os.getenv("METRICS_ENABLED", "true").lower() in ("true", "1", "yes")


# ============================================================
# 1. 默认启用: GET /metrics 返回 200 + Prometheus 文本格式
# ============================================================
def test_metrics_endpoint_default_enabled():
    """默认 (METRICS_ENABLED 未设置) 时 /metrics 返回 200, content-type 为 Prometheus 文本格式."""
    # 清理环境变量，模拟默认场景
    old = os.environ.pop("METRICS_ENABLED", None)
    try:
        assert _metrics_enabled_from_env() is True, "默认应启用"
        app = _make_app_with_metrics_router()
        client = _make_local_client(app)
        r = client.get("/metrics")
        assert r.status_code == 200, f"status={r.status_code} body={r.text[:200]}"
        # Prometheus exposition format 是 text/plain
        ct = r.headers.get("content-type", "")
        assert ct.startswith("text/plain"), f"content-type={ct}"
        # 应包含 Prometheus 格式注释行 (# HELP / # TYPE)
        body = r.text
        assert "# HELP" in body, "缺少 # HELP 行"
        assert "# TYPE" in body, "缺少 # TYPE 行"
    finally:
        if old is not None:
            os.environ["METRICS_ENABLED"] = old


# ============================================================
# 2. 显式禁用: METRICS_ENABLED=false 时路由不注册 -> 404
# ============================================================
def test_metrics_endpoint_disabled(monkeypatch):
    """METRICS_ENABLED=false 时不注册 metrics router, GET /metrics 返回 404."""
    monkeypatch.setenv("METRICS_ENABLED", "false")
    assert _metrics_enabled_from_env() is False, "禁用时应为 False"
    # 模拟 server.py 的条件注册逻辑
    app = (
        _make_app_with_metrics_router()
        if _metrics_enabled_from_env()
        else _make_app_without_metrics_router()
    )
    client = _make_local_client(app)
    r = client.get("/metrics")
    assert r.status_code == 404, f"disabled 时应 404, got {r.status_code}"


# ============================================================
# 3. 响应包含核心指标
# ============================================================
def test_metrics_contains_core_metrics():
    """响应应包含 spec 要求的核心指标名.

    spec 要求: tool_exec_success_total, model_router_latency_seconds, memory_count
    (核心指标由 web/routers/metrics.py 预注册到 prometheus_client REGISTRY)
    """
    app = _make_app_with_metrics_router()
    client = _make_local_client(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    # spec 明确要求的核心指标 (即使值为 0 也应出现在 exposition 中)
    assert "tool_exec_success_total" in body, "缺少 tool_exec_success_total"
    assert "model_router_latency_seconds" in body, "缺少 model_router_latency_seconds"
    assert "memory_count" in body, "缺少 memory_count"


# ============================================================
# 4. 响应包含进程级默认指标
# ============================================================
def test_metrics_contains_process_metrics():
    """响应应包含 prometheus_client 默认 process/platform 指标."""
    app = _make_app_with_metrics_router()
    client = _make_local_client(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    # process_cpu_seconds_total 子串匹配 process_cpu_seconds
    assert "process_cpu_seconds" in body, "缺少 process_cpu_seconds 指标"
    assert "process_resident_memory_bytes" in body, "缺少 process_resident_memory_bytes"
    assert "python_info" in body, "缺少 python_info 指标"


# ============================================================
# 5. 端点响应时间 < 50ms
# ============================================================
def test_metrics_endpoint_performance():
    """端点应轻量, 5 次请求平均响应时间 < 50ms."""
    app = _make_app_with_metrics_router()
    client = _make_local_client(app)
    # 预热一次 (避免首次 include_router 延迟干扰)
    warmup = client.get("/metrics")
    assert warmup.status_code == 200
    times_ms: list[float] = []
    for _ in range(5):
        start = time.perf_counter()
        r = client.get("/metrics")
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        times_ms.append(elapsed_ms)
        assert r.status_code == 200
    avg_ms = sum(times_ms) / len(times_ms)
    assert avg_ms < 50.0, (
        f"端点平均响应时间 {avg_ms:.2f}ms 超过 50ms 阈值 (samples={times_ms})"
    )


# ============================================================
# 6. 桥接 utils/metrics.py 的 4 类指标
# ============================================================
def test_metrics_bridges_utils_metrics():
    """utils/metrics.py 中已记录的指标应被桥接到 Prometheus 输出.

    通过 utils.metrics 单例记录 counter/gauge/timer/histogram, 然后访问 /metrics
    验证输出包含对应桥接后的指标名。
    """
    from utils.metrics import metrics as utils_metrics

    # 记录 4 类指标
    utils_metrics.inc("tool_execute.test_tool.success", 1)
    utils_metrics.gauge("memory_count", 42)
    utils_metrics.observe("model_route.chat.duration", 0.5)
    utils_metrics.histogram("test_histogram_metric", 1.0)

    app = _make_app_with_metrics_router()
    client = _make_local_client(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text

    # counter 桥接: xiaoda_<name_with_underscores>_total
    assert "xiaoda_tool_execute_test_tool_success_total" in body, (
        "缺少桥接 counter 指标"
    )
    # gauge 桥接: xiaoda_<name_with_underscores>
    assert "xiaoda_memory_count" in body, "缺少桥接 gauge 指标"
    # timer 桥接为 histogram: xiaoda_<name_with_underscores>_seconds
    assert "xiaoda_model_route_chat_duration_seconds" in body, (
        "缺少桥接 timer 指标"
    )
    # histogram 桥接: xiaoda_<name_with_underscores>
    assert "xiaoda_test_histogram_metric" in body, "缺少桥接 histogram 指标"


# ============================================================
# 7. localhost 访问控制 (保留原 web/server.py 安全约束)
# ============================================================
class TestMetricsAccessControl:
    """验证 /metrics 端点的 localhost-only 访问限制.

    原 web/server.py:519-523 中 /metrics 端点限制了 localhost 访问,
    web/routers/metrics.py 重构后必须保留此约束, 避免在局域网暴露请求统计.
    """

    def test_rejects_lan_client_with_403(self):
        """非 localhost 来源 (局域网 IP) 应返回 403 Forbidden."""
        app = _make_app_with_metrics_router()
        client = _make_lan_client(app)
        r = client.get("/metrics")
        assert r.status_code == 403, (
            f"局域网应 403, got {r.status_code}"
        )
        # 403 响应体应为 JSON 错误
        assert r.json() == {"error": "Forbidden"}

    def test_rejects_public_ip_with_403(self):
        """公网 IP 来源应返回 403 Forbidden."""
        app = _make_app_with_metrics_router()
        client = TestClient(app, client=("8.8.8.8", 51000))
        r = client.get("/metrics")
        assert r.status_code == 403
        assert r.json() == {"error": "Forbidden"}

    def test_allows_ipv4_localhost(self):
        """127.0.0.1 来源应允许访问, 返回 200 + 指标."""
        app = _make_app_with_metrics_router()
        client = TestClient(app, client=("127.0.0.1", 50000))
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "# TYPE" in r.text

    def test_allows_ipv6_localhost(self):
        """::1 (IPv6 localhost) 来源应允许访问."""
        app = _make_app_with_metrics_router()
        client = TestClient(app, client=("::1", 50000))
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "# TYPE" in r.text

    def test_allows_localhost_hostname(self):
        """'localhost' 字符串来源应允许访问."""
        app = _make_app_with_metrics_router()
        client = TestClient(app, client=("localhost", 50000))
        r = client.get("/metrics")
        assert r.status_code == 200

    def test_forbidden_response_not_prometheus_format(self):
        """403 响应体不应包含 Prometheus 指标 (避免信息泄漏)."""
        app = _make_app_with_metrics_router()
        client = _make_lan_client(app)
        r = client.get("/metrics")
        assert r.status_code == 403
        # 403 响应体不应含任何 Prometheus 指标 (process_* / python_info 等)
        body = r.text
        assert "process_cpu_seconds" not in body
        assert "python_info" not in body
        assert "tool_exec_success_total" not in body

    def test_is_local_request_helper(self):
        """_is_local_request 辅助函数直接单元测试."""
        from web.routers.metrics import _is_local_request

        class _FakeRequest:
            """最小化的 Request mock, 仅含 client 属性."""
            class _Client:
                def __init__(self, host: str):
                    self.host = host

            def __init__(self, host: str):
                self.client = self._Client(host)

        # localhost 系列
        assert _is_local_request(_FakeRequest("127.0.0.1")) is True
        assert _is_local_request(_FakeRequest("::1")) is True
        assert _is_local_request(_FakeRequest("localhost")) is True

        # 非 localhost
        assert _is_local_request(_FakeRequest("192.168.1.100")) is False
        assert _is_local_request(_FakeRequest("8.8.8.8")) is False
        assert _is_local_request(_FakeRequest("10.0.0.1")) is False

    def test_is_local_request_handles_missing_client(self):
        """client 为 None 时 (异常情况) 应拒绝访问 (fail-closed)."""
        from web.routers.metrics import _is_local_request

        class _FakeRequest:
            client = None

        # client 为 None 时 host 为空, 不在 _ALLOWED_HOSTS 中, 返回 False
        assert _is_local_request(_FakeRequest()) is False


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
