"""FaultInjectingLLMClient — 在 LLM 调用中注入故障的混沌工程客户端 (P1 Chaos Engineering)

参考:
- Chaos Engineering 原则: 主动注入故障验证系统韧性
- core/degradation_strategy.py (4 级降级策略)
- core/degradation_detector.py (三轴退化检测)
- tests/fault_injection.py (早期单故障版本, 此处为多类型 + 可复现版本)

故障类型:
- timeout: 抛出 asyncio.TimeoutError (模拟请求超时, 永远不返回正常结果)
- error:   抛出 LLMFaultError (模拟 API 错误, 如 500/429/502/503)
- slow:    延迟 10s 后返回真实结果 (模拟慢响应)
- empty:   返回空字符串 (模拟空响应, content="")

故障选择:
- 每次调用按 fault_rate 决定是否注入 (random < fault_rate)
- 若注入, 从 fault_types 中均匀随机选一个

可复现性:
- 设置 seed 后, 同一序列的调用会产生相同的故障注入序列
- 适用于回归测试与混沌实验复现

用法:
    from chaos.fault_injecting_llm_client import FaultConfig, FaultInjectingLLMClient

    cfg = FaultConfig(fault_rate=0.3, seed=42)
    client = FaultInjectingLLMClient(real_llm_client, cfg)
    try:
        reply = await client.chat(messages=[...], task_type="chat")
    except asyncio.TimeoutError:
        # 处理超时 (触发 reliability 退化检测)
        ...
    stats = client.get_stats()  # {total_calls, faults_injected, by_type}
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional
from collections.abc import AsyncIterator

from loguru import logger


# ============================================================
# 常量与枚举
# ============================================================

# 慢响应注入的延迟 (秒)
SLOW_FAULT_DELAY_SECONDS: float = 10.0

# error 故障可选的 HTTP 状态码
ERROR_FAULT_CODES: tuple[int, ...] = (500, 429, 502, 503)

# 支持的故障类型字符串
VALID_FAULT_TYPES: tuple[str, ...] = ("timeout", "error", "slow", "empty")


class FaultType(str, Enum):
    """故障类型枚举 (str 子类便于 JSON 序列化)"""

    TIMEOUT = "timeout"
    ERROR = "error"
    SLOW = "slow"
    EMPTY = "empty"


# ============================================================
# 异常
# ============================================================

class LLMFaultError(Exception):
    """故障注入产生的 LLM 错误 (模拟 API 错误)

    属性:
        message: 错误消息
        code:    模拟的 HTTP 状态码 (500/429/502/503)
        fault_type: 故障类型字符串 (固定为 "error")
    """

    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.fault_type = "error"

    def __str__(self) -> str:
        return f"[fault error {self.code}] {self.message}"


# ============================================================
# 故障配置
# ============================================================

@dataclass
class FaultConfig:
    """故障注入配置

    属性:
        fault_rate:  故障注入概率 (0-1), 默认 0.1
        fault_types: 故障类型列表 (从其中随机选一个), 默认 4 种全开
        seed:        随机种子, 设置后可复现故障序列, None 表示不可复现
    """
    fault_rate: float = 0.1
    fault_types: list[str] = field(
        default_factory=lambda: list(VALID_FAULT_TYPES)
    )
    seed: int | None = None

    def __post_init__(self) -> None:
        # 校验 fault_rate 范围
        if not 0.0 <= self.fault_rate <= 1.0:
            raise ValueError(
                f"fault_rate 必须在 [0, 1] 范围内, 当前值: {self.fault_rate}"
            )
        # 校验 fault_types 非空且均为合法类型
        if not self.fault_types:
            raise ValueError("fault_types 不能为空")
        invalid = [t for t in self.fault_types if t not in VALID_FAULT_TYPES]
        if invalid:
            raise ValueError(
                f"不支持的故障类型: {invalid}, 合法类型: {VALID_FAULT_TYPES}"
            )


# ============================================================
# 故障注入 LLM 客户端
# ============================================================

class FaultInjectingLLMClient:
    """故障注入 LLM 客户端 — 包装真实 LLM client, 按概率注入故障

    包装模式: 不修改真实 client, 仅在外层包裹故障注入逻辑.
    真实 client 需要实现 async `chat(*args, **kwargs)` 与
    async `chat_stream(*args, **kwargs)` 接口 (返回字符串 / 异步迭代器).

    用法:
        cfg = FaultConfig(fault_rate=0.3, seed=42)
        client = FaultInjectingLLMClient(real_client, cfg)
        reply = await client.chat(messages=[...], task_type="chat")
        stats = client.get_stats()
    """

    def __init__(
        self,
        real_client: Any,
        config: FaultConfig,
    ) -> None:
        if real_client is None:
            raise ValueError("real_client 不能为 None")
        if not isinstance(config, FaultConfig):
            raise TypeError("config 必须是 FaultConfig 实例")
        self._real = real_client
        self._config = config
        # 使用独立 Random 实例, 避免污染全局 random 状态
        self._rng = random.Random(config.seed)
        # 故障注入统计
        self._stats: dict[str, Any] = {
            "total_calls": 0,
            "faults_injected": 0,
            "by_type": {t: 0 for t in VALID_FAULT_TYPES},
        }
        # 故障注入历史 (用于调试与验证)
        self._fault_log: list[dict[str, Any]] = []

    # ─── 公共属性 ───

    @property
    def config(self) -> FaultConfig:
        """当前故障配置"""
        return self._config

    @property
    def fault_rate(self) -> float:
        """当前故障率"""
        return self._config.fault_rate

    # ─── 核心调用: chat ───

    async def chat(self, *args: Any, **kwargs: Any) -> str:
        """调用真实 client 的 chat, 按概率注入故障

        - timeout: 抛出 asyncio.TimeoutError
        - error:   抛出 LLMFaultError (随机 500/429/502/503)
        - slow:    延迟 10s 后调用真实 client 返回结果
        - empty:   直接返回空字符串 ""
        - 不注入:   调用真实 client 返回结果
        """
        self._stats["total_calls"] += 1

        if self._should_inject():
            fault_type = self._pick_fault_type()
            context = self._build_context(args, kwargs)
            self._record_injection(fault_type, context)
            logger.debug(
                f"FaultInject.chat 注入故障: type={fault_type} "
                f"call#{self._stats['total_calls']}"
            )
            if fault_type == "timeout":
                # 永远不返回正常结果 (抛出超时异常)
                raise TimeoutError(
                    "FaultInject: 注入超时故障 (LLM 永远不返回)"
                )
            if fault_type == "error":
                code = self._rng.choice(ERROR_FAULT_CODES)
                raise LLMFaultError(
                    f"FaultInject: 注入 API 错误 (code={code})", code=code
                )
            if fault_type == "slow":
                # 延迟后正常返回 (模拟慢响应)
                await asyncio.sleep(SLOW_FAULT_DELAY_SECONDS)
                return await self._real.chat(*args, **kwargs)
            if fault_type == "empty":
                # 返回空响应
                return ""
            # 理论上不会到达 (FaultConfig 校验已过滤)
            raise ValueError(f"未知故障类型: {fault_type}")

        # 不注入, 调用真实 client
        return await self._real.chat(*args, **kwargs)

    # ─── 核心调用: chat_stream ───

    async def chat_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[str]:
        """调用真实 client 的 chat_stream, 按概率注入故障 (流式版本)

        故障表现:
        - timeout: 抛出 asyncio.TimeoutError (在产出第一个 chunk 前)
        - error:   抛出 LLMFaultError
        - slow:    延迟 10s 后透传真实流
        - empty:   立即结束 (不产出任何 chunk)
        - 不注入:   透传真实流
        """
        self._stats["total_calls"] += 1

        if self._should_inject():
            fault_type = self._pick_fault_type()
            context = self._build_context(args, kwargs)
            self._record_injection(fault_type, context)
            logger.debug(
                f"FaultInject.chat_stream 注入故障: type={fault_type} "
                f"call#{self._stats['total_calls']}"
            )
            if fault_type == "timeout":
                raise TimeoutError(
                    "FaultInject: 注入超时故障 (stream 永远不返回)"
                )
            if fault_type == "error":
                code = self._rng.choice(ERROR_FAULT_CODES)
                raise LLMFaultError(
                    f"FaultInject: 注入 API 错误 (stream, code={code})", code=code
                )
            if fault_type == "slow":
                await asyncio.sleep(SLOW_FAULT_DELAY_SECONDS)
                # 透传真实流
                async for chunk in self._real.chat_stream(*args, **kwargs):
                    yield chunk
                return
            if fault_type == "empty":
                # 空流, 立即结束
                return
            raise ValueError(f"未知故障类型: {fault_type}")

        # 不注入, 透传真实流
        async for chunk in self._real.chat_stream(*args, **kwargs):
            yield chunk

    # ─── 故障注入内部控制 ───

    def _should_inject(self) -> bool:
        """按 fault_rate 决定本次调用是否注入故障"""
        return self._rng.random() < self._config.fault_rate

    def _pick_fault_type(self) -> str:
        """从 fault_types 中均匀随机选一个"""
        return self._rng.choice(self._config.fault_types)

    @staticmethod
    def _build_context(args: tuple, kwargs: dict) -> dict:
        """构造故障上下文 (用于记录, 截断避免日志膨胀)"""
        try:
            args_repr = repr(args)[:200]
        except Exception:
            args_repr = "<unreprable>"
        try:
            kwargs_repr = repr(kwargs)[:200]
        except Exception:
            kwargs_repr = "<unreprable>"
        return {
            "args": args_repr,
            "kwargs": kwargs_repr,
        }

    # ─── 故障记录与统计 ───

    def record_fault(self, type: str, context: dict) -> None:
        """记录一次注入的故障 (公共 API, 便于外部扩展记录)

        参数:
            type:    故障类型 (timeout/error/slow/empty)
            context: 上下文字典 (调用参数等)
        """
        if type not in VALID_FAULT_TYPES:
            logger.warning(f"record_fault: 未知故障类型 {type!r}, 已忽略")
            return
        self._fault_log.append({"type": type, "context": context})

    def _record_injection(self, fault_type: str, context: dict) -> None:
        """内部: 记录一次注入 (统计 + 日志)"""
        self._stats["faults_injected"] += 1
        self._stats["by_type"][fault_type] = (
            self._stats["by_type"].get(fault_type, 0) + 1
        )
        self.record_fault(fault_type, context)

    def get_stats(self) -> dict:
        """返回故障注入统计

        返回:
            {
                "total_calls":     int,   总调用次数
                "faults_injected": int,   注入故障次数
                "by_type":         dict,  各故障类型注入次数
                "fault_rate":      float, 当前故障率
            }
        """
        return {
            "total_calls": self._stats["total_calls"],
            "faults_injected": self._stats["faults_injected"],
            "by_type": dict(self._stats["by_type"]),
            "fault_rate": self._config.fault_rate,
        }

    def get_fault_log(self) -> list[dict]:
        """返回故障注入历史日志 (按时间顺序)"""
        return list(self._fault_log)

    # ─── 动态调整故障率 ───

    def set_fault_rate(self, rate: float) -> None:
        """动态调整故障率 (运行时切换故障强度)

        参数:
            rate: 新的故障率, 范围 [0, 1]
        """
        if not 0.0 <= rate <= 1.0:
            raise ValueError(f"fault_rate 必须在 [0, 1] 范围内, 当前值: {rate}")
        old = self._config.fault_rate
        self._config.fault_rate = rate
        logger.info(f"FaultInject.set_fault_rate {old:.2f} -> {rate:.2f}")

    def reset_stats(self) -> None:
        """重置统计 (不影响 fault_rate / seed / fault_types)"""
        self._stats = {
            "total_calls": 0,
            "faults_injected": 0,
            "by_type": {t: 0 for t in VALID_FAULT_TYPES},
        }
        self._fault_log.clear()

    def reseed(self, seed: int | None) -> None:
        """重置随机种子 (用于重新生成故障序列)

        注意: 重置后调用序列会从头开始, 但已记录的统计不重置.
        如需完整复现, 请同时调用 reset_stats().
        """
        self._config.seed = seed
        self._rng = random.Random(seed)
