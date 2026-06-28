"""FaultInjectingLLMClient — 故障注入测试

在测试环境中注入 LLM 故障 (超时/限流/内容过滤/空响应),
验证降级策略和错误恢复是否正确触发。
"""
import random
from loguru import logger
from dataclasses import dataclass
from enum import Enum


class FaultType(Enum):
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
    probability: float = 0.3  # 30% 的请求注入故障
    delay_ms: int = 0         # 故障前延迟


class FaultInjectingLLMClient:
    """故障注入 LLM 客户端 — 包装真实 LLM 客户端"""

    def __init__(self, real_client, faults: list[FaultConfig] | None = None):
        self._real = real_client
        self._faults = faults or []
        self._injection_count = 0

    def add_fault(self, config: FaultConfig):
        self._faults.append(config)

    async def complete(self, messages: list[dict], **kwargs) -> dict:
        """模拟 LLM 调用, 可能注入故障"""
        for fault in self._faults:
            if random.random() < fault.probability:
                self._injection_count += 1
                logger.warning(f"故障注入: {fault.fault_type.value} (#{self._injection_count})")
                return self._generate_fault_response(fault)

        return await self._real.complete(messages, **kwargs)

    def _generate_fault_response(self, config: FaultConfig) -> dict:
        """生成故障响应"""
        ft = config.fault_type
        if ft == FaultType.TIMEOUT:
            raise TimeoutError("Simulated LLM timeout")
        elif ft == FaultType.RATE_LIMIT:
            return {"error": "rate_limit", "message": "Too many requests", "code": 429}
        elif ft == FaultType.CONTENT_FILTER:
            return {"error": "content_filter", "message": "Content filtered", "code": 451}
        elif ft == FaultType.EMPTY_RESPONSE:
            return {"choices": [{"message": {"content": ""}}]}
        elif ft == FaultType.INVALID_JSON:
            return {"choices": [{"message": {"content": "{invalid json}"}}]}
        elif ft == FaultType.PARTIAL_RESPONSE:
            return {"choices": [{"message": {"content": "这是一个不完整的"}}]}
        return {}

    def get_stats(self) -> dict:
        return {
            "total_injections": self._injection_count,
            "configured_faults": len(self._faults),
        }
