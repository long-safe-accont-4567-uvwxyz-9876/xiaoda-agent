"""测试 memory/memory_distiller.py — 免费模型蒸馏 + router 降级。

风格参考 tests/test_instinct_manager.py：unittest + asyncio.run + mock。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from memory.memory_distiller import MemoryDistiller


class TestMemoryDistiller(unittest.TestCase):
    """MemoryDistiller：免费模型蒸馏 / router 降级 / 空输入"""

    def setUp(self):
        self.mock_router = MagicMock()
        self.distiller = MemoryDistiller(router=self.mock_router)
        # 启用免费模型路径
        self.distiller._free_api_key = "fake-key"

    def _sample_memories(self):
        return [
            {"summary": "用户喜欢用 Python", "timestamp": 1700000000},
            {"summary": "讨论了 AI Agent 架构", "timestamp": 1700100000},
        ]

    def test_distill_success(self):
        """mock 免费模型返回 '摘要文本'，distill() 返回该文本"""
        self.distiller._call_free_model = AsyncMock(return_value="摘要文本")
        result = asyncio.run(self.distiller.distill(self._sample_memories()))
        self.assertEqual(result, "摘要文本")
        self.distiller._call_free_model.assert_awaited_once()

    def test_distill_fallback_to_router(self):
        """免费模型失败（返回 None）时降级到 router.route"""
        self.distiller._call_free_model = AsyncMock(return_value=None)
        self.mock_router.route = AsyncMock(return_value="router摘要")
        result = asyncio.run(self.distiller.distill(self._sample_memories()))
        self.assertEqual(result, "router摘要")
        self.mock_router.route.assert_awaited_once()

    def test_distill_empty_memories(self):
        """空列表返回空字符串"""
        result = asyncio.run(self.distiller.distill([]))
        self.assertEqual(result, "")
        # 也不应调用任何模型
        self.distiller._call_free_model = AsyncMock(return_value="should_not_call")
        result2 = asyncio.run(self.distiller.distill([]))
        self.assertEqual(result2, "")
        self.distiller._call_free_model.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
