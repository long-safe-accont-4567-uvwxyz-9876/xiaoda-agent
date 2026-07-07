"""6 级恢复编排 (Dr3) — 自动恢复 + 反模式注册

参考:
- Chaos Engineering Recovery Patterns
- Site Reliability Engineering (Google)
- Recursive self-improvement

6 级恢复:
1. RETRY         - 简单重试 (3 次)
2. BACKOFF       - 指数退避 (1s, 2s, 4s, 8s, 16s)
3. FALLBACK      - 降级备选方案
4. RECONFIGURE   - 重新配置 (切换 provider / 关闭可选模块)
5. RESTART       - 局部重启 (重启子进程 / 重连)
6. ESCALATE      - 上报人工介入

特性:
- 自动选择恢复级别 (根据错误类型)
- 反模式注册 (失败模式 → 恢复策略)
- 递归改进 (恢复失败后升级到下一级)
- 恢复审计日志
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from loguru import logger


class RecoveryLevel(int, Enum):
    """故障恢复级别枚举，从重试到逐级升级共六档。"""
    RETRY = 1
    BACKOFF = 2
    FALLBACK = 3
    RECONFIGURE = 4
    RESTART = 5
    ESCALATE = 6


class RecoveryError(Exception):
    """恢复失败"""


@dataclass
class RecoveryContext:
    """恢复上下文"""
    operation: str
    args: dict = field(default_factory=dict)
    error: Optional[Exception] = None
    attempt: int = 0
    level: RecoveryLevel = RecoveryLevel.RETRY
    history: list[dict] = field(default_factory=list)


@dataclass
class RecoveryResult:
    """恢复结果"""
    success: bool
    level_used: RecoveryLevel = RecoveryLevel.RETRY
    result: Any = None
    duration: float = 0
    attempts: int = 0


class RecoveryOrchestrator:
    """6 级恢复编排器

    用法:
        orch = RecoveryOrchestrator()
        # 注册恢复策略
        orch.register_fallback("search_web", lambda **kw: "cached_result")
        # 执行可恢复操作
        result = await orch.execute(
            operation="search_web",
            handler=search_web_handler,
            args={"q": "weather"},
        )
    """

    def __init__(self, max_level: RecoveryLevel = RecoveryLevel.ESCALATE,
                 backoff_delays: list[float] | None = None) -> None:
        self._fallbacks: dict[str, Callable] = {}
        self._reconfigure_handlers: dict[str, Callable] = {}
        self._restart_handlers: dict[str, Callable] = {}
        self._anti_patterns: dict[str, dict] = {}  # 反模式注册
        self._max_level = max_level
        self._backoff_delays = backoff_delays or [1, 2, 4, 8, 16]
        self._audit_log: list[dict] = []
        self._stats = {"total": 0, "success": 0, "failed": 0,
                         "by_level": {l.name: 0 for l in RecoveryLevel}}

    def register_fallback(self, operation: str,
                            handler: Callable) -> None:
        """注册降级方案"""
        self._fallbacks[operation] = handler

    def register_reconfigure(self, operation: str,
                              handler: Callable) -> None:
        """注册重新配置处理器"""
        self._reconfigure_handlers[operation] = handler

    def register_restart(self, operation: str,
                          handler: Callable) -> None:
        """注册重启处理器"""
        self._restart_handlers[operation] = handler

    def register_anti_pattern(self, error_pattern: str,
                                strategy: dict) -> None:
        """注册反模式 (错误模式 → 推荐恢复策略)"""
        self._anti_patterns[error_pattern] = strategy

    def _select_initial_level(self, error: Exception) -> RecoveryLevel:
        """根据错误类型选择初始恢复级别"""
        err_str = str(error).lower()
        # 检查反模式注册
        for pattern, strategy in self._anti_patterns.items():
            if pattern.lower() in err_str:
                level_str = strategy.get("level", "RETRY")
                try:
                    return RecoveryLevel[level_str]
                except KeyError as e:
                    logger.debug("recovery_orchestrator.invalid_recovery_level", exc_info=True)
        # 默认规则
        if "timeout" in err_str or "connection" in err_str:
            return RecoveryLevel.BACKOFF
        if "rate limit" in err_str or "429" in err_str:
            return RecoveryLevel.BACKOFF
        if "auth" in err_str or "401" in err_str or "403" in err_str:
            return RecoveryLevel.RECONFIGURE
        if "not found" in err_str or "404" in err_str:
            return RecoveryLevel.FALLBACK
        if "memory" in err_str or "oom" in err_str:
            return RecoveryLevel.RESTART
        return RecoveryLevel.RETRY

    async def execute(self, operation: str, handler: Callable,
                       args: Optional[dict] = None,
                       max_level: Optional[RecoveryLevel] = None
                       ) -> RecoveryResult:
        """执行可恢复操作

        自动按级别尝试恢复, 直到成功或达到最大级别
        """
        args = args or {}
        max_lvl = max_level or self._max_level
        t0 = time.time()
        ctx = RecoveryContext(operation=operation, args=args)

        # 首次执行
        result = await self._execute_first_attempt(
            ctx, handler, args, operation, t0)
        if result is not None:
            return result

        # 按级别递进恢复
        result = await self._run_recovery_levels(
            ctx, handler, max_lvl, operation, t0)
        if result is not None:
            return result

        # 全部失败
        self._stats["total"] += 1
        self._stats["failed"] += 1
        self._audit_log.append({
            "operation": operation, "success": False,
            "level": ctx.level.name, "attempts": ctx.attempt,
            "duration": time.time() - t0,
        })
        return RecoveryResult(
            success=False, level_used=ctx.level,
            duration=time.time() - t0, attempts=ctx.attempt,
        )

    async def _execute_first_attempt(self, ctx: RecoveryContext, handler: Callable,
                                      args: dict, operation: str, t0: float
                                      ) -> Optional[RecoveryResult]:
        """首次执行: 成功返回 RecoveryResult; 失败返回 None 并设置 ctx 的初始恢复级别"""
        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(**args)
            else:
                result = await asyncio.to_thread(handler, **args)
            self._stats["total"] += 1
            self._stats["success"] += 1
            self._audit_log.append({
                "operation": operation, "success": True,
                "level": "none", "duration": time.time() - t0,
            })
            return RecoveryResult(
                success=True, result=result,
                level_used=RecoveryLevel.RETRY,  # 实际未触发
                duration=time.time() - t0, attempts=1,
            )
        except Exception as e:
            ctx.error = e
            ctx.level = self._select_initial_level(e)
            ctx.attempt = 1  # 首次执行失败, attempt 计为 1
            logger.info(f"Recovery.start op={operation} "
                         f"initial_level={ctx.level.name} error={str(e)[:100]}")
            return None

    async def _run_recovery_levels(self, ctx: RecoveryContext, handler: Callable,
                                    max_lvl: RecoveryLevel, operation: str,
                                    t0: float) -> Optional[RecoveryResult]:
        """按级别递进恢复: 成功返回 RecoveryResult, 全部失败返回 None"""
        while ctx.level <= max_lvl:
            ctx.attempt += 1  # 每个级别尝试计为一次执行
            try:
                result = await self._try_recover(ctx, handler)
                self._stats["total"] += 1
                self._stats["success"] += 1
                self._stats["by_level"][ctx.level.name] += 1
                self._audit_log.append({
                    "operation": operation, "success": True,
                    "level": ctx.level.name, "attempts": ctx.attempt,
                    "duration": time.time() - t0,
                })
                return RecoveryResult(
                    success=True, result=result,
                    level_used=ctx.level,
                    duration=time.time() - t0,
                    attempts=ctx.attempt,
                )
            except RecoveryError as e:
                logger.warning(f"Recovery.level_{ctx.level.name}_failed "
                                f"op={operation} attempt={ctx.attempt} "
                                f"error={str(e)[:100]}")
                ctx.history.append({
                    "level": ctx.level.name, "error": str(e)[:200],
                    "ts": time.time(),
                })
                # 升级到下一级
                next_level = RecoveryLevel(min(ctx.level.value + 1, max_lvl.value))
                if next_level == ctx.level:
                    break
                ctx.level = next_level
            except Exception as e:
                logger.error(f"Recovery.unexpected_error op={operation} "
                               f"level={ctx.level.name} error={e}")
                ctx.history.append({
                    "level": ctx.level.name, "error": str(e)[:200],
                    "ts": time.time(),
                })
                next_level = RecoveryLevel(min(ctx.level.value + 1, max_lvl.value))
                if next_level == ctx.level:
                    break
                ctx.level = next_level
        return None

    async def _try_recover(self, ctx: RecoveryContext,
                             handler: Callable) -> Any:
        """尝试指定级别的恢复"""
        lvl = ctx.level

        if lvl == RecoveryLevel.RETRY:
            # 直接重试
            return await self._invoke_handler(handler, ctx.args)

        if lvl == RecoveryLevel.BACKOFF:
            # 指数退避
            return await self._recover_with_backoff(handler, ctx.args)

        if lvl == RecoveryLevel.FALLBACK:
            # 降级备选
            fb = self._fallbacks.get(ctx.operation)
            if not fb:
                raise RecoveryError(f"No fallback registered for {ctx.operation}")
            return await self._invoke_handler(fb, ctx.args)

        if lvl == RecoveryLevel.RECONFIGURE:
            # 重新配置
            rc = self._reconfigure_handlers.get(ctx.operation)
            if rc:
                await self._invoke_handler(rc, ctx.args)
            # 配置完后重试
            return await self._invoke_handler(handler, ctx.args)

        if lvl == RecoveryLevel.RESTART:
            # 重启子模块
            rs = self._restart_handlers.get(ctx.operation)
            if rs:
                await self._invoke_handler(rs, ctx.args)
            # 重启后重试
            return await self._invoke_handler(handler, ctx.args)

        if lvl == RecoveryLevel.ESCALATE:
            # 上报人工
            self._escalate_to_human(ctx)
            raise RecoveryError(f"Escalated to human, operation={ctx.operation}")

        raise RecoveryError(f"Unknown level {lvl}")

    async def _invoke_handler(self, handler: Callable, args: dict) -> Any:
        """根据 handler 是否为协程函数选择 await 或 to_thread 执行"""
        if asyncio.iscoroutinefunction(handler):
            return await handler(**args)
        return await asyncio.to_thread(handler, **args)

    async def _recover_with_backoff(self, handler: Callable, args: dict) -> Any:
        """指数退避重试 (默认 1/2/4/8/16s), 全部失败抛出 RecoveryError"""
        delays = self._backoff_delays
        for i, d in enumerate(delays):
            await asyncio.sleep(d)
            try:
                return await self._invoke_handler(handler, args)
            except Exception:
                if i == len(delays) - 1:
                    raise RecoveryError(f"Backoff exhausted after {len(delays)} retries")
        raise RecoveryError("Backoff failed")

    def _escalate_to_human(self, ctx: RecoveryContext) -> None:
        """上报人工介入 (通过 self_diagnostic 回调机制)"""
        try:
            from core.self_diagnostic import get_self_diagnostic, ReportLevel, SelfReport
            diag = get_self_diagnostic()
            # 通过回调机制上报
            report = SelfReport(
                level=ReportLevel.CRITICAL,
                category="recovery",
                message=f"Recovery escalated for {ctx.operation}: "
                          f"{str(ctx.error)[:100]}",
                metrics={
                    "operation": ctx.operation,
                    "attempts": ctx.attempt,
                    "level": ctx.level.name,
                },
            )
            # 直接通知
            for cb in diag._callbacks:
                try:
                    if asyncio.iscoroutinefunction(cb):
                        asyncio.create_task(cb(report))
                    else:
                        cb(report)
                except Exception:
                    pass
        except Exception as e:
            logger.debug("recovery_orchestrator.escalate_to_human_failed", exc_info=True)

    def stats(self) -> dict:
        """返回恢复统计 (含总数/成功/失败/各级别计数/审计日志大小)."""
        return {**self._stats, "audit_log_size": len(self._audit_log)}

    def get_audit_log(self, limit: int = 50) -> list[dict]:
        """返回最近 N 条恢复审计日志.

        Args:
            limit: 返回的最大日志数, 默认 50

        Returns:
            审计日志列表 (按时间升序)
        """
        return self._audit_log[-limit:]


# 全局单例
_orch: Optional[RecoveryOrchestrator] = None


def get_recovery_orchestrator() -> RecoveryOrchestrator:
    """获取全局 RecoveryOrchestrator 单例, 不存在时创建."""
    global _orch
    if _orch is None:
        _orch = RecoveryOrchestrator()
    return _orch
