"""测试 error_classifier.py 的 ErrorClassifier"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest

from utils.error_classifier import ErrorClassifier, FailoverReason, RecoveryAction


class TestErrorClassifier(unittest.TestCase):
    """测试 ErrorClassifier 分类逻辑"""

    def setUp(self):
        self.classifier = ErrorClassifier()

    def test_classify_auth_error(self):
        """模拟 openai.AuthenticationError，验证分类为 AUTH_ERROR，恢复策略为 ROTATE_CREDENTIAL"""
        # 通过消息匹配路径测试认证错误
        exc = Exception("invalid api key, authentication failed")
        result = self.classifier.classify(exc)
        self.assertEqual(result.reason, FailoverReason.AUTH_ERROR)
        self.assertEqual(result.action, RecoveryAction.ROTATE_CREDENTIAL)

    def test_classify_rate_limit(self):
        """模拟 openai.RateLimitError，验证分类为 RATE_LIMIT，恢复策略为 BACKOFF_RETRY"""
        exc = Exception("rate limit exceeded, 429 too many requests")
        result = self.classifier.classify(exc)
        self.assertEqual(result.reason, FailoverReason.RATE_LIMIT)
        self.assertEqual(result.action, RecoveryAction.BACKOFF_RETRY)

    def test_classify_timeout(self):
        """模拟 openai.APITimeoutError，验证分类为 TIMEOUT"""
        exc = Exception("timeout error")
        result = self.classifier.classify(exc)
        self.assertEqual(result.reason, FailoverReason.TIMEOUT)
        self.assertEqual(result.action, RecoveryAction.RETRY)

    def test_classify_connection_error(self):
        """模拟 openai.APIConnectionError，验证分类为 CONNECTION_ERROR"""
        exc = Exception("connection error: unable to reach server")
        result = self.classifier.classify(exc)
        self.assertEqual(result.reason, FailoverReason.CONNECTION_ERROR)

    def test_classify_generic_exception(self):
        """模拟普通 Exception，验证分类为 UNKNOWN"""
        exc = Exception("something unexpected happened")
        result = self.classifier.classify(exc)
        self.assertEqual(result.reason, FailoverReason.UNKNOWN)
        self.assertEqual(result.action, RecoveryAction.RETRY)
        self.assertTrue(result.is_retryable)

    def test_classify_by_status_code_500(self):
        """用包含状态码 500 的异常测试服务器错误分类"""
        exc = Exception("server returned 500 internal server error")
        result = self.classifier.classify(exc)
        self.assertEqual(result.reason, FailoverReason.SERVER_ERROR)
        self.assertEqual(result.action, RecoveryAction.BACKOFF_RETRY)

    def test_classify_by_status_code_429(self):
        """用包含状态码 429 的异常测试限速分类"""
        exc = Exception("Error 429: Too Many Requests")
        result = self.classifier.classify(exc)
        self.assertEqual(result.reason, FailoverReason.RATE_LIMIT)

    def test_classify_by_status_code_401(self):
        """用包含状态码 401 的异常测试认证错误分类"""
        exc = Exception("HTTP 401 Unauthorized")
        result = self.classifier.classify(exc)
        self.assertEqual(result.reason, FailoverReason.AUTH_ERROR)
        self.assertEqual(result.action, RecoveryAction.ROTATE_CREDENTIAL)


if __name__ == '__main__':
    unittest.main()
