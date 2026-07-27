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
    """测试 archive_stale 清理从未使用的垃圾本能。

    根因（2026-07-27 生产事故）：
    - 本能表 4714 条 active，其中 4702 条 use_count=0（从未被 get_active_instincts 选中）
    - archive_stale 只按 last_used_at < cutoff(30天) 归档
    - 但 INSERT 时 last_used_at = now，导致刚创建的本能 30 天内不会被归档
    - 垃圾无限堆积 → merge_duplicates O(n²) 处理 4714 条卡 370 秒 → 事件循环冻结

    修复：archive_stale 额外归档 use_count=0 且 created_at < cutoff 的本能
    （创建超过 N 天但从未被使用 = 垃圾）
    """

    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_router = MagicMock()
        self.manager = InstinctManager(db=self.mock_db, router=self.mock_router)
        self.manager._available = True

    def test_archive_stale_archives_never_used_old_instincts(self):
        """archive_stale 应归档 use_count=0 且创建超过阈值的本能"""
        import time as _time
        now = _time.time()

        # mock DB：第一次 COUNT 查询返回垃圾数量，第二次 UPDATE 返回影响行数
        # archive_stale 现在的逻辑：先 COUNT 再 UPDATE
        call_count = {"count": 0}

        async def mock_execute(sql, params=()):
            call_count["count"] += 1
            mock_cursor = AsyncMock()
            if "COUNT" in sql:
                # row[0] 访问第一列，返回 [42] 模拟 sqlite3.Row
                mock_cursor.fetchone = AsyncMock(return_value=[42])
            else:
                mock_cursor.rowcount = 42
            return mock_cursor

        self.mock_db._conn.execute = AsyncMock(side_effect=mock_execute)
        self.mock_db._conn.commit = AsyncMock()

        asyncio.run(self.manager.archive_stale(max_age_days=7))

        # 断言：SQL 应该同时检查 use_count=0（不只是 last_used_at）
        execute_calls = self.mock_db._conn.execute.call_args_list
        all_sql = " ".join(str(c) for c in execute_calls)
        self.assertIn(
            "use_count", all_sql,
            f"archive_stale 的 SQL 应包含 use_count=0 条件，实际: {all_sql}"
        )

    def test_archive_stale_keeps_recently_created_unused(self):
        """archive_stale 不应归档刚创建的 use_count=0 本能（给它们被使用的机会）"""
        # 验证 SQL 中有 created_at 条件（控制只归档创建超 N 天的）
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=[0])
        self.mock_db._conn.execute = AsyncMock(return_value=mock_cursor)
        self.mock_db._conn.commit = AsyncMock()

        asyncio.run(self.manager.archive_stale(max_age_days=7))

        execute_calls = self.mock_db._conn.execute.call_args_list
        all_sql = " ".join(str(c) for c in execute_calls)
        self.assertTrue(
            "created_at" in all_sql or "last_used_at" in all_sql,
            f"archive_stale 的 SQL 应有时间条件，实际: {all_sql}"
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
