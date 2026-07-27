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


class TestArchiveStaleCleansGarbage(unittest.TestCase):
    """测试 archive_stale 只清明确垃圾，不一刀切。

    设计原则（2026-07-27 用户反馈后重设计）：
    - use_count=0 是 get_active_instincts 只取 top 6 的排序副产品，不是垃圾证据
    - archive_stale 只归档"明确垃圾"：空内容、过短碎片、真正长期未使用
    - 疑似重复/低价值不自动处理，由 correct_instinct 对话内修正
    """

    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_router = MagicMock()
        self.manager = InstinctManager(db=self.mock_db, router=self.mock_router)
        self.manager._available = True

    def test_archive_stale_does_not_use_count_condition(self):
        """archive_stale 的 SQL 不应包含 use_count 条件（避免一刀切归档）"""
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=[0])
        self.mock_db._conn.execute = AsyncMock(return_value=mock_cursor)
        self.mock_db._conn.commit = AsyncMock()

        asyncio.run(self.manager.archive_stale(max_age_days=30))

        execute_calls = self.mock_db._conn.execute.call_args_list
        all_sql = " ".join(str(c) for c in execute_calls)
        self.assertNotIn(
            "use_count", all_sql,
            f"archive_stale 的 SQL 不应包含 use_count 条件（一刀切），实际: {all_sql}"
        )

    def test_archive_stale_checks_empty_and_short_content(self):
        """archive_stale 应检查空内容和过短碎片"""
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=[0])
        self.mock_db._conn.execute = AsyncMock(return_value=mock_cursor)
        self.mock_db._conn.commit = AsyncMock()

        asyncio.run(self.manager.archive_stale(max_age_days=30))

        execute_calls = self.mock_db._conn.execute.call_args_list
        all_sql = " ".join(str(c) for c in execute_calls)
        self.assertTrue(
            "TRIM" in all_sql or "LENGTH" in all_sql,
            f"archive_stale 的 SQL 应检查空/短内容，实际: {all_sql}"
        )

    def test_archive_stale_has_last_used_at_condition(self):
        """archive_stale 应有 last_used_at 时间条件（长期未使用才归档）"""
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=[0])
        self.mock_db._conn.execute = AsyncMock(return_value=mock_cursor)
        self.mock_db._conn.commit = AsyncMock()

        asyncio.run(self.manager.archive_stale(max_age_days=30))

        execute_calls = self.mock_db._conn.execute.call_args_list
        all_sql = " ".join(str(c) for c in execute_calls)
        self.assertIn(
            "last_used_at", all_sql,
            f"archive_stale 的 SQL 应有 last_used_at 时间条件，实际: {all_sql}"
        )


class TestExtractInstinctsDedup(unittest.TestCase):
    """测试 extract_instincts 插入前去重，防止本能表无限膨胀。

    根因：extract_instincts 每轮对话都提取 2-5 条新本能，INSERT 前不检查
    是否已有相似内容。LLM 每次措辞略有不同（如"用户喜欢亲密互动" vs
    "用户偏好亲密行为"），导致语义重复的本能无限堆积（4714 条，99.7% 垃圾）。
    """

    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_router = MagicMock()
        self.manager = InstinctManager(db=self.mock_db, router=self.mock_router)
        self.manager._available = True
        # mock 免费模型返回固定结果，跳过 LLM 调用
        self.manager._call_free_model = AsyncMock(return_value="用户喜欢亲密互动|0.8")

    def test_extract_skips_similar_to_existing(self):
        """当新本能与已有 active instinct 高度相似时，不应 INSERT"""
        # 已有本能："用户偏好亲密互动"（与新提取的"用户喜欢亲密互动"高度相似）
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"content": "用户偏好亲密互动"},
        ])
        self.mock_db._conn.execute = AsyncMock(return_value=mock_cursor)
        self.mock_db._conn.executemany = AsyncMock()
        self.mock_db._conn.commit = AsyncMock()

        asyncio.run(self.manager.extract_instincts("测试输入", "测试回复", "session1"))

        # 断言：executemany（INSERT）不应被调用，因为新本能与已有本能相似
        self.mock_db._conn.executemany.assert_not_called()

    def test_extract_inserts_genuinely_new(self):
        """当新本能与已有 active instinct 不相似时，应正常 INSERT"""
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"content": "用户重视承诺，厌恶言而无信"},  # 完全不同的话题
        ])
        self.mock_db._conn.execute = AsyncMock(return_value=mock_cursor)
        self.mock_db._conn.executemany = AsyncMock()
        self.mock_db._conn.commit = AsyncMock()

        asyncio.run(self.manager.extract_instincts("测试输入", "测试回复", "session1"))

        # 断言：executemany（INSERT）应被调用
        self.mock_db._conn.executemany.assert_called_once()

class TestCorrectInstinct(unittest.TestCase):
    """测试 LLM 驱动的本能修正：correct_instinct(hint, action)

    设计原则（2026-07-27 用户反馈驱动）：
    - LLM 在 extract_instincts 里判断用户否定后，调用 correct_instinct 修正
    - 不靠硬编码词表轮询，零硬代码
    - 只在当前 active top 6 本能里定位（最可能被否定的）
    - action 由 LLM 决定：demote=降权，archive=归档
    """

    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_router = MagicMock()
        self.manager = InstinctManager(db=self.mock_db, router=self.mock_router)
        self.manager._available = True

    def test_empty_hint_returns_none(self):
        """空 hint 不触发修正"""
        result = asyncio.run(self.manager.correct_instinct("", "demote"))
        self.assertIsNone(result)

    def test_no_match_returns_none(self):
        """hint 与所有本能都不匹配时，不修正（避免误伤）"""
        async def mock_get_active(limit=6, min_confidence=0.0):
            return [{
                "id": 3,
                "content": "用户偏好浓香入味的菜品",
                "confidence": 0.8,
            }]
        self.manager.get_active_instincts = mock_get_active

        mock_cursor = AsyncMock()
        self.mock_db._conn.execute = AsyncMock(return_value=mock_cursor)
        self.mock_db._conn.commit = AsyncMock()

        result = asyncio.run(self.manager.correct_instinct("用户喜欢被打断时继续说", "demote"))

        self.assertIsNone(result)
        self.mock_db._conn.execute.assert_not_called()

    def test_demote_action_lowers_confidence(self):
        """action=demote 时，应降权（confidence *= 0.5）"""
        async def mock_get_active(limit=6, min_confidence=0.0):
            return [{
                "id": 1,
                "content": "用户喜欢被打断时继续说",
                "confidence": 0.8,
            }]
        self.manager.get_active_instincts = mock_get_active

        mock_cursor = AsyncMock()
        self.mock_db._conn.execute = AsyncMock(return_value=mock_cursor)
        self.mock_db._conn.commit = AsyncMock()

        result = asyncio.run(self.manager.correct_instinct("用户喜欢被打断时继续说", "demote"))

        self.assertIsNotNone(result)
        self.assertTrue(result["corrected"])
        self.assertEqual(result["action"], "demoted")
        self.assertAlmostEqual(result["old_conf"], 0.8)
        self.assertAlmostEqual(result["new_conf"], 0.4)
        execute_calls = self.mock_db._conn.execute.call_args_list
        all_sql = " ".join(str(c) for c in execute_calls)
        self.assertIn("UPDATE instincts SET confidence", all_sql)

    def test_archive_action_archives_instinct(self):
        """action=archive 时，应归档（status='archived'）"""
        async def mock_get_active(limit=6, min_confidence=0.0):
            return [{
                "id": 2,
                "content": "用户喜欢被打断时继续说",
                "confidence": 0.8,
            }]
        self.manager.get_active_instincts = mock_get_active

        mock_cursor = AsyncMock()
        self.mock_db._conn.execute = AsyncMock(return_value=mock_cursor)
        self.mock_db._conn.commit = AsyncMock()

        result = asyncio.run(self.manager.correct_instinct("用户喜欢被打断时继续说", "archive"))

        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "archived")
        execute_calls = self.mock_db._conn.execute.call_args_list
        all_sql = " ".join(str(c) for c in execute_calls)
        self.assertIn("status='archived'", all_sql)

    def test_default_action_is_demote(self):
        """不传 action 时，默认 demote"""
        async def mock_get_active(limit=6, min_confidence=0.0):
            return [{
                "id": 1,
                "content": "用户喜欢被打断时继续说",
                "confidence": 0.8,
            }]
        self.manager.get_active_instincts = mock_get_active

        mock_cursor = AsyncMock()
        self.mock_db._conn.execute = AsyncMock(return_value=mock_cursor)
        self.mock_db._conn.commit = AsyncMock()

        result = asyncio.run(self.manager.correct_instinct("用户喜欢被打断时继续说"))

        self.assertIsNotNone(result)
        self.assertEqual(result["action"], "demoted")
