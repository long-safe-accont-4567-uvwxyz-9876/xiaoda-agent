"""Prometheus /metrics 端点 — 桥接 utils/metrics.py 4 类指标 + 进程级默认指标.

设计:
    1. 使用 prometheus_client 默认 REGISTRY, 自带 GC / 进程 / Python 平台指标
       (process_cpu_seconds_total / process_resident_memory_bytes / python_info 等)
    2. 显式注册 spec 要求的核心指标 (Counter / Histogram / Gauge), 即使值为 0
       也会出现在 exposition 输出中
    3. 注册自定义 Collector, 动态将 utils/metrics.Metrics 单例中的
       counter / timer / gauge / histogram 4 类指标桥接为 Prometheus 格式

指标命名约定 (桥接 utils/metrics.py):
    counter     -> xiaoda_<name_with_underscores>_total
    timer       -> xiaoda_<name_with_underscores>_seconds  (histogram)
    gauge       -> xiaoda_<name_with_underscores>
    histogram   -> xiaoda_<name_with_underscores>           (histogram)

性能:
    - generate_latest() 调用 prometheus_client 内部已优化的序列化路径
    - 自定义 Collector 在每次抓取时只读 utils/metrics 单例的 in-memory dict
    - 不持有锁, 不写盘, 不触发外部 IO — 满足 <50ms 响应预算
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    REGISTRY,
    generate_latest,
)
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)

# utils.metrics 是项目内全局单例, 桥接时直接读取其内部状态
from utils.metrics import metrics as _utils_metrics


router = APIRouter(tags=["metrics"])


# ============================================================
# 0. 访问控制: localhost-only (保留原 web/server.py 的安全约束)
# ============================================================
# /metrics 暴露进程/请求统计, 不应开放给局域网或外网.
# 仅允许本机回环地址访问; 其他来源返回 403.
# 与原 web/server.py 中 prometheus_metrics 端点的限制保持一致.
_ALLOWED_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_local_request(request: Request) -> bool:
    """判断请求来源是否为本机回环地址.

    Args:
        request: FastAPI Request 对象

    Returns:
        True 表示来自 localhost, 允许访问 /metrics
    """
    client = getattr(request, "client", None)
    client_host = getattr(client, "host", "") or ""
    return client_host in _ALLOWED_HOSTS


# ============================================================
# 1. 进程级 / 平台级默认指标 (prometheus_client 内置, 自动注册到默认 REGISTRY)
# ============================================================
# prometheus_client 在导入时已自动将 ProcessCollector / PlatformCollector
# 注册到默认 REGISTRY, 直接使用即可:
#   - process_cpu_seconds_total / process_resident_memory_bytes
#   - process_virtual_memory_bytes / process_open_fds / process_start_time_seconds
#   - python_info{version, implementation, major, minor, ...}
# 无需手动注册 (手动注册会触发 "Duplicated timeseries" 错误).


# ============================================================
# 2. spec 要求的核心指标 (预注册, 值为 0 时也输出)
# ============================================================
# 模块可能被多次导入 (测试 reload / uvicorn --reload), 重复注册同名校验
# 会抛 ValueError, 用 try/except 保证幂等.
def _safe_register(factory):
    """注册指标到默认 REGISTRY, 已存在时返回已注册实例."""
    try:
        return factory()
    except ValueError:
        # 同名 metric 已注册, 从 REGISTRY 中取回已有实例
        return None


TOOL_EXEC_SUCCESS_TOTAL = _safe_register(
    lambda: Counter(
        "tool_exec_success_total",
        "Total number of successful tool executions",
        registry=REGISTRY,
    )
)
MODEL_ROUTER_LATENCY_SECONDS = _safe_register(
    lambda: Histogram(
        "model_router_latency_seconds",
        "Model router call latency in seconds",
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
        registry=REGISTRY,
    )
)
MEMORY_COUNT = _safe_register(
    lambda: Gauge(
        "memory_count",
        "Current number of memory entries",
        registry=REGISTRY,
    )
)


# ============================================================
# 3. 自定义 Collector: 桥接 utils/metrics.Metrics 4 类指标
# ============================================================
# 默认 histogram 桶 (与 SLAExporter 一致, 覆盖 5ms ~ 10s)
_DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _sanitize_name(name: str) -> str:
    """将 utils/metrics.py 中的点分指标名转为 Prometheus 合法名.

    Args:
        name: 原始指标名 (如 "model_route.chat.duration")

    Returns:
        Prometheus 合法指标名 (如 "model_route_chat_duration")
    """
    return name.replace(".", "_").replace("-", "_")


class _UtilsMetricsCollector:
    """将 utils/metrics.Metrics 单例的 4 类指标桥接为 Prometheus exposition.

    collect() 由 prometheus_client 在 generate_latest() 时调用,
    读取 utils.metrics 单例的 in-memory dict, 不持有锁, 不写盘.
    """

    def collect(self):  # type: ignore[override]
        # ── counter ──
        for name, value in _utils_metrics._counters.items():
            metric_name = "xiaoda_" + _sanitize_name(name) + "_total"
            yield CounterMetricFamily(
                metric_name,
                f"Counter bridged from utils/metrics.py: {name}",
                value=float(value),
            )

        # ── gauge ──
        for name, value in _utils_metrics._gauges.items():
            metric_name = "xiaoda_" + _sanitize_name(name)
            yield GaugeMetricFamily(
                metric_name,
                f"Gauge bridged from utils/metrics.py: {name}",
                value=float(value),
            )

        # ── timer -> histogram ──
        for name, durations in _utils_metrics._timers.items():
            if not durations:
                continue
            metric_name = "xiaoda_" + _sanitize_name(name) + "_seconds"
            yield self._build_histogram(
                metric_name,
                f"Timer bridged from utils/metrics.py: {name}",
                durations,
            )

        # ── histogram ──
        for name, values in _utils_metrics._histograms.items():
            if not values:
                continue
            metric_name = "xiaoda_" + _sanitize_name(name)
            yield self._build_histogram(
                metric_name,
                f"Histogram bridged from utils/metrics.py: {name}",
                values,
            )

    @staticmethod
    def _build_histogram(
        metric_name: str, help_text: str, samples: list[float]
    ) -> HistogramMetricFamily:
        """构造 HistogramMetricFamily, 桶按累积计数输出."""
        hist = HistogramMetricFamily(metric_name, help_text)
        sorted_samples = sorted(samples)
        n = len(sorted_samples)
        for b in _DEFAULT_BUCKETS:
            # 累积计数: <= b 的样本数
            count = sum(1 for s in sorted_samples if s <= b)
            hist.add_sample(
                metric_name + "_bucket",
                {"le": str(b)},
                value=count,
            )
        # +Inf 桶
        hist.add_sample(
            metric_name + "_bucket",
            {"le": "+Inf"},
            value=n,
        )
        hist.add_sample(metric_name + "_sum", {}, value=sum(samples))
        hist.add_sample(metric_name + "_count", {}, value=n)
        return hist


# 注册自定义 collector (幂等, 重复注册会抛 ValueError, 捕获即可)
try:
    REGISTRY.register(_UtilsMetricsCollector())
except ValueError:
    # 已注册 (例如 reload), 跳过
    logger.debug("metrics.collector_already_registered")


# ============================================================
# 4. /metrics 端点
# ============================================================
@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus 格式指标端点.

    访问控制: 仅允许 localhost 回环地址访问; 其他来源返回 403 Forbidden.
    避免在局域网或公网暴露进程/请求统计.

    Args:
        request: FastAPI Request 对象, 用于验证 client 来源

    Returns:
        200 OK, body 为 Prometheus exposition format 文本,
        Content-Type 为 text/plain; version=0.0.4; charset=utf-8
        403 Forbidden 若非本机回环地址访问
    """
    if not _is_local_request(request):
        logger.warning(
            "metrics.access_denied_non_localhost",
            client_host=getattr(getattr(request, "client", None), "host", ""),
        )
        return JSONResponse(
            status_code=403,
            content={"error": "Forbidden"},
        )
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
