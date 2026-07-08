"""测试 instinct_manager.py 的 InstinctManager"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from instinct_manager import InstinctManager


class TestInstinctManager(unittest.TestCase):
    """测试 InstinctManager（mock DatabaseManager 和 ModelRouter）"""

    def setUp(self):
        """创建 mock 依赖"""
        self.mock_db = MagicMock()
        self.mock_router = MagicMock()
        self.manager = InstinctManager(db=self.mock_db, router=self.mock_router)
        # 禁用免费模型，强制走 router.route 降级路径
        self.manager._free_api_key = ""

    def test_build_instinct_prompt_empty(self):
        """无 Instinct 时返回空字符串"""
        # mock get_active_instincts 返回空列表
        self.manager.get_active_instincts = AsyncMock(return_value=[])
        result = asyncio.run(
            self.manager.build_instinct_prompt()
        )
        self.assertEqual(result, "")

    def test_build_instinct_prompt_with_data(self):
        """有 Instinct 时返回格式化提示"""
        instincts = [
            {"content": "用户喜欢用中文交流", "confidence": 0.9},
            {"content": "用户是开发者", "confidence": 0.85},
        ]
        self.manager.get_active_instincts = AsyncMock(return_value=instincts)
        result = asyncio.run(
            self.manager.build_instinct_prompt()
        )
        self.assertIn("用户喜欢用中文交流", result)
        self.assertIn("用户是开发者", result)
        self.assertIn("已学习的经验模式", result)

    def test_parse_instinct_response(self):
        """解析 LLM 返回的"模式描述 | 置信度"格式"""
        # 模拟 LLM 返回
        llm_response = "用户偏好中文对话 | 0.9\n用户经常调试代码 | 0.85\n无效行（无竖线）\n另一个模式 | 0.7"

        # 模拟 router.route 返回
        self.mock_router.route = AsyncMock(return_value=llm_response)
        # 模拟数据库连接
        mock_conn = AsyncMock()
        self.mock_db._conn = mock_conn

        # 调用 extract_instincts
        asyncio.run(
            self.manager.extract_instincts("你好", "你好！", "session_1")
        )

        # 验证 router.route 被调用（免费模型禁用后降级到 router）
        self.mock_router.route.assert_called_once()
        # 验证数据库批量插入被调用，且包含3条有效行
        mock_conn.executemany.assert_called_once()
        inserted_rows = mock_conn.executemany.call_args[0][1]
        self.assertGreaterEqual(len(inserted_rows), 3)

    def test_parse_instinct_response_low_confidence_filtered(self):
        """低置信度的模式被过滤"""
        llm_response = "高价值模式 | 0.9\n低价值模式 | 0.3"

        self.mock_router.route = AsyncMock(return_value=llm_response)
        mock_conn = AsyncMock()
        self.mock_db._conn = mock_conn

        asyncio.run(
            self.manager.extract_instincts("你好", "你好！", "session_1")
        )

        # 只插入高置信度的（0.9 >= 0.5），低置信度（0.3 < 0.5）被过滤
        mock_conn.executemany.assert_called_once()
        inserted_rows = mock_conn.executemany.call_args[0][1]
        self.assertEqual(len(inserted_rows), 1)


if __name__ == '__main__':
    unittest.main()
