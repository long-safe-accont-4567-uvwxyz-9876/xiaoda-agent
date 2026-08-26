"""tools/anysearch_client.py — AnySearch 统一搜索 REST 客户端.

依据《AnySearch 使用手册》（2026-08-22 版，https://www.coze.cn/s/qBK5eb8QVoE/）：
- 统一信封：成功 code:0；失败 code:-1 带 error_code；request_id 服务端生成。
- 认证：Bearer Key（env ANYSEARCH_API_KEY）；Key 非法/禁用/过期返回 401/403，
  **不会降级为匿名调用**——本客户端以 PermissionError 上抛，由引擎链切换其他引擎。
- 服务端做意图识别与分层路由（含新鲜度意图自动转实时数据），客户端只发 query。

开关语义（默认关，不改变既有引擎链行为）：
- ANYSEARCH_API_KEY 非空 → 开启（认证调用）；
- ANYSEARCH_ENABLED=true 且无 Key → 开启匿名调用（按 IP 限流，每日免费额度）；
- 两者皆无 → 关闭。

熔断器：连续失败 3 次后熔断 10 分钟（服务不可达时避免每次搜索都白等超时），
冷却结束自动半开重试。
"""
from __future__ import annotations

import os
import time
from typing import Any

import httpx
from loguru import logger

from config_constants import env_flag

_BASE_URL = "https://api.anysearch.com"
_REQUEST_TIMEOUT = 8.0
_CLIENT_HEADER = {"X-Anysearch-Client": "ai-agent/1.0"}

# 熔断器（进程内）
_BREAK_FAIL_THRESHOLD = 3
_BREAK_COOLDOWN_S = 600.0
_fail_streak = 0
_break_until = 0.0


class AnySearchAuthError(PermissionError):
    """Key 非法/禁用/过期（401/403）。按手册语义不降级匿名，上抛切换引擎。"""


def _breaker_open(now: float | None = None) -> bool:
    now = now if now is not None else time.monotonic()
    return now < _break_until


def _record_success() -> None:
    global _fail_streak
    _fail_streak = 0


def _record_failure() -> None:
    global _fail_streak, _break_until
    _fail_streak += 1
    if _fail_streak >= _BREAK_FAIL_THRESHOLD:
        _break_until = time.monotonic() + _BREAK_COOLDOWN_S
        logger.warning("anysearch.breaker_open fails={} cooldown_s={}",
                       _fail_streak, _BREAK_COOLDOWN_S)


def anysearch_available() -> bool:
    """是否启用 AnySearch：有 Key 或显式 ANYSEARCH_ENABLED，且熔断器未开。"""
    if _breaker_open():
        return False
    if os.getenv("ANYSEARCH_API_KEY", "").strip():
        return True
    return env_flag("ANYSEARCH_ENABLED", False)


def _http_post_json(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST JSON 并解析响应（独立函数便于测试注入）。非 2xx / 信封失败均上抛。"""
    headers = dict(_CLIENT_HEADER)
    key = os.getenv("ANYSEARCH_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    with httpx.Client(timeout=_REQUEST_TIMEOUT, headers=headers) as client:
        resp = client.post(f"{_BASE_URL}{path}", json=payload)
    if resp.status_code in (401, 403):
        raise AnySearchAuthError(f"anysearch key 无效或被拒(status={resp.status_code})")
    resp.raise_for_status()
    return resp.json()


def anysearch_search_sync(query: str, max_results: int = 8) -> tuple[list[dict], str]:
    """AnySearch 通用搜索。返回 (results, ai_answer)，与 Tavily 路径同构。

    结果字段映射：title/url/snippet→content(摘要)/content→content(正文)/date。
    失败（网络/信封 code≠0/熔断）上抛异常，由调用方降级到下一引擎。
    """
    if _breaker_open():
        raise ConnectionError("anysearch 熔断中")
    payload: dict[str, Any] = {
        "query": query,
        "max_results": max(1, min(int(max_results), 20)),
        "language": "zh-CN",
        "format": "json",
    }
    try:
        data = _http_post_json("/v1/search", payload)
    except AnySearchAuthError:
        # 认证失败计入熔断：Key 问题在修复前不该反复打
        _record_failure()
        raise
    except (RuntimeError, OSError, ValueError, httpx.HTTPError) as e:
        _record_failure()
        raise ConnectionError(f"anysearch 请求失败: {e!s:.200}") from e

    if data.get("code") != 0:
        _record_failure()
        error_code = data.get("error_code", "unknown")
        raise RuntimeError(f"anysearch error_code={error_code} message={data.get('message', '')[:120]}")

    _record_success()
    # 信封内层结构手册未完全给出——兼容顶层 results 与 data.results 两种形态
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    results: list[dict] = []
    for r in body.get("results", []) or []:
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content") or r.get("snippet", ""),
            "snippet": r.get("snippet", ""),
            "date": r.get("date") or r.get("published_date", ""),
        })
    answer = body.get("answer", "") or ""
    if data.get("request_id"):
        logger.debug("anysearch.ok request_id={} results={}",
                     data.get("request_id"), len(results))
    return results, answer
