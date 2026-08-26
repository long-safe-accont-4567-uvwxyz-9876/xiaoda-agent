"""测试 credential_pool.py 的 CredentialPool"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import time
import unittest
from unittest.mock import patch

from utils.credential_pool import EXHAUSTED_COOLDOWN, Credential, CredentialPool, CredentialState
from utils.error_classifier import ClassifiedError, FailoverReason, RecoveryAction


def _run_async(coro):
    """Helper: run async coroutine in a new event loop"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestCredentialPool(unittest.TestCase):
    """测试 CredentialPool 凭证管理"""

    def setUp(self):
        """每个测试前创建空的凭证池（跳过环境变量加载）"""
        with patch.object(CredentialPool, '_load_from_env', lambda self: None):
            self.pool = CredentialPool()

    def test_add_and_get_credential(self):
        """添加凭证后能获取"""
        cred = Credential(api_key="sk-test123456", provider="mimo")
        self.pool.add_credential(cred)
        result = _run_async(self.pool.get_credential("mimo"))
        self.assertIsNotNone(result)
        self.assertEqual(result.api_key, "sk-test123456")
        self.assertEqual(result.provider, "mimo")
        self.assertEqual(result.state, CredentialState.OK)

    def test_credential_rotation(self):
        """添加多个凭证，exhausted 后自动轮换"""
        cred1 = Credential(api_key="sk-key1aaaaaa", provider="mimo")
        cred2 = Credential(api_key="sk-key2bbbbbb", provider="mimo")
        self.pool.add_credential(cred1)
        self.pool.add_credential(cred2)

        # 获取第一个凭证
        first = _run_async(self.pool.get_credential("mimo"))
        self.assertIsNotNone(first)

        # 将第一个标记为 exhausted
        first.state = CredentialState.EXHAUSTED
        first.exhausted_at = time.time()

        # 再次获取应该轮换到第二个
        second = _run_async(self.pool.get_credential("mimo"))
        self.assertIsNotNone(second)
        self.assertEqual(second.api_key, "sk-key2bbbbbb")
        self.assertEqual(second.state, CredentialState.OK)

    def test_exhausted_recovery(self):
        """exhausted 凭证冷却后恢复"""
        cred = Credential(api_key="sk-recovery1234", provider="mimo")
        cred.state = CredentialState.EXHAUSTED
        # 设置 exhausted_at 为很久以前（超过冷却期）
        cred.exhausted_at = time.time() - EXHAUSTED_COOLDOWN - 10
        self.pool.add_credential(cred)

        # 获取凭证时应该自动恢复
        result = _run_async(self.pool.get_credential("mimo"))
        self.assertIsNotNone(result)
        self.assertEqual(result.state, CredentialState.OK)

    def test_dead_credential(self):
        """dead 凭证不可恢复"""
        cred = Credential(api_key="sk-dead1234567", provider="mimo")
        cred.state = CredentialState.DEAD
        self.pool.add_credential(cred)

        # dead 凭证不应被返回
        result = _run_async(self.pool.get_credential("mimo"))
        self.assertIsNone(result)

    def test_report_error_rate_limit(self):
        """限速错误将凭证标记为 exhausted"""
        cred = Credential(api_key="sk-ratelimit12", provider="mimo")
        self.pool.add_credential(cred)
        # 先使用一次以增加 use_count
        _run_async(self.pool.get_credential("mimo"))

        error = ClassifiedError(
            reason=FailoverReason.RATE_LIMIT,
            action=RecoveryAction.BACKOFF_RETRY,
            original_error=Exception("429"),
            message="rate limit exceeded",
            is_retryable=True,
            backoff_seconds=5.0,
        )
        _run_async(self.pool.report_error("mimo", error))

        # 验证凭证状态变为 exhausted
        stats = self.pool.get_stats()
        self.assertEqual(stats["mimo"]["exhausted"], 1)
        self.assertEqual(stats["mimo"]["ok"], 0)

    def test_report_error_auth(self):
        """认证错误将凭证标记为 exhausted（连续 AUTH_ERROR 达阈值才 DEAD）"""
        cred = Credential(api_key="sk-auth1234567", provider="mimo")
        self.pool.add_credential(cred)
        # 先使用一次
        _run_async(self.pool.get_credential("mimo"))

        error = ClassifiedError(
            reason=FailoverReason.AUTH_ERROR,
            action=RecoveryAction.ROTATE_CREDENTIAL,
            original_error=Exception("401"),
            message="invalid api key",
            is_retryable=False,
            backoff_seconds=0.0,
        )
        _run_async(self.pool.report_error("mimo", error))

        # 验证凭证状态变为 exhausted（首次 AUTH_ERROR 不直接 DEAD）
        stats = self.pool.get_stats()
        self.assertEqual(stats["mimo"]["dead"], 0)
        self.assertEqual(stats["mimo"]["exhausted"], 1)
        self.assertEqual(stats["mimo"]["ok"], 0)

    def _make_rate_limit_error(self) -> ClassifiedError:
        return ClassifiedError(
            reason=FailoverReason.RATE_LIMIT,
            action=RecoveryAction.ROTATE_CREDENTIAL,
            original_error=Exception("429"),
            message="rate limited",
            is_retryable=True,
            backoff_seconds=30.0,
        )

    def test_report_error_attributes_to_exact_key(self):
        """多凭证并发在途：错误按传入 api_key 精确归因，不误伤最近使用的凭证"""
        cred_a = Credential(api_key="sk-aaaaaaaaaa", provider="mimo")
        cred_b = Credential(api_key="sk-bbbbbbbbbb", provider="mimo")
        self.pool.add_credential(cred_a)
        self.pool.add_credential(cred_b)

        # 构造"B 是最近使用"的现场：A 先用、B 后用（_use_seq 更大）
        _run_async(self.pool.get_credential("mimo"))   # A, seq=1
        first_state = cred_a.state
        cred_a.state = CredentialState.EXHAUSTED       # 强制轮换到 B
        cred_a.exhausted_at = time.time()
        _run_async(self.pool.get_credential("mimo"))   # B, seq=2
        cred_a.state = first_state                     # 还原 A 为 ok（模拟 A 的请求仍在途）

        # A 的在途请求失败——旧启发式会记到 B（use_seq 最大），精确归因应记到 A
        _run_async(self.pool.report_error(
            "mimo", self._make_rate_limit_error(), api_key="sk-aaaaaaaaaa"))

        self.assertEqual(cred_a.state, CredentialState.EXHAUSTED)
        self.assertEqual(cred_a.error_count, 1)
        self.assertEqual(cred_b.state, CredentialState.OK, "健康凭证不应被误标")
        self.assertEqual(cred_b.error_count, 0)

    def test_report_success_attributes_to_exact_key(self):
        """成功按传入 api_key 归因：恢复的是真正成功的那把凭证"""
        cred_a = Credential(api_key="sk-cccccccccc", provider="mimo")
        cred_b = Credential(api_key="sk-dddddddddd", provider="mimo")
        self.pool.add_credential(cred_a)
        self.pool.add_credential(cred_b)
        cred_a.state = CredentialState.EXHAUSTED
        cred_a.error_count = 1

        # B 最近被使用，但成功的是 A
        _run_async(self.pool.get_credential("mimo"))
        cred_a.state = CredentialState.EXHAUSTED
        _run_async(self.pool.get_credential("mimo"))

        _run_async(self.pool.report_success("mimo", api_key="sk-cccccccccc"))
        self.assertEqual(cred_a.state, CredentialState.OK, "成功归因应恢复 A")
        self.assertEqual(cred_a.error_count, 0)
        self.assertEqual(cred_b.state, CredentialState.OK)

    def test_report_error_unknown_key_falls_back_to_heuristic(self):
        """传入池中不存在的 key → 退回"最近使用"启发式，行为与旧版一致"""
        cred = Credential(api_key="sk-eeeeeeeeee", provider="mimo")
        self.pool.add_credential(cred)
        _run_async(self.pool.get_credential("mimo"))

        _run_async(self.pool.report_error(
            "mimo", self._make_rate_limit_error(), api_key="sk-not-in-pool"))

        self.assertEqual(cred.state, CredentialState.EXHAUSTED)

    def test_report_error_no_key_keeps_legacy_behavior(self):
        """不传 api_key（缺省）→ 与旧版完全一致的启发式路径"""
        cred = Credential(api_key="sk-ffffffffff", provider="mimo")
        self.pool.add_credential(cred)
        _run_async(self.pool.get_credential("mimo"))

        _run_async(self.pool.report_error("mimo", self._make_rate_limit_error()))

        self.assertEqual(cred.state, CredentialState.EXHAUSTED)


if __name__ == '__main__':
    unittest.main()
