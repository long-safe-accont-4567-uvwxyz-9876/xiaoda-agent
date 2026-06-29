"""Secrets Broker 模式 — Rafter Layer 2

凭证代理: LLM 永不接触原始凭证。
流程: Agent请求操作 → Broker查凭证 → Broker发起请求 → 返回脱敏结果
"""
import re
from loguru import logger
from typing import Any

from utils.encrypted_credential import reveal_credential


class SecretsBroker:
    """凭证代理 — LLM 只发出"我要调用X"的意图, Broker 代查凭证"""

    def __init__(self, credential_store: dict | None = None) -> None:
        # credential_store: key_id → EncryptedCredential 或 str
        self._store: dict = credential_store or {}

    def register(self, key_id: str, credential: Any) -> None:
        """注册凭证"""
        self._store[key_id] = credential

    def get_credential(self, service: str) -> str:
        """获取解密后的凭证 (仅在 Broker 内部使用)"""
        cred_key = f"{service.upper()}_API_KEY"
        if cred_key not in self._store:
            raise PermissionError(f"无{service}凭证,请检查.env配置")
        return reveal_credential(self._store[cred_key])

    async def execute_api_call(self, service: str, method: str, params: dict) -> dict:
        """代理执行 API 调用 — LLM 只传 service+method+params, 不传凭证"""
        api_key = self.get_credential(service)

        import httpx
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"Bearer {api_key}"}
            resp = await client.post(
                f"https://api.{service}.com/v1/{method}",
                json=params, headers=headers, timeout=30
            )
        result = resp.json()
        logger.info(f"SecretsBroker: {service}.{method} 调用成功, 返回{len(str(result))}字符")
        return self._sanitize_response(result)

    @staticmethod
    def _sanitize_response(data: dict | str) -> dict | str:
        """脱敏: 移除响应中可能包含的凭证信息"""
        sanitized = str(data)
        # 移除类 API Key 模式
        sanitized = re.sub(r'(sk-|key-|token-)[a-zA-Z0-9]{20,}', '[REDACTED_KEY]', sanitized)
        # 移除 Bearer token
        sanitized = re.sub(r'Bearer\s+[a-zA-Z0-9._-]{20,}', '[REDACTED_TOKEN]', sanitized)
        if isinstance(data, dict):
            # 尝试安全解析回 dict
            import json
            try:
                return json.loads(sanitized)
            except (json.JSONDecodeError, ValueError):
                return {"_sanitized": sanitized}
        return sanitized


# 全局单例
_broker = SecretsBroker()


def get_secrets_broker() -> SecretsBroker:
    """获取全局 SecretsBroker 单例."""
    return _broker


def init_broker(env_store: dict) -> None:
    """从环境变量初始化 Broker"""
    for key, val in env_store.items():
        if val and key.endswith("_API_KEY"):
            service = key.replace("_API_KEY", "").lower()
            _broker.register(key, val)
            logger.debug(f"SecretsBroker: 已注册 {service} 凭证")
