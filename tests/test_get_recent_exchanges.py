"""get_recent_exchanges —— 回复去重窗口的 DB 层测试。

2026-08-21 根因（用户"相同内容一直返回相同回复/重复度80%"）：
  去重原本只查最近 1 条回复，用户相同问题隔几轮再发时旧回复已滚出窗口
  → 换 (user_message, assistant_reply) 交换对查询、按 user 消息相似度定位。
"""
import asyncio
import os
import tempfile

import pytest

from db.database import DatabaseManager


@pytest.mark.asyncio
async def test_get_recent_exchanges_newest_first_and_source_filter():
    db_path = tempfile.mktemp(suffix=".db")
    db = DatabaseManager(db_path)
    await db.init()
    try:
        await db.insert_conversation_log("qq_t1", "qq_c2c", "在吗", "旧回复", auto_commit=False)
        await db.insert_conversation_log("qq_t1", "qq_c2c", "看看", "小妲给你看看哦", auto_commit=False)
        await db.insert_conversation_log("qq_t1", "wechat_c2c", "聊聊", "另一渠道的回复", auto_commit=False)
        await db.insert_conversation_log("qq_t1", "qq_c2c", "快一点", "再近一点啦")

        # 最新在前
        got = await db.get_recent_exchanges("qq_t1", source="qq_c2c", limit=2)
        assert got == [("快一点", "再近一点啦"), ("看看", "小妲给你看看哦")], got
        # 空 source 不限渠道
        got_all = await db.get_recent_exchanges("qq_t1", source="", limit=20)
        assert len(got_all) == 4
        # 未命中用户返回空
        assert await db.get_recent_exchanges("nobody", limit=5) == []
    finally:
        await db.close()
        os.unlink(db_path)