"""会话存储抽象层 — 借鉴 Claude Agent SDK 的 SessionStore 设计"""
from __future__ import annotations

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
