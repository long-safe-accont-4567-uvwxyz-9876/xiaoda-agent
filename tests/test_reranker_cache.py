"""G14: Reranker LRU 缓存测试.

风格参考 tests/test_memory_distiller.py：unittest + asyncio.run + AsyncMock。
验证 memory/reranker.py 的 (query_hash, doc_hash) → score LRU 缓存行为。
"""
import sys
import asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
from unittest.mock import AsyncMock, patch

from memory.reranker import Reranker


class TestRerankerCache(unittest.TestCase):
    """G14: 验证 Reranker LRU 缓存行为"""

    def setUp(self):
        self.reranker = Reranker(api_key="test-key")
        # mock _call_rerank_api，避免真实 HTTP 调用
        self.api_mock = AsyncMock()
        self._patcher = patch.object(
            self.reranker, "_call_rerank_api", self.api_mock
        )
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _api_response(self, scores):
        """构造假 API 响应：scores 是 [(index_in_batch, score), ...]"""
        return [
            {"index": idx, "relevance_score": score}
            for idx, score in scores
        ]

    def test_rerank_cache_hit(self):
        """相同 (query, documents) 第二次不调 API."""
        self.api_mock.return_value = self._api_response([(0, 0.9), (1, 0.5)])

        docs = ["doc1", "doc2"]
        r1 = asyncio.run(self.reranker.rerank("query1", docs, top_n=5))
        r2 = asyncio.run(self.reranker.rerank("query1", docs, top_n=5))

        # API 只应被调用一次
        self.assertEqual(self.api_mock.await_count, 1)
        # 两次返回的分数一致
        self.assertEqual(
            r1[0]["relevance_score"], r2[0]["relevance_score"]
        )

    def test_rerank_cache_miss_different_query(self):
        """不同 query 触发 API 调用."""
        self.api_mock.return_value = self._api_response([(0, 0.7)])

        asyncio.run(self.reranker.rerank("query1", ["doc1"]))
        asyncio.run(self.reranker.rerank("query2", ["doc1"]))

        self.assertEqual(self.api_mock.await_count, 2)

    def test_rerank_cache_miss_different_doc(self):
        """不同 doc 触发 API 调用."""
        self.api_mock.return_value = self._api_response([(0, 0.7)])

        asyncio.run(self.reranker.rerank("query1", ["doc1"]))
        asyncio.run(self.reranker.rerank("query1", ["doc2"]))

        self.assertEqual(self.api_mock.await_count, 2)

    def test_rerank_cache_partial_hit(self):
        """部分 doc 命中缓存，只对未命中的调 API."""
        # 第一次：缓存 [doc1, doc2]
        self.api_mock.return_value = self._api_response(
            [(0, 0.9), (1, 0.5)]
        )
        asyncio.run(self.reranker.rerank("query1", ["doc1", "doc2"]))

        # 第二次：[doc1, doc3]，doc1 命中，doc3 未命中
        self.api_mock.return_value = self._api_response([(0, 0.3)])
        asyncio.run(self.reranker.rerank("query1", ["doc1", "doc3"]))

        # 第二次调用应该只传 [doc3] 给 API
        second_call_args = self.api_mock.await_args_list[1]
        # _call_rerank_api(query, documents, top_n, return_documents)
        docs_passed = second_call_args.args[1]
        self.assertEqual(docs_passed, ["doc3"])

    def test_rerank_cache_clear(self):
        """clear_cache 后重新调 API."""
        self.api_mock.return_value = self._api_response([(0, 0.8)])

        asyncio.run(self.reranker.rerank("query1", ["doc1"]))
        self.assertEqual(self.api_mock.await_count, 1)

        self.reranker.clear_cache()

        asyncio.run(self.reranker.rerank("query1", ["doc1"]))
        self.assertEqual(self.api_mock.await_count, 2)

    def test_rerank_cache_maxsize(self):
        """超过 maxsize 时淘汰最旧（LRU 行为）."""
        # 临时把 maxsize 改小
        original_maxsize = self.reranker.RERANK_CACHE_MAXSIZE
        self.reranker.RERANK_CACHE_MAXSIZE = 3
        try:
            self.api_mock.return_value = self._api_response([(0, 0.5)])

            # 填满缓存：cache LRU 顺序 = [q1, q2, q3]
            asyncio.run(self.reranker.rerank("q1", ["d1"]))
            asyncio.run(self.reranker.rerank("q2", ["d2"]))
            asyncio.run(self.reranker.rerank("q3", ["d3"]))
            self.assertEqual(self.api_mock.await_count, 3)

            # 访问 q1（cache hit，move_to_end 让 q1 变 MRU）
            # LRU 顺序变为: [q2, q3, q1]
            asyncio.run(self.reranker.rerank("q1", ["d1"]))
            self.assertEqual(self.api_mock.await_count, 3)

            # 插入 q4 — 应淘汰 LRU 即 q2
            # cache LRU 顺序: [q3, q1, q4]
            asyncio.run(self.reranker.rerank("q4", ["d4"]))
            self.assertEqual(self.api_mock.await_count, 4)

            # q2 应被淘汰（cache miss，重新调 API）
            # 重新插入 q2 后，淘汰 q3（当前 LRU）
            # cache LRU 顺序: [q1, q4, q2]
            asyncio.run(self.reranker.rerank("q2", ["d2"]))
            self.assertEqual(self.api_mock.await_count, 5)

            # q1 仍应命中（曾被访问过，不应被淘汰）
            asyncio.run(self.reranker.rerank("q1", ["d1"]))
            self.assertEqual(self.api_mock.await_count, 5)
        finally:
            self.reranker.RERANK_CACHE_MAXSIZE = original_maxsize

    def test_rerank_cache_preserves_return_documents(self):
        """return_documents=True 时缓存命中也返回 document 字段."""
        self.api_mock.return_value = self._api_response([(0, 0.9)])

        # 第一次：cache miss，API 返回不含 document（return_documents=False 由 rerank 内部传）
        r1 = asyncio.run(
            self.reranker.rerank("query1", ["doc1"], return_documents=True)
        )

        # 第二次：cache hit
        r2 = asyncio.run(
            self.reranker.rerank("query1", ["doc1"], return_documents=True)
        )

        # 两次都应包含 document 字段
        self.assertIn("document", r1[0])
        self.assertIn("document", r2[0])
        # 缓存命中时也应构造 document 字段
        self.assertEqual(r2[0]["document"]["text"], "doc1")

    def test_rerank_cache_preserves_order(self):
        """返回结果按 relevance_score 降序."""
        # API 返回乱序
        self.api_mock.return_value = self._api_response(
            [
                (0, 0.3),  # doc1: 0.3
                (1, 0.9),  # doc2: 0.9
                (2, 0.5),  # doc3: 0.5
            ]
        )

        results = asyncio.run(
            self.reranker.rerank(
                "query1", ["doc1", "doc2", "doc3"], top_n=3
            )
        )

        # 验证降序
        scores = [r["relevance_score"] for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))
        # 最高分是 doc2 (index=1)
        self.assertEqual(results[0]["relevance_score"], 0.9)


if __name__ == "__main__":
    unittest.main()
