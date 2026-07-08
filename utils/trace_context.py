"""Trace ID 上下文管理 — 基于 contextvars 的端到端请求追踪。

用法:
    # 中间件层（自动）
    from utils.trace_context import new_trace_id, get_trace_id
    tid = new_trace_id()          # 生成并存储到 contextvars

    # 任意层（自动）
    tid = get_trace_id()          # 获取当前请求的 trace_id

    # 日志自动绑定（由 logging_config.py patcher 处理，无需手动 bind）
"""
from __future__ import annotations

import random
from contextvars import ContextVar
from time import time

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")


def new_trace_id() -> str:
    """生成短 trace_id 并存入 contextvars。

    格式: {timestamp_ms 后6位}{random_hex4}，共10字符。
    示例: "834f21a7c3"
    """
    ts_part = f"{int(time() * 1000) % 1_000_000:06x}"
    rand_part = f"{random.randint(0, 0xFFFF):04x}"
    tid = ts_part + rand_part
    _trace_id_var.set(tid)
    return tid


def get_trace_id() -> str:
    """获取当前 contextvars 中的 trace_id，未设置时返回空串。"""
    return _trace_id_var.get()
