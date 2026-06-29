"""SLA 指标埋点 + Prometheus 导出 (Q5)

参考:
- Prometheus client_python
- OpenMetrics standard
- RED method (Rate, Errors, Duration)

特性:
- 4 个 SLA 指标: request_count / request_latency / error_count / active_users
- Counter / Gauge / Histogram 三种 metric 类型
- /metrics endpoint Prometheus 兼容输出
- 不依赖 prometheus_client 库 (零依赖)
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Metric:
    """指标基类"""
    name: str
    help: str
    type: str = "counter"          # counter / gauge / histogram
    labels: list[str] = field(default_factory=list)
    values: dict = field(default_factory=dict)  # label_tuple -> value
    buckets: list[float] = field(default_factory=list)  # 仅 histogram


class SLAExporter:
    """SLA 指标导出器 (Prometheus 兼容)

    用法:
        exp = SLAExporter()
        exp.inc_request("/api/v1/chat", "200")
        exp.observe_latency("/api/v1/chat", 0.123)
        # /metrics 端点
        @app.get("/metrics")
        def metrics():
            return exp.export()
    """

    def __init__(self) -> None:
        """初始化 SLA 导出器并注册标准 SLA 指标."""
        self._metrics: dict[str, Metric] = {}
        # 默认 buckets (秒)
        self._default_buckets = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25,
                                    0.5, 1.0, 2.5, 5.0, 10.0]
        # 注册标准 SLA 指标
        self._register(
            "agent_requests_total", "Total HTTP requests", "counter",
            labels=["endpoint", "status"]
        )
        self._register(
            "agent_request_duration_seconds", "Request latency", "histogram",
            labels=["endpoint"], buckets=self._default_buckets
        )
        self._register(
            "agent_errors_total", "Total errors", "counter",
            labels=["type", "endpoint"]
        )
        self._register(
            "agent_active_users", "Active users", "gauge"
        )
        self._register(
            "agent_tool_calls_total", "Tool calls", "counter",
            labels=["tool", "status"]
        )
        self._register(
            "agent_llm_tokens_total", "LLM tokens used", "counter",
            labels=["model", "direction"]  # direction: prompt/completion
        )
        self._register(
            "agent_cache_hits_total", "Cache hits", "counter",
            labels=["layer"]  # L1/L2/L3
        )

    def _register(self, name: str, help_: str, type_: str,
                    labels: Optional[list[str]] = None,
                    buckets: Optional[list[float]] = None) -> None:
        self._metrics[name] = Metric(
            name=name, help=help_, type=type_,
            labels=labels or [],
            buckets=buckets or [],
        )

    def inc(self, metric: str, value: float = 1, **labels: Any) -> None:
        """递增 counter"""
        m = self._metrics.get(metric)
        if not m or m.type != "counter":
            return
        key = tuple(labels.get(l, "") for l in m.labels)
        m.values[key] = m.values.get(key, 0) + value

    def set(self, metric: str, value: float, **labels: Any) -> None:
        """设置 gauge"""
        m = self._metrics.get(metric)
        if not m or m.type != "gauge":
            return
        key = tuple(labels.get(l, "") for l in m.labels)
        m.values[key] = value

    def observe(self, metric: str, value: float, **labels: Any) -> None:
        """观察 histogram"""
        m = self._metrics.get(metric)
        if not m or m.type != "histogram":
            return
        key = tuple(labels.get(l, "") for l in m.labels)
        # 存储桶计数
        bucket_key = key + ("_bucket",)
        for b in m.buckets:
            b_k = key + (f"le={b}",)
            if value <= b:
                m.values[b_k] = m.values.get(b_k, 0) + 1
        # +Inf 桶
        inf_k = key + ("le=+Inf",)
        m.values[inf_k] = m.values.get(inf_k, 0) + 1
        # sum + count
        sum_k = key + ("_sum",)
        cnt_k = key + ("_count",)
        m.values[sum_k] = m.values.get(sum_k, 0) + value
        m.values[cnt_k] = m.values.get(cnt_k, 0) + 1

    # ─── 便捷方法 ───
    def inc_request(self, endpoint: str, status: str) -> None:
        """记录一次 HTTP 请求.

        Args:
            endpoint: 端点路径
            status: HTTP 状态码字符串
        """
        self.inc("agent_requests_total", endpoint=endpoint, status=status)

    def observe_latency(self, endpoint: str, seconds: float) -> None:
        """记录请求延迟.

        Args:
            endpoint: 端点路径
            seconds: 延迟秒数
        """
        self.observe("agent_request_duration_seconds", seconds, endpoint=endpoint)

    def inc_error(self, error_type: str, endpoint: str = "") -> None:
        """记录一次错误.

        Args:
            error_type: 错误类型
            endpoint: 关联端点, 默认空字符串
        """
        self.inc("agent_errors_total", type=error_type, endpoint=endpoint)

    def set_active_users(self, count: int) -> None:
        """设置当前活跃用户数 (gauge).

        Args:
            count: 活跃用户数
        """
        self.set("agent_active_users", float(count))

    def inc_tool_call(self, tool: str, status: str = "success") -> None:
        """记录一次工具调用.

        Args:
            tool: 工具名
            status: 调用状态, 默认 success
        """
        self.inc("agent_tool_calls_total", tool=tool, status=status)

    def inc_llm_tokens(self, model: str, direction: str, count: int) -> None:
        """记录 LLM token 消耗.

        Args:
            model: 模型名
            direction: prompt 或 completion
            count: token 数
        """
        self.inc("agent_llm_tokens_total", value=count, model=model, direction=direction)

    def inc_cache_hit(self, layer: str) -> None:
        """记录一次缓存命中.

        Args:
            layer: 缓存层 (L1/L2/L3)
        """
        self.inc("agent_cache_hits_total", layer=layer)

    # ─── Prometheus 格式导出 ───
    def export(self) -> str:
        """导出为 Prometheus exposition format"""
        lines = []
        for m in self._metrics.values():
            lines.append(f"# HELP {m.name} {m.help}")
            lines.append(f"# TYPE {m.name} {m.type}")
            if m.type == "counter":
                for k, v in m.values.items():
                    label_str = self._format_labels(m.labels, k)
                    lines.append(f"{m.name}{label_str} {v}")
            elif m.type == "gauge":
                for k, v in m.values.items():
                    label_str = self._format_labels(m.labels, k)
                    lines.append(f"{m.name}{label_str} {v}")
            elif m.type == "histogram":
                # 按主键分组输出 buckets
                groups: dict = {}
                for k, v in m.values.items():
                    base = k[:-1] if k[-1].startswith(("le=", "_sum", "_count")) else k
                    if base not in groups:
                        groups[base] = []
                    groups[base].append((k[-1], v))
                for base, items in groups.items():
                    for tag, v in items:
                        full_k = base + (tag,)
                        label_str = self._format_labels(m.labels + ["le"] if tag.startswith("le=") else m.labels, full_k)
                        if tag.startswith("le="):
                            lines.append(f"{m.name}_bucket{label_str} {v}")
                        elif tag == "_sum":
                            lines.append(f"{m.name}_sum{label_str} {v}")
                        elif tag == "_count":
                            lines.append(f"{m.name}_count{label_str} {v}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _format_labels(labels: list[str], values: tuple) -> str:
        if not labels or not values:
            return ""
        pairs = [f'{l}="{v}"' for l, v in zip(labels, values) if l]
        return "{" + ",".join(pairs) + "}" if pairs else ""


# 全局单例
_exporter: Optional[SLAExporter] = None


def get_sla_exporter() -> SLAExporter:
    """获取全局 SLA 导出器单例."""
    global _exporter
    if _exporter is None:
        _exporter = SLAExporter()
    return _exporter
