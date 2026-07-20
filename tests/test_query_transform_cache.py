"""G15 修复：测试 query_transform LRU + TTL 缓存"""
import sys
import asyncio
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import AsyncMock, patch
from memory.query_transform import QueryTransformer, _CACHE_MISS


class TestQueryTransformCache(unittest.TestCase):
    """G15: 验证 LRU + TTL 缓存避免重复调 LLM"""

    def setUp(self):
        """创建一个可用的 QueryTransformer 实例（mock API key）"""
        self.transformer = QueryTransformer(api_key="test-key")

    def _mock_call_free_model(self, return_value="mocked_rewrite"):
        """返回一个 AsyncMock，记录调用次数并返回指定值

        注意：mock 整个 _call_free_model，不会触发真实代码里的 strip()，
        所以测试数据应使用已 strip 的最终值。
        """
        mock = AsyncMock(return_value=return_value)
        return mock

    # ----- rewrite_query 缓存 -----

    def test_rewrite_cache_hit(self):
        """G15: 相同 (query, context) 第二次不调 LLM"""
        mock_call = self._mock_call_free_model("rewritten_query")
        with patch.object(self.transformer, '_call_free_model', mock_call):
            r1 = asyncio.run(self.transformer.rewrite_query("你好", context="上下文"))
            r2 = asyncio.run(self.transformer.rewrite_query("你好", context="上下文"))

            self.assertEqual(r1, "rewritten_query")
            self.assertEqual(r2, "rewritten_query")
            # 只调一次 LLM
            self.assertEqual(mock_call.await_count, 1,
                             f"缓存命中应只调一次 LLM，实际 {mock_call.await_count}")

    def test_rewrite_cache_miss_different_query(self):
        """G15: 不同 query 应分别调 LLM"""
        mock_call = self._mock_call_free_model("rewritten")
        with patch.object(self.transformer, '_call_free_model', mock_call):
            asyncio.run(self.transformer.rewrite_query("你好"))
            asyncio.run(self.transformer.rewrite_query("再见"))

            self.assertEqual(mock_call.await_count, 2,
                             f"不同 query 应调 2 次 LLM，实际 {mock_call.await_count}")

    def test_rewrite_cache_miss_different_context(self):
        """G15: 同 query 不同 context 应分别调 LLM"""
        mock_call = self._mock_call_free_model("rewritten")
        with patch.object(self.transformer, '_call_free_model', mock_call):
            asyncio.run(self.transformer.rewrite_query("你好", context="上下文A"))
            asyncio.run(self.transformer.rewrite_query("你好", context="上下文B"))

            self.assertEqual(mock_call.await_count, 2,
                             f"不同 context 应调 2 次 LLM，实际 {mock_call.await_count}")

    def test_rewrite_cache_ttl_expiry(self):
        """G15: TTL 过期后应重新调 LLM"""
        mock_call = self._mock_call_free_model("rewritten")
        with patch.object(self.transformer, '_call_free_model', mock_call):
            asyncio.run(self.transformer.rewrite_query("你好", context="上下文"))
            self.assertEqual(mock_call.await_count, 1)

            # 直接操作内部缓存，把 expire_at 设为已过期
            for key in list(self.transformer._rewrite_cache.keys()):
                result, _ = self.transformer._rewrite_cache[key]
                self.transformer._rewrite_cache[key] = (result, time.monotonic() - 1)

            asyncio.run(self.transformer.rewrite_query("你好", context="上下文"))
            self.assertEqual(mock_call.await_count, 2,
                             f"TTL 过期后应重新调 LLM，实际 {mock_call.await_count}")

    def test_rewrite_cache_clear(self):
        """G15: clear_cache 后应重新调 LLM"""
        mock_call = self._mock_call_free_model("rewritten")
        with patch.object(self.transformer, '_call_free_model', mock_call):
            asyncio.run(self.transformer.rewrite_query("你好", context="上下文"))
            self.assertEqual(mock_call.await_count, 1)

            self.transformer.clear_cache()

            asyncio.run(self.transformer.rewrite_query("你好", context="上下文"))
            self.assertEqual(mock_call.await_count, 2,
                             f"clear_cache 后应重新调 LLM，实际 {mock_call.await_count}")

    # ----- expand_query 缓存 -----

    def test_expand_cache_hit(self):
        """G15: 相同 (query, n) 第二次不调 LLM"""
        mock_call = self._mock_call_free_model("exp1\nexp2\nexp3")
        with patch.object(self.transformer, '_call_free_model', mock_call):
            r1 = asyncio.run(self.transformer.expand_query("你好", n=3))
            r2 = asyncio.run(self.transformer.expand_query("你好", n=3))

            self.assertEqual(r1, r2)
            self.assertEqual(mock_call.await_count, 1,
                             f"缓存命中应只调一次 LLM，实际 {mock_call.await_count}")

    def test_expand_cache_miss_different_n(self):
        """G15: 同 query 不同 n 应分别调 LLM"""
        mock_call = self._mock_call_free_model("exp1\nexp2\nexp3")
        with patch.object(self.transformer, '_call_free_model', mock_call):
            asyncio.run(self.transformer.expand_query("你好", n=3))
            asyncio.run(self.transformer.expand_query("你好", n=5))

            self.assertEqual(mock_call.await_count, 2,
                             f"不同 n 应调 2 次 LLM，实际 {mock_call.await_count}")

    # ----- generate_hyde_document 缓存 -----

    def test_hyde_cache_hit(self):
        """G15: 相同 (query, context) HyDE 第二次不调 LLM"""
        mock_call = AsyncMock(return_value="假设文档内容")
        with patch.object(self.transformer, '_call_free_model', mock_call):
            r1 = asyncio.run(self.transformer.generate_hyde_document("你好", context="上下文"))
            r2 = asyncio.run(self.transformer.generate_hyde_document("你好", context="上下文"))

            self.assertEqual(r1, "假设文档内容")
            self.assertEqual(r2, "假设文档内容")
            self.assertEqual(mock_call.await_count, 1,
                             f"缓存命中应只调一次 LLM，实际 {mock_call.await_count}")

    def test_hyde_timeout_not_cached(self):
        """G15: timeout 异常不缓存，下次仍调 LLM"""
        mock_call = AsyncMock(side_effect=asyncio.TimeoutError())
        with patch.object(self.transformer, '_call_free_model', mock_call):
            r1 = asyncio.run(self.transformer.generate_hyde_document("你好"))
            self.assertIsNone(r1)

            # 切换 mock 让第二次返回正常结果
            mock_call.side_effect = None
            mock_call.return_value = "假设文档"
            r2 = asyncio.run(self.transformer.generate_hyde_document("你好"))

            self.assertEqual(r2, "假设文档")
            # 两次都应调 LLM（timeout 未缓存）
            self.assertEqual(mock_call.await_count, 2,
                             f"timeout 不应缓存，应调 2 次 LLM，实际 {mock_call.await_count}")

    # ----- 缓存返回副本 -----

    def test_expand_cache_returns_copy(self):
        """G15: expand_query 返回的 list 不应被外部修改污染缓存"""
        mock_call = AsyncMock(return_value="exp1\nexp2\nexp3")
        with patch.object(self.transformer, '_call_free_model', mock_call):
            r1 = asyncio.run(self.transformer.expand_query("你好", n=3))
            # 外部修改返回值
            r1.append("EXTERNAL_MODIFICATION")

            r2 = asyncio.run(self.transformer.expand_query("你好", n=3))

            # 缓存内的结果不应被污染
            self.assertNotIn("EXTERNAL_MODIFICATION", r2,
                             "返回的 list 应为副本，外部修改不应污染缓存")

    # ----- G15 修复：None 缓存语义 -----

    def test_hyde_cache_stores_none(self):
        """G15 修复: LLM 返回 None 时应缓存 None，第二次不再调 LLM

        场景：LLM 返回空内容（None），_cache_put 应将 None 存入缓存。
        下次调用应命中缓存（_cache_get 返回 None 但表示"命中"），
        不再调 LLM。
        根因：原 _cache_get 在未命中时也返回 None，调用方用
        `if cached is not None` 判断命中，无法区分"未命中"和"命中 None"。
        """
        mock_call = AsyncMock(return_value=None)
        with patch.object(self.transformer, '_call_free_model', mock_call):
            r1 = asyncio.run(self.transformer.generate_hyde_document("你好", context="上下文"))
            r2 = asyncio.run(self.transformer.generate_hyde_document("你好", context="上下文"))

            # 两次都应返回 None（LLM 返回 None）
            self.assertIsNone(r1)
            self.assertIsNone(r2)
            # 缓存命中后不应再调 LLM——只调一次
            self.assertEqual(mock_call.await_count, 1,
                             f"None 应被缓存，第二次应命中不再调 LLM，实际调 {mock_call.await_count} 次")

    def test_rewrite_cache_returns_miss_sentinel_correctly(self):
        """G15 修复: _cache_get 在未命中时应返回 _CACHE_MISS sentinel

        场景：缓存未命中时，调用方需用 `is not _CACHE_MISS` 区分"未命中"
        与"命中 None"。sentinel 必须是模块级 object()，不能是 None。
        """
        # _CACHE_MISS 必须是一个独立的对象，不是 None
        self.assertIsNotNone(_CACHE_MISS,
                             "_CACHE_MISS sentinel 不应是 None")

        # 空缓存时 _cache_get 应返回 _CACHE_MISS
        cache = self.transformer._rewrite_cache.__class__()  # 空 OrderedDict
        result = self.transformer._cache_get(cache, "nonexistent_key")
        self.assertIs(result, _CACHE_MISS,
                      f"未命中应返回 _CACHE_MISS sentinel，实际返回 {result!r}")

        # 写入 None 后再取，应返回 None（而非 _CACHE_MISS）
        self.transformer._cache_put(cache, "key_with_none", None)
        result = self.transformer._cache_get(cache, "key_with_none")
        self.assertIsNone(result,
                          "命中 None 时应返回 None，而非 _CACHE_MISS")
        self.assertIsNot(result, _CACHE_MISS,
                         "命中 None 不应返回 _CACHE_MISS sentinel")


if __name__ == '__main__':
    unittest.main()
