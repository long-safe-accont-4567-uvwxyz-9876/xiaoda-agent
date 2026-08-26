"""测试 tool_engine/error_rule_pipeline.py — 规则提取、命中检查、命中计数。

风格参考 tests/test_instinct_manager.py：unittest + asyncio.run + mock。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from tool_engine.error_rule_pipeline import ErrorRulePipeline


def _make_mock_db(fetchone_return=None, fetchall_return=None, lastrowid=42):
    """构造 mock DatabaseManager：_conn 为 AsyncMock，execute 返回 mock cursor。"""
    mock_db = MagicMock()
    mock_conn = AsyncMock()
    mock_db._conn = mock_conn
    mock_cursor = AsyncMock()
    mock_cursor.fetchone.return_value = fetchone_return
    mock_cursor.fetchall.return_value = fetchall_return or []
    mock_cursor.lastrowid = lastrowid
    mock_conn.execute.return_value = mock_cursor
    return mock_db


class TestErrorRulePipelineExtract(unittest.TestCase):
    """extract_rule：成功提取 / 空响应 / 写入数据库"""

    def setUp(self):
        self.mock_router = MagicMock()
        self.mock_db = _make_mock_db(fetchone_return=None, lastrowid=42)
        self.pipeline = ErrorRulePipeline(db=self.mock_db, router=self.mock_router)
        self.pipeline._free_api_key = "fake-key"
        # 清除类级节流时间窗，确保测试间互不影响（extract_rule 的 60s 节流是类变量）
        ErrorRulePipeline._last_extract_time.clear()

    def test_extract_rule_success(self):
        """mock 免费模型返回 'URL 必须以 http 开头 | url_not_http'，规则被写入数据库"""
        self.pipeline._call_free_model = AsyncMock(
            return_value="URL 必须以 http 开头 | url_not_http"
        )
        result = asyncio.run(
            self.pipeline.extract_rule("web_browse", {"url": "ftp://bad"}, "无效 URL")
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["tool_name"], "web_browse")
        self.assertEqual(result["pattern"], "url_not_http")
        self.assertEqual(result["rule_text"], "URL 必须以 http 开头")
        self.assertEqual(result["hit_count"], 0)
        # 验证 INSERT 被调用（count SELECT + 去重 SELECT + INSERT，至少 2 次 execute）
        self.assertGreaterEqual(self.mock_db._conn.execute.await_count, 2)
        self.mock_db._conn.commit.assert_awaited()

    def test_extract_rule_empty_response(self):
        """模型返回空行，extract_rule 返回 None，不写库"""
        self.pipeline._call_free_model = AsyncMock(return_value="")
        result = asyncio.run(
            self.pipeline.extract_rule("web_browse", {"url": "x"}, "some error msg")
        )
        self.assertIsNone(result)
        # 空响应不应触发 INSERT（仅可能有去重 SELECT，但因为提前 return 也不会）
        # 关键：commit 不应被调用
        self.mock_db._conn.commit.assert_not_awaited()


class TestErrorRulePipelineCheck(unittest.TestCase):
    """check_rules：匹配 / 不匹配"""

    def setUp(self):
        self.mock_router = MagicMock()
        self.mock_db = _make_mock_db()
        self.pipeline = ErrorRulePipeline(db=self.mock_db, router=self.mock_router)

    def test_check_rules_match(self):
        """预置规则 pattern=url_not_http（拆为 ['url','http']，'not' 被停用词过滤），
        args 同时含 'url' 和 'http' token → 匹配（AND 逻辑）"""
        rows = [{
            "id": 1, "tool_name": "web_browse",
            "pattern": "url_not_http", "rule_text": "URL 必须以 http 开头",
            "hit_count": 0, "created_at": 0,
        }]
        # 为本次测试重新设置 fetchall
        cursor = self.mock_db._conn.execute.return_value
        cursor.fetchall.return_value = rows
        matched = asyncio.run(
            self.pipeline.check_rules("web_browse", {"url": "http://bad"})
        )
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["pattern"], "url_not_http")

    def test_check_rules_no_match(self):
        """pattern token 都不在参数 JSON 中 → 返回空列表"""
        rows = [{
            "id": 1, "tool_name": "web_browse",
            "pattern": "url_not_http", "rule_text": "URL 必须以 http 开头",
            "hit_count": 0, "created_at": 0,
        }]
        cursor = self.mock_db._conn.execute.return_value
        cursor.fetchall.return_value = rows
        matched = asyncio.run(
            self.pipeline.check_rules("web_browse", {"query": "hello"})
        )
        self.assertEqual(matched, [])

    def test_check_rules_and_logic_partial_match(self):
        """AND 逻辑：args 只含部分 token（'url' 但不含 'http'）→ 不匹配"""
        rows = [{
            "id": 1, "tool_name": "web_browse",
            "pattern": "url_not_http", "rule_text": "URL 必须以 http 开头",
            "hit_count": 0, "created_at": 0,
        }]
        cursor = self.mock_db._conn.execute.return_value
        cursor.fetchall.return_value = rows
        # args 含 'url' 但不含 'http'，AND 逻辑下不应匹配
        matched = asyncio.run(
            self.pipeline.check_rules("web_browse", {"url": "ftp://bad"})
        )
        self.assertEqual(matched, [])

    def test_check_rules_stopword_only_pattern_no_match(self):
        """pattern 拆分后全是停用词/短 token（如 'has_no' → 过滤后为空）→ 不匹配"""
        rows = [{
            "id": 1, "tool_name": "web_browse",
            "pattern": "has_no", "rule_text": "无意义规则",
            "hit_count": 0, "created_at": 0,
        }]
        cursor = self.mock_db._conn.execute.return_value
        cursor.fetchall.return_value = rows
        matched = asyncio.run(
            self.pipeline.check_rules("web_browse", {"has": "no", "url": "http://x"})
        )
        self.assertEqual(matched, [])


class TestErrorRulePipelineIncrement(unittest.TestCase):
    """increment_hit_count：命中后递增"""

    def test_increment_hit_count(self):
        mock_router = MagicMock()
        mock_db = _make_mock_db()
        pipeline = ErrorRulePipeline(db=mock_db, router=mock_router)
        asyncio.run(pipeline.increment_hit_count(7))
        # 验证 UPDATE 被调用，参数为 rule_id=7
        mock_db._conn.execute.assert_awaited_once()
        sql_arg = mock_db._conn.execute.await_args.args[0]
        params_arg = mock_db._conn.execute.await_args.args[1]
        self.assertIn("UPDATE", sql_arg)
        self.assertIn("hit_count", sql_arg)
        self.assertEqual(params_arg, (7,))
        mock_db._conn.commit.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
