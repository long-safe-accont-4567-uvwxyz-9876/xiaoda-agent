"""A4 修复：测试 query_transform 错误日志改进和超时优化"""
import sys
import asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from memory.query_transform import QueryTransformer


class TestQueryTransformErrorLogging(unittest.TestCase):
    """A4 修复：验证错误日志包含异常类型信息"""

    def setUp(self):
        """创建一个可用的 QueryTransformer 实例（mock API key）"""
        self.transformer = QueryTransformer(api_key="test-key")

    def test_classify_intent_timeout_error_logged_with_type(self):
        """A4 修复：asyncio.TimeoutError 的日志应包含异常类型名（而非空字符串）

        场景：SiliconFlow 免费模型响应超过 2s 超时
        根因：asyncio.TimeoutError 的 str() 返回空字符串 ''
        修复：日志中添加 type(e).__name__ 作为 error_type
        """
        with patch.object(self.transformer, '_call_free_model',
                          side_effect=asyncio.TimeoutError()):
            with patch('memory.query_transform.logger') as mock_logger:
                # 需要设置 INTENT_LLM_CLASSIFY=true
                with patch('config.INTENT_LLM_CLASSIFY', True):
                    result = asyncio.run(self.transformer.classify_intent("量子计算原理"))

                    # 应降级返回 factual
                    self.assertEqual(result, "factual")

                    # 验证日志被调用
                    warning_calls = [c for c in mock_logger.warning.call_args_list
                                     if 'classify_intent_failed' in str(c)]
                    self.assertTrue(len(warning_calls) > 0,
                                    "classify_intent_failed 应被记录")

                    # 验证日志包含异常类型信息
                    logged_kwargs = warning_calls[0].kwargs
                    error_str = logged_kwargs.get('error', '')
                    error_type = logged_kwargs.get('error_type', '')
                    # 至少有一个非空（error 或 error_type）
                    self.assertTrue(
                        error_str or error_type,
                        f"日志应包含 error 或 error_type，实际 error='{error_str}' error_type='{error_type}'"
                    )
                    self.assertEqual(error_type, 'TimeoutError',
                                     f"error_type 应为 'TimeoutError'，实际 '{error_type}'")

    def test_free_model_failed_error_logged_with_type(self):
        """A4 修复：_call_free_model 的错误日志应包含异常类型名
        修复 P2 Bug 8: 日志级别从 warning 降为 debug（已有降级兜底）
        """
        import httpx

        # 模拟 httpx 请求抛出 ConnectError
        with patch('httpx.AsyncClient.post',
                   side_effect=httpx.ConnectError("Connection refused")):
            with patch('memory.query_transform.logger') as mock_logger:
                result = asyncio.run(self.transformer._call_free_model("test prompt"))

                # 应返回 None
                self.assertIsNone(result)

                # 验证日志（P2 Bug 8 修复后使用 debug 级别）
                debug_calls = [c for c in mock_logger.debug.call_args_list
                               if 'free_model_failed' in str(c)]
                self.assertTrue(len(debug_calls) > 0,
                                "应有 debug 级别的 free_model_failed 日志")

                logged_kwargs = debug_calls[0].kwargs
                error_type = logged_kwargs.get('error_type', '')
                self.assertTrue(error_type, "error_type 不应为空")
                self.assertEqual(error_type, 'ConnectError')


class TestQueryTransformTimeoutConfig(unittest.TestCase):
    """A4 修复：验证意图分类超时可配置且默认值合理"""

    def test_intent_classify_timeout_is_configurable(self):
        """A4 修复：意图分类超时应从环境变量读取，默认 5.0s（从 2.0s 提升）"""
        import importlib
        import config

        # 检查配置项存在
        timeout = getattr(config, 'INTENT_CLASSIFY_TIMEOUT', None)
        self.assertIsNotNone(timeout, "config 应定义 INTENT_CLASSIFY_TIMEOUT")
        self.assertGreaterEqual(timeout, 3.0,
                                f"超时至少 3.0s，当前 {timeout}s（原 2.0s 过短导致 29 次误超时）")


class TestCragAssessorChatIntent(unittest.TestCase):
    """A4 修复：CRAG 评估器应对 chat 类查询跳过重试"""

    def test_assessor_returns_no_retry_for_empty_results(self):
        """空结果应触发 fallback 而非 retry"""
        from memory.retrieval_assessor import RetrievalAssessor

        assessor = RetrievalAssessor()
        result = assessor.assess("你好呀", [])
        self.assertTrue(result["should_fallback"], "空结果应触发 fallback")
        self.assertFalse(result["should_retry"], "空结果不应触发 retry")

    def test_assessor_low_confidence_triggers_retry(self):
        """低置信度结果应触发 retry"""
        from memory.retrieval_assessor import RetrievalAssessor

        assessor = RetrievalAssessor()
        # 模拟低分结果
        results = [{"final_score": 0.1}, {"final_score": 0.05}, {"final_score": 0.02}]
        result = assessor.assess("量子计算原理", results)
        self.assertTrue(result["should_retry"], "低分结果应触发 retry")
        self.assertEqual(result["level"], "low")


if __name__ == '__main__':
    unittest.main()
