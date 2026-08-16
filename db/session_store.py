"""会话存储抽象层 — 借鉴 Claude Agent SDK 的 SessionStore 设计"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from loguru import logger


@dataclass
class SessionInfo:
    """会话元信息"""
    session_id: str
    summary: str
    last_modified: int  # Unix epoch milliseconds
    custom_title: str | None = None
    first_prompt: str | None = None
    tag: str | None = None
    created_at: int | None = None


@dataclass
class SessionSummaryData:
    """增量摘要数据（opaque，存储层不应解释）"""
    first_prompt: str | None = None
    first_prompt_locked: bool = False
    custom_title: str | None = None
    ai_title: str | None = None
    last_prompt: str | None = None
    summary_hint: str | None = None
    tag: str | None = None
    created_at: int | None = None


@dataclass
class SessionSummaryEntry:
    """增量维护的会话摘要条目"""
    session_id: str
    mtime: int  # Unix epoch ms
    data: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SessionStoreProtocol(Protocol):
    """会话存储协议 — 借鉴 Claude Agent SDK 的 SessionStore"""

    async def append_session_entry(self, session_id: str, entry: dict[str, Any]) -> None:
        """追加一条会话条目"""
        ...

    async def load_session(self, session_id: str) -> list[dict[str, Any]] | None:
        """加载完整会话"""
        ...

    async def list_sessions(self, project_key: str = "default") -> list[SessionInfo]:
        """列出所有会话"""
        ...

    async def delete_session(self, session_id: str) -> None:
        """删除会话"""
        ...

    async def rename_session(self, session_id: str, new_title: str) -> None:
        """重命名会话"""
        ...

    async def tag_session(self, session_id: str, tag: str) -> None:
        """为会话添加标签"""
        ...

    async def fork_session(self, session_id: str) -> str | None:
        """Fork 一个会话，返回新会话 ID"""
        ...


def fold_session_summary(
    prev: SessionSummaryEntry | None,
    session_id: str,
    entry: dict[str, Any],
) -> SessionSummaryEntry:
    """增量折叠会话摘要 — 避免全量重读

    每次追加新条目时调用，增量更新摘要数据。
    prev 为 None 时创建新摘要。
    """
    if prev is not None:
        summary = SessionSummaryEntry(
            session_id=prev.session_id,
            mtime=prev.mtime,
            data=dict(prev.data),
        )
    else:
        summary = SessionSummaryEntry(session_id=session_id, mtime=0, data={})

    data = summary.data

    # 提取时间戳
    ts = entry.get("timestamp")
    if isinstance(ts, str):
        try:
            from datetime import datetime
            norm = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
            ms = int(datetime.fromisoformat(norm).timestamp() * 1000)
            if "created_at" not in data:
                data["created_at"] = ms
        except (ValueError, OSError):
            logger.debug("session_store.timestamp_parse_failed", exc_info=True)

    # 提取首条提示词（仅用户消息，仅一次）
    if not data.get("first_prompt_locked") and entry.get("type") == "user" and not entry.get("isMeta"):
        content = entry.get("content", "")
        if isinstance(content, str):
            text = content.replace("\n", " ").strip()
        elif isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            text = " ".join(texts).strip()
        else:
            text = ""

        if text and len(text) <= 200:
            data["first_prompt"] = text
            data["first_prompt_locked"] = True

    # Last-wins 字段
    last_wins = {
        "custom_title": "custom_title",
        "ai_title": "ai_title",
        "last_prompt": "last_prompt",
        "summary_hint": "summary_hint",
    }
    for src, dst in last_wins.items():
        val = entry.get(src)
        if isinstance(val, str) and val:
            data[dst] = val

    # 标签
    if entry.get("type") == "tag":
        tag_val = entry.get("tag")
        if isinstance(tag_val, str) and tag_val:
            data["tag"] = tag_val
        else:
            data.pop("tag", None)

    return summary


def summary_to_session_info(entry: SessionSummaryEntry) -> SessionInfo | None:
    """将 SessionSummaryEntry 转换为 SessionInfo"""
    data = entry.data if isinstance(entry.data, dict) else {}

    first_prompt = data.get("first_prompt") if data.get("first_prompt_locked") else None
    custom_title = data.get("custom_title") or data.get("ai_title")
    summary = (
        custom_title
        or data.get("last_prompt")
        or data.get("summary_hint")
        or first_prompt
    )
    if not summary:
        return None

    return SessionInfo(
        session_id=entry.session_id,
        summary=summary,
        last_modified=entry.mtime,
        custom_title=custom_title,
        first_prompt=first_prompt,
        tag=data.get("tag"),
        created_at=data.get("created_at"),
    )


class SessionStoreMixin:
    # ── SessionStoreProtocol 实现 ──────────────────────────────────

    async def append_session_entry(self, session_id: str, entry: dict[str, Any]) -> None:
        """追加一条会话条目，并增量折叠摘要"""
        now = time.time()
        entry_json = json.dumps(entry, ensure_ascii=False)
        await self._conn.execute(
            """INSERT INTO session_entries (session_id, entry_json, created_at)
               VALUES (?, ?, ?)""",
            (session_id, entry_json, now),
        )

        # 加载已有摘要
        prev_summary = await self._load_summary_entry(session_id)

        # 增量折叠
        new_summary = fold_session_summary(prev_summary, session_id, entry)
        new_summary.mtime = int(now * 1000)

        # 持久化摘要
        await self._conn.execute(
            """INSERT OR REPLACE INTO session_summaries (session_id, mtime, summary_data)
               VALUES (?, ?, ?)""",
            (session_id, new_summary.mtime, json.dumps(new_summary.data, ensure_ascii=False)),
        )
        await self._conn.commit()

    async def load_session(self, session_id: str) -> list[dict[str, Any]] | None:
        """加载完整会话条目列表"""
        cursor = await self._conn.execute(
            """SELECT entry_json FROM session_entries
               WHERE session_id=? ORDER BY created_at ASC, id ASC""",
            (session_id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return None
        result = []
        for row in rows:
            try:
                result.append(json.loads(row["entry_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
        return result

    async def list_sessions(self, project_key: str = "default") -> list[SessionInfo]:
        """列出所有会话（含增量摘要信息）"""
        cursor = await self._conn.execute(
            """SELECT s.id, s.summary, s.ended_at, s.started_at, s.status,
                      sm.mtime, sm.summary_data
               FROM sessions s
               LEFT JOIN session_summaries sm ON s.id = sm.session_id
               ORDER BY COALESCE(sm.mtime, s.ended_at * 1000, 0) DESC"""
        )
        rows = await cursor.fetchall()

        results: list[SessionInfo] = []
        for row in rows:
            info = self._build_session_info(row)
            if info is not None:
                results.append(info)
        return results

    def _build_session_info(self, row: Any) -> SessionInfo | None:
        """从一行 sessions+session_summaries 联表结果构造 SessionInfo；无有效摘要返回 None。"""
        sid = row["id"]
        summary_text = row["summary"] or ""
        mtime = row["mtime"] or int((row["ended_at"] or row["started_at"] or 0) * 1000)
        summary_data = {}
        try:
            summary_data = json.loads(row["summary_data"]) if row["summary_data"] else {}
        except (json.JSONDecodeError, TypeError):
            logger.debug("database.summary_data_parse_failed", exc_info=True)
        custom_title = summary_data.get("custom_title") or summary_data.get("ai_title")
        first_prompt = summary_data.get("first_prompt") if summary_data.get("first_prompt_locked") else None
        display_summary = (
            custom_title
            or summary_data.get("last_prompt")
            or summary_data.get("summary_hint")
            or first_prompt
            or summary_text
        )
        if not display_summary:
            return None
        return SessionInfo(
            session_id=sid,
            summary=display_summary,
            last_modified=mtime,
            custom_title=custom_title,
            first_prompt=first_prompt,
            tag=summary_data.get("tag"),
            created_at=summary_data.get("created_at"),
        )

    async def delete_session(self, session_id: str) -> None:
        """删除会话及其所有条目和摘要"""
        await self._conn.execute("DELETE FROM session_entries WHERE session_id=?", (session_id,))
        await self._conn.execute("DELETE FROM session_summaries WHERE session_id=?", (session_id,))
        await self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        await self._conn.commit()

    async def rename_session(self, session_id: str, new_title: str) -> None:
        """重命名会话（更新 custom_title）"""
        # 更新 sessions 表的 summary
        await self._conn.execute(
            "UPDATE sessions SET summary=? WHERE id=?",
            (new_title, session_id),
        )
        # 更新增量摘要中的 custom_title
        prev = await self._load_summary_entry(session_id)
        if prev is None:
            prev = SessionSummaryEntry(session_id=session_id, mtime=int(time.time() * 1000), data={})
        prev.data["custom_title"] = new_title
        prev.mtime = int(time.time() * 1000)
        await self._conn.execute(
            """INSERT OR REPLACE INTO session_summaries (session_id, mtime, summary_data)
               VALUES (?, ?, ?)""",
            (session_id, prev.mtime, json.dumps(prev.data, ensure_ascii=False)),
        )
        await self._conn.commit()

    async def tag_session(self, session_id: str, tag: str) -> None:
        """为会话添加标签"""
        prev = await self._load_summary_entry(session_id)
        if prev is None:
            prev = SessionSummaryEntry(session_id=session_id, mtime=int(time.time() * 1000), data={})
        prev.data["tag"] = tag
        prev.mtime = int(time.time() * 1000)
        await self._conn.execute(
            """INSERT OR REPLACE INTO session_summaries (session_id, mtime, summary_data)
               VALUES (?, ?, ?)""",
            (session_id, prev.mtime, json.dumps(prev.data, ensure_ascii=False)),
        )
        await self._conn.commit()

    async def fork_session(self, session_id: str) -> str | None:
        """Fork 一个会话，返回新会话 ID"""
        # 加载原始会话条目
        entries = await self.load_session(session_id)
        if entries is None:
            return None

        # 获取原始会话信息
        cursor = await self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
        orig = await cursor.fetchone()
        if not orig:
            return None

        # 创建新会话
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        new_id = f"SES-{date_str}-{int(now % 100000):05d}"

        await self._conn.execute(
            """INSERT INTO sessions (id, user_openid, summary, turn_count, total_cost_usd,
               cache_hit_tokens, cache_miss_tokens, started_at, ended_at, status)
               VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?, 'active')""",
            (new_id, orig["user_openid"], f"Fork of {session_id}", now, now),
        )

        # 复制所有条目
        for entry in entries:
            entry_json = json.dumps(entry, ensure_ascii=False)
            await self._conn.execute(
                """INSERT INTO session_entries (session_id, entry_json, created_at)
                   VALUES (?, ?, ?)""",
                (new_id, entry_json, now),
            )

        # 复制摘要
        prev = await self._load_summary_entry(session_id)
        if prev is not None:
            new_summary = SessionSummaryEntry(
                session_id=new_id,
                mtime=int(now * 1000),
                data=dict(prev.data),
            )
            await self._conn.execute(
                """INSERT OR REPLACE INTO session_summaries (session_id, mtime, summary_data)
                   VALUES (?, ?, ?)""",
                (new_id, new_summary.mtime, json.dumps(new_summary.data, ensure_ascii=False)),
            )

        await self._conn.commit()
        return new_id

    async def _load_summary_entry(self, session_id: str) -> SessionSummaryEntry | None:
        """从 session_summaries 表加载摘要条目"""
        cursor = await self._conn.execute(
            "SELECT mtime, summary_data FROM session_summaries WHERE session_id=?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            data = json.loads(row["summary_data"])
        except (json.JSONDecodeError, TypeError):
            data = {}
        return SessionSummaryEntry(
            session_id=session_id,
            mtime=row["mtime"],
            data=data,
        )
