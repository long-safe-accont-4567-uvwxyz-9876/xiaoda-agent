"""Secrets Broker 安全工具 — 暴露给 LLM 的凭证代理工具

LLM 通过这两个工具间接使用凭证，永远不接触原始 API Key：
- list_secrets(): 列出可用凭证名（仅名称，不返回任何凭证值）
- use_secret(name, action): 使用凭证执行操作（由 Broker 代理，调用方不接触原始密钥）
"""
from __future__ import annotations

from security.secrets_broker import SecretsBroker
from tool_engine.tool_registry import ToolPermission, register_tool

# 模块级 Broker 单例（默认空凭证源；通过 init_secrets_tool 注入真实凭证）
_broker = SecretsBroker()


def get_broker() -> SecretsBroker:
    """获取当前 Secrets 工具使用的 Broker"""
    return _broker


def init_secrets_tool(credentials: dict[str, str], ttl_seconds: float = 300.0) -> SecretsBroker:
    """初始化 Secrets 工具的 Broker（注入凭证源）

    Args:
        credentials: 凭证名 → 加密(enc:v1:)或明文值 的映射
        ttl_seconds: 临时凭证有效期，默认 5 分钟
    """
    global _broker
    _broker = SecretsBroker(credentials=credentials, ttl_seconds=ttl_seconds)
    return _broker


@register_tool(
    name="list_secrets",
    description="列出当前可用的凭证名（仅返回名称列表，不返回任何凭证值）",
    schema={
        "type": "object",
        "properties": {},
    },
    permission=ToolPermission.READ_ONLY,
    category="security",
    max_frequency=10,
)
def list_secrets() -> list[str]:
    """列出可用凭证名 — 不返回凭证值"""
    return get_broker().list_available()


@register_tool(
    name="use_secret",
    description="使用指定凭证执行操作（由 Secrets Broker 代理，调用方不接触原始 API Key）",
    schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "凭证名，例如 OPENAI_API_KEY"},
            "action": {"type": "string", "description": "要执行的操作描述"},
        },
        "required": ["name", "action"],
    },
    permission=ToolPermission.EXECUTE,
    category="security",
    max_frequency=10,
)
def use_secret(name: str, action: str) -> str:
    """使用凭证执行操作 — Broker 代理签发临时凭证，LLM 不接触原始 key"""
    broker = get_broker()
    try:
        cred = broker.get_credential(name, scope=action, caller="llm")
    except PermissionError as e:
        return f"[secrets] 凭证不可用: {e}"

    # Broker 已签发临时凭证；此处不发起真实网络调用，
    # 仅返回脱敏确认信息（LLM 看不到原始 key，也看不到完整临时 token）
    return (
        f"[secrets] 已为操作 '{action}' 签发临时凭证 "
        f"(name={name}, scope={cred.scope}, "
        f"expires_at={cred.expires_at:.0f}, token=<redacted>)"
    )
