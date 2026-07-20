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

    # SSRF 防护: service/method 由 LLM 控制, 必须白名单校验, 防止拼接 evil.com/v1/x
    # service 走严格白名单 (避免 api.{service}.com 拼接到恶意域名)
    _ALLOWED_SERVICES = frozenset({
        "openai", "anthropic", "deepseek", "siliconflow",
        "mimo", "agnes", "openrouter", "moonshot",
    })
    # method 只允许 [a-zA-Z0-9_/] 字符 (防 path injection, 例如 method="evil.com/v1/x")
    _METHOD_PATTERN = re.compile(r"^[a-zA-Z0-9_/]+$")

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
        # SSRF 防护: 校验白名单, 防止 service/method 拼接出内网或恶意 URL
        if service not in self._ALLOWED_SERVICES:
            raise ValueError(f"unknown service: {service}")
        if not isinstance(method, str) or not self._METHOD_PATTERN.match(method):
            raise ValueError(f"invalid method: {method!r}")
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
        """脱敏: 移除响应中可能包含的凭证信息

        修复: 原代码对 dict 用 ``str(data)`` 得到 Python repr (单引号),
        json.loads 必抛 JSONDecodeError, 永远走 except 分支返回 ``{"_sanitized": ...}``,
        丢失原 dict 结构. 改用 json.dumps 得到合法 JSON 字符串再做正则脱敏,
        或者直接对 dict 做字段级递归脱敏 (更安全, 不依赖正则覆盖面).
        """
        import json

        def _redact_text(text: str) -> str:
            # 移除类 API Key 模式
            text = re.sub(r'(sk-|key-|token-)[a-zA-Z0-9]{20,}', '[REDACTED_KEY]', text)
            # 移除 Bearer token
            text = re.sub(r'Bearer\s+[a-zA-Z0-9._-]{20,}', '[REDACTED_TOKEN]', text)
            return text

        if isinstance(data, dict):
            # 直接对 dict 做字段级递归脱敏, 结构保留
            def _sanitize_obj(obj):
                if isinstance(obj, dict):
                    return {k: _sanitize_obj(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [_sanitize_obj(v) for v in obj]
                if isinstance(obj, str):
                    return _redact_text(obj)
                return obj
            return _sanitize_obj(data)
        if isinstance(data, str):
            return _redact_text(data)
        # 其他类型 (int/None 等): 走字符串脱敏路径
        try:
            text = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(data)
        return _redact_text(text)


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
