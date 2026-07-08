"""Secrets Broker — LLM 零接触凭证代理

安全模型
--------
LLM / 工具永远不直接读取原始 API Key，而是通过 Broker 获取一个
*临时凭证* (TemporaryCredential)，包含临时 access_token（非原始 key）、
过期时间和作用域。Broker 内部维护「临时 token → 原始 key」的映射，
并负责轮换 (rotate) 与撤销 (revoke)。

- 临时凭证有 TTL（默认 5 分钟），过期自动失效。
- rotate 后该凭证所有已签发 token 立即作废。
- revoke 可提前撤销单个临时凭证。
- 每次 get / rotate / revoke 都写入审计日志（who / what / when）。
- 凭证来源：复用 security/credential_vault.py（机器绑定的加解密）。
"""
from __future__ import annotations

import secrets
import time
import hmac
from dataclasses import dataclass
from typing import Callable

from loguru import logger

from security import credential_vault


# ── 数据结构 ──────────────────────────────────────────────────
@dataclass
class TemporaryCredential:
    """临时凭证 — 交付给调用方的「短期通行证」，不含原始 API Key"""

    access_token: str   # 临时 token（随机生成，非原始 key）
    expires_at: float    # Unix 时间戳，过期时间
    scope: str           # 作用域（如操作名）

    def __repr__(self) -> str:  # 避免日志意外泄漏完整 token
        masked = (self.access_token[:4] + "…") if self.access_token else ""
        return (
            f"TemporaryCredential(token={masked!r}, "
            f"expires_at={self.expires_at}, scope={self.scope!r})"
        )


@dataclass
class AuditEntry:
    """审计日志条目：who(谁) / what(做了什么) / when(何时) / name(目标凭证)"""

    who: str
    what: str            # get / rotate / revoke
    name: str             # 目标凭证名
    when: float           # 时间戳
    detail: str = ""      # 附加信息


# ── SecretsBroker ─────────────────────────────────────────────
class SecretsBroker:
    """凭证代理 — LLM 通过它获取临时凭证，永不接触原始 Key。

    内部状态：
        _source : {凭证名: 加密/明文值}          来自 credential_vault
        _creds  : {凭证名: {raw_key, use_count, current_token, key_version}}
        _tokens : {临时token: {name, expires_at, scope}}
        _revoked: 已撤销的 token 集合
        _audit  : 审计日志列表
    """

    def __init__(
        self,
        credentials: dict[str, str] | None = None,
        ttl_seconds: float = 300.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # 凭证来源：name → 已加密(enc:v1:)或明文值（decrypt 会原样返回明文）
        self._source: dict[str, str] = dict(credentials or {})
        self._ttl_seconds: float = ttl_seconds
        # 允许注入时钟，便于测试 TTL 过期而无需真实 sleep
        self._clock: Callable[[], float] = clock or time.time

        self._creds: dict[str, dict] = {}
        self._tokens: dict[str, dict] = {}
        self._revoked: set[str] = set()
        self._audit: list[AuditEntry] = []

    # ── 内部工具 ──
    def _ensure_entry(self, name: str) -> dict:
        """惰性解密并缓存某凭证的原始 key（仅 Broker 内部持有）"""
        entry = self._creds.get(name)
        if entry is None:
            try:
                raw = credential_vault.decrypt(self._source[name])
            except credential_vault.DecryptionError as e:
                logger.error(f"secrets_broker.decrypt_failed: {name} ({e})")
                raise RuntimeError(f"凭证 {name} 解密失败：{e}") from e
            entry = {
                "raw_key": raw,
                "use_count": 0,
                "current_token": None,
                "key_version": 0,
            }
            self._creds[name] = entry
        return entry

    def _audit_log(self, who: str, what: str, name: str, detail: str = "") -> None:
        self._audit.append(AuditEntry(who=who, what=what, name=name,
                                      when=self._clock(), detail=detail))

    # ── 公共 API ──
    def get_credential(
        self,
        name: str,
        scope: str = "default",
        caller: str = "system",
    ) -> TemporaryCredential:
        """获取临时凭证 — 返回临时 token，绝不返回原始 API Key"""
        if name not in self._source:
            raise PermissionError(f"凭证 '{name}' 未注册")

        entry = self._ensure_entry(name)

        # 生成随机临时 token（不基于原始 key 派生，避免泄漏）
        token = secrets.token_urlsafe(32)
        expires_at = self._clock() + self._ttl_seconds

        self._tokens[token] = {"name": name, "expires_at": expires_at, "scope": scope}
        entry["current_token"] = token
        entry["use_count"] += 1

        self._audit_log(caller, "get", name, f"scope={scope}")
        logger.debug(f"SecretsBroker.get: name={name} scope={scope} caller={caller}")
        return TemporaryCredential(access_token=token, expires_at=expires_at, scope=scope)

    def is_valid(self, credential: TemporaryCredential) -> bool:
        """校验临时凭证是否仍然有效（已签发、未撤销、未过期）"""
        token = credential.access_token
        info = self._tokens.get(token)
        if info is None:
            return False
        if token in self._revoked:
            return False
        return not self._clock() >= info["expires_at"]

    def revoke(self, credential: TemporaryCredential, caller: str = "system") -> bool:
        """提前撤销某个临时凭证"""
        token = credential.access_token
        info = self._tokens.pop(token, None)
        if info is None:
            self._audit_log(caller, "revoke", "", "token not found")
            return False
        self._revoked.add(token)
        name = info["name"]
        if hmac.compare_digest(self._creds.get(name, {}).get("current_token", ""), token):
            self._creds[name]["current_token"] = None
        self._audit_log(caller, "revoke", name)
        logger.debug(f"SecretsBroker.revoke: name={name} caller={caller}")
        return True

    def rotate(self, name: str, caller: str = "system") -> bool:
        """轮换凭证 — 生成新 key 版本，旧 token 全部作废

        说明：实际外部 API Key 的更换由运维/凭证源完成；Broker 此处
        立即作废该凭证所有已签发的活跃 token，并提升 key_version，
        后续 get_credential 将签发全新 token。
        """
        if name not in self._source:
            raise PermissionError(f"凭证 '{name}' 未注册")

        invalidated = 0
        for token in [t for t, info in self._tokens.items() if info["name"] == name]:
            self._tokens.pop(token)
            self._revoked.add(token)
            invalidated += 1

        entry = self._creds.get(name)
        if entry is not None:
            entry["current_token"] = None
            entry["key_version"] += 1  # 生成新 key 版本，旧的作废

        self._audit_log(caller, "rotate", name, f"invalidated={invalidated}")
        logger.debug(
            f"SecretsBroker.rotate: name={name} invalidated={invalidated} caller={caller}"
        )
        return True

    def list_active(self) -> list[str]:
        """列出活跃凭证名（有有效 token 的），不返回任何凭证值"""
        now = self._clock()
        names: set[str] = set()
        for token, info in self._tokens.items():
            if token in self._revoked:
                continue
            if now < info["expires_at"]:
                names.add(info["name"])
        return sorted(names)

    def list_available(self) -> list[str]:
        """列出所有已注册的凭证名（不返回值）— 供工具枚举可用凭证"""
        return sorted(self._source.keys())

    def get_audit_log(self) -> list[AuditEntry]:
        """返回审计日志副本"""
        return list(self._audit)
