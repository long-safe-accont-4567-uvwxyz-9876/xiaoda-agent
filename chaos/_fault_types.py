"""故障注入类型 — chaos 内部副本，断开对 tests/ 的生产依赖。

原版在 tests/fault_injection.py，本文件仅供 chaos/ 模块在非测试环境中使用。
tests/ 中的代码仍直接引用 tests.fault_injection。
"""
import random
from dataclasses import dataclass
from enum import Enum

from loguru import logger


class FaultType(Enum):
    """故障注入类型枚举。"""

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    CONTENT_FILTER = "content_filter"
    EMPTY_RESPONSE = "empty_response"
    INVALID_JSON = "invalid_json"
    PARTIAL_RESPONSE = "partial_response"


@dataclass
class FaultConfig:
    """故障注入配置"""
    fault_type: FaultType
    probability: float = 0.3
    delay_ms: int = 0


class SimpleFaultInjectingLLMClient:
    """故障注入 LLM 客户端 — 包装真实 LLM 客户端"""

    def __init__(self, real_client, faults: list[FaultConfig] | None = None):
        self._real = real_client
        self._faults = faults or []
        self._injection_count = 0

    def add_fault(self, config: FaultConfig):
        self._faults.append(config)

    async def complete(self, messages: list[dict], **kwargs) -> dict:
        for fault in self._faults:
            if random.random() < fault.probability:
                self._injection_count += 1
                logger.warning(f"故障注入: {fault.fault_type.value} (#{self._injection_count})")
                return self._generate_fault_response(fault)
        return await self._real.complete(messages, **kwargs)

    def _generate_fault_response(self, config: FaultConfig) -> dict:
        ft = config.fault_type
        if ft == FaultType.TIMEOUT:
            raise TimeoutError("Simulated LLM timeout")
        if ft == FaultType.RATE_LIMIT:
            return {"error": "rate_limit", "message": "Too many requests", "code": 429}
        if ft == FaultType.CONTENT_FILTER:
            return {"error": "content_filter", "message": "Content filtered", "code": 451}
        if ft == FaultType.EMPTY_RESPONSE:
            return {"choices": [{"message": {"content": ""}}]}
        if ft == FaultType.INVALID_JSON:
            return {"choices": [{"message": {"content": "{invalid json}"}}]}
        if ft == FaultType.PARTIAL_RESPONSE:
            return {"choices": [{"message": {"content": "这是一个不完整的"}}]}
        return {}

    def get_stats(self) -> dict:
        return {
            "total_injections": self._injection_count,
            "configured_faults": len(self._faults),
        }
