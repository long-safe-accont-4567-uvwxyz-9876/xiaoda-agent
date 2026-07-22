"""
多凭证管理池 - 支持凭证轮换和状态机
借鉴 Hermes Agent 的凭证池机制，替代 ModelRouter 中简单的重试/降级逻辑
"""

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from enum import Enum

from loguru import logger

from .error_classifier import ClassifiedError, FailoverReason


def _mask_api_key(key: str) -> str:
    """将 api_key 转为 8 字符 sha256 前缀，避免日志泄漏真实 key 片段。

    同一 key 哈希稳定，不同 key 哈希不同，无法逆推原始 key。
    """
    if not key:
        return "***"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


class CredentialState(Enum):
    """凭证状态机枚举。"""

    OK = "ok"
    EXHAUSTED = "exhausted"    # 限速耗尽，冷却后可恢复
    DEAD = "dead"              # 永久失败（如 token_revoked）


@dataclass
class Credential:
    api_key: str
    provider: str              # "mimo" | "agnes" | ...
    base_url: str = ""
    state: CredentialState = CredentialState.OK
    last_error: str = ""
    exhausted_at: float = 0.0
    cooldown_until: float = 0.0  # 冷却结束的绝对时间戳
    use_count: int = 0
    error_count: int = 0
    last_used_at: float = 0.0
    _use_seq: int = 0          # 单调递增序列号，用于精确标识最近使用（避免 time.time 并发精度不足）


EXHAUSTED_COOLDOWN = 60.0  # exhausted 凭证冷却期 60 秒
AUTH_ERROR_DEAD_THRESHOLD = 3  # 连续 AUTH_ERROR 达到此数才标记 DEAD


class CredentialPool:
    """多凭证管理池，支持凭证轮换和状态机"""

    def __init__(self) -> None:
        # {provider: [Credential, ...]}
        self._pool: dict[str, list[Credential]] = {}
        # {provider: int} 当前轮换索引
        self._cursor: dict[str, int] = {}
        # 统一使用 threading.Lock 保护并发访问 (修复: 原asyncio.Lock与threading.Lock不同步)
        # threading.Lock 同时提供线程安全和协程安全, 且操作耗时极短不会阻塞事件循环
        self._sync_lock = threading.Lock()
        # 单调递增序列号，用于精确标识最近使用的凭证（避免 time.time() 并发精度不足）
        self._use_seq: int = 0
        self._load_from_env()

    def add_credential(self, cred: Credential) -> None:
        """添加凭证到池中"""
        with self._sync_lock:
            provider = cred.provider
            if provider not in self._pool:
                self._pool[provider] = []
                self._cursor[provider] = 0
            self._pool[provider].append(cred)
            logger.info("credential_pool.added",
                        provider=provider,
                        key_hash=_mask_api_key(cred.api_key),
                        total=len(self._pool[provider]))

    async def get_credential(self, provider: str) -> Credential | None:
        """获取当前可用凭证（轮换逻辑：优先 ok 状态，跳过 exhausted 和 dead）"""
        with self._sync_lock:
            self._recover_exhausted(provider)

            creds = self._pool.get(provider)
            if not creds:
                logger.warning("credential_pool.no_credentials", provider=provider)
                return None

            n = len(creds)
            start = self._cursor.get(provider, 0)

            for i in range(n):
                idx = (start + i) % n
                cred = creds[idx]
                if cred.state == CredentialState.OK:
                    # 推进游标到下一个位置，下次调用时优先使用不同凭证
                    self._cursor[provider] = (idx + 1) % n
                    cred.use_count += 1
                    cred.last_used_at = time.time()
                    self._use_seq += 1
                    cred._use_seq = self._use_seq
                    return cred

            # 所有凭证都不可用，尝试找冷却中的 exhausted 凭证
            for i in range(n):
                idx = (start + i) % n
                cred = creds[idx]
                if cred.state == CredentialState.EXHAUSTED:
                    elapsed = time.time() - cred.exhausted_at
                    remaining = EXHAUSTED_COOLDOWN - elapsed
                    logger.warning("credential_pool.all_exhausted_using_cooling",
                                   provider=provider,
                                   key_hash=_mask_api_key(cred.api_key),
                                   remaining=f"{remaining:.0f}s")
                    self._cursor[provider] = (idx + 1) % n
                    cred.use_count += 1
                    cred.last_used_at = time.time()
                    self._use_seq += 1
                    cred._use_seq = self._use_seq
                    return cred

            logger.error("credential_pool.all_dead", provider=provider)
            return None

    async def report_error(self, provider: str, error: ClassifiedError) -> None:
        """报告错误，更新凭证状态"""
        with self._sync_lock:
            creds = self._pool.get(provider, [])
            if not creds:
                return

            # 找到最近使用的凭证（use_count 最大的 ok/exhausted 凭证）
            target = self._find_active_credential(provider)
            if target is None:
                return

            target.error_count += 1
            target.last_error = error.message[:200]

            # 状态转换
            if error.reason == FailoverReason.AUTH_ERROR:
                # 认证错误：不再一次就 DEAD，先 EXHAUSTED 给恢复机会
                # 连续 AUTH_ERROR 达到阈值才标记 DEAD
                if target.error_count >= AUTH_ERROR_DEAD_THRESHOLD:
                    target.state = CredentialState.DEAD
                    logger.error("credential_pool.credential_dead",
                                 provider=provider,
                                 key_hash=_mask_api_key(target.api_key),
                                 reason=error.reason.value,
                                 consecutive_auth_errors=target.error_count)
                else:
                    # 首次/二次 AUTH_ERROR → EXHAUSTED，冷却后可重试
                    target.state = CredentialState.EXHAUSTED
                    target.exhausted_at = time.time()
                    target.cooldown_until = time.time() + EXHAUSTED_COOLDOWN
                    logger.warning("credential_pool.credential_auth_exhausted",
                                   provider=provider,
                                   key_hash=_mask_api_key(target.api_key),
                                   error_count=target.error_count,
                                   threshold=AUTH_ERROR_DEAD_THRESHOLD)
            elif error.reason == FailoverReason.RATE_LIMIT:
                # ok -> exhausted: 限速错误
                target.state = CredentialState.EXHAUSTED
                target.exhausted_at = time.time()
                # 使用 API 返回的实际退避时间，取较大值
                backoff = max(
                    EXHAUSTED_COOLDOWN,
                    error.backoff_seconds,
                ) if error.backoff_seconds > 0 else EXHAUSTED_COOLDOWN
                # 记录冷却结束的绝对时间戳
                target.cooldown_until = time.time() + backoff
                logger.warning("credential_pool.credential_exhausted",
                               provider=provider,
                               key_hash=_mask_api_key(target.api_key),
                               cooldown=f"{backoff:.0f}s")
            else:
                logger.debug("credential_pool.error_no_state_change",
                             provider=provider,
                             reason=error.reason.value)

    async def report_success(self, provider: str) -> None:
        """报告成功"""
        with self._sync_lock:
            _creds = self._pool.get(provider, [])
            # 找到最近使用的凭证，确认其状态为 ok
            target = self._find_active_credential(provider)
            if target and target.state == CredentialState.EXHAUSTED:
                target.state = CredentialState.OK
                target.exhausted_at = 0.0
                target.cooldown_until = 0.0
                target.error_count = 0  # 成功后重置错误计数
                logger.info("credential_pool.recovered_on_success",
                            provider=provider,
                            key_hash=_mask_api_key(target.api_key))
            elif target and target.state == CredentialState.OK:
                target.error_count = 0  # 成功后重置错误计数，避免非连续 AUTH_ERROR 累积

    def _recover_exhausted(self, provider: str) -> None:
        """检查并恢复冷却期结束的 exhausted 凭证"""
        creds = self._pool.get(provider, [])
        now = time.time()
        for cred in creds:
            if cred.state == CredentialState.EXHAUSTED:
                # 使用 cooldown_until 绝对时间戳判断，而非相对计算
                if cred.cooldown_until > 0 and now >= cred.cooldown_until:
                    cred.state = CredentialState.OK
                    cred.exhausted_at = 0.0
                    cred.cooldown_until = 0.0
                    logger.info("credential_pool.exhausted_recovered",
                                provider=provider,
                                key_hash=_mask_api_key(cred.api_key))
                elif cred.cooldown_until <= 0:
                    # 兼容旧数据：没有 cooldown_until 时用默认冷却期
                    if now - cred.exhausted_at >= EXHAUSTED_COOLDOWN:
                        cred.state = CredentialState.OK
                        cred.exhausted_at = 0.0
                        logger.info("credential_pool.exhausted_recovered",
                                    provider=provider,
                                    key_hash=_mask_api_key(cred.api_key))

    def _find_active_credential(self, provider: str) -> Credential | None:
        """找到最近使用的活跃凭证（ok 或 exhausted 状态）

        使用单调递增序列号 _use_seq 而非 last_used_at 时间戳，
        避免并发下 time.time() 精度不足导致误标记其他凭证。
        """
        creds = self._pool.get(provider, [])
        if not creds:
            return None

        # 优先找 _use_seq 最大的非 dead 凭证（序列号保证单调递增，无并发精度问题）
        active = [c for c in creds if c.state != CredentialState.DEAD]
        if not active:
            return None
        return max(active, key=lambda c: c._use_seq)

    def reset_provider(self, provider: str) -> None:
        """重置指定 provider 的所有凭证状态为 OK（用于 Setup 保存新 Key 后恢复）。"""
        creds = self._pool.get(provider, [])
        for cred in creds:
            if cred.state == CredentialState.DEAD:
                cred.state = CredentialState.OK
                cred.error_count = 0
                cred.last_error = ""
                logger.info("credential_pool.dead_reset",
                            provider=provider,
                            key_hash=_mask_api_key(cred.api_key))

    def replace_provider(self, provider: str, new_credential: "Credential") -> None:
        """替换指定 provider 的所有凭证为单个新凭证（Setup 保存新 Key 后调用）。

        旧的 DEAD 凭证即使重置状态也仍是错误的 Key，必须替换才能生效。
        """
        old_count = len(self._pool.get(provider, []))
        self._pool[provider] = [new_credential]
        logger.info("credential_pool.provider_replaced",
                    provider=provider,
                    old_count=old_count,
                    key_hash=_mask_api_key(new_credential.api_key))

    def get_stats(self) -> dict:
        """获取凭证池状态统计"""
        stats = {}
        for provider, creds in self._pool.items():
            ok_count = sum(1 for c in creds if c.state == CredentialState.OK)
            exhausted_count = sum(1 for c in creds if c.state == CredentialState.EXHAUSTED)
            dead_count = sum(1 for c in creds if c.state == CredentialState.DEAD)
            total_uses = sum(c.use_count for c in creds)
            total_errors = sum(c.error_count for c in creds)
            stats[provider] = {
                "total": len(creds),
                "ok": ok_count,
                "exhausted": exhausted_count,
                "dead": dead_count,
                "total_uses": total_uses,
                "total_errors": total_errors,
            }
        return stats

    def _load_from_env(self) -> None:
        """从环境变量自动加载凭证"""
        # 加载 MiMo API Key
        mimo_key = os.getenv("MIMO_API_KEY", "")
        if mimo_key:
            mimo_url = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
            self.add_credential(Credential(
                api_key=mimo_key,
                provider="mimo",
                base_url=mimo_url,
            ))

        # 加载额外的 MiMo Key（MIMO_API_KEY_2, MIMO_API_KEY_3 等）
        for i in range(2, 10):
            extra_key = os.getenv(f"MIMO_API_KEY_{i}", "")
            if extra_key:
                self.add_credential(Credential(
                    api_key=extra_key,
                    provider="mimo",
                    base_url=os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1"),
                ))

        # 加载 Agnes API Key
        agnes_key = os.getenv("AGNES_API_KEY", "")
        if agnes_key:
            agnes_url = os.getenv("AGNES_BASE_URL", "")
            self.add_credential(Credential(
                api_key=agnes_key,
                provider="agnes",
                base_url=agnes_url,
            ))

        # 加载免费模型平台凭证
        _KNOWN_PROVIDERS = {
            "SILICONFLOW_API_KEY": ("siliconflow", "https://api.siliconflow.cn/v1"),
            "OPENROUTER_API_KEY": ("openrouter", "https://openrouter.ai/api/v1"),
            "MODELSCOPE_ACCESS_TOKEN": ("modelscope", "https://api-inference.modelscope.cn/v1"),
        }
        for env_key, (provider, base_url) in _KNOWN_PROVIDERS.items():
            key = os.getenv(env_key, "")
            if key:
                self.add_credential(Credential(
                    api_key=key,
                    provider=provider,
                    base_url=base_url,
                ))

        # Ollama 本地部署，无需真实 API Key，使用占位 Key
        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        if ollama_url:
            self.add_credential(Credential(
                api_key="ollama",  # 占位 Key
                provider="ollama",
                base_url=ollama_url,
            ))

        # 统计
        total = sum(len(creds) for creds in self._pool.values())
        providers = list(self._pool.keys())
        if total > 0:
            logger.info("credential_pool.loaded_from_env",
                        total=total,
                        providers=providers)
        else:
            logger.warning("credential_pool.no_credentials_in_env")


# 全局单例
_pool_instance: CredentialPool | None = None
_pool_lock = threading.Lock()


def get_credential_pool() -> CredentialPool:
    """获取全局凭证池实例"""
    global _pool_instance
    if _pool_instance is None:
        with _pool_lock:
            if _pool_instance is None:
                _pool_instance = CredentialPool()
    return _pool_instance
