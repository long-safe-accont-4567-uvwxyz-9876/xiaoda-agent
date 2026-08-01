"""Context governance (精简版) — 哈希链版本管理 + 审计追踪

保留 ContextGovernance 类名和核心接口, 简化内部实现。
"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from loguru import logger


def compute_content_hash(summary: str) -> str:
    """SHA-256 of memory summary."""
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


class ContextGovernance:
    """记忆版本管理 + 上下文审计 (精简版)"""

    def __init__(self, conn: Any = None) -> None:
        self._conn = conn

    async def record_initial_version(self, memory_id: int, summary: str,
                                       auto_commit: bool = True) -> str:
        """记录记忆初始版本 (version=1)"""
        content_hash = compute_content_hash(summary)
        if not self._conn:
            return content_hash
        try:
            cursor = await self._conn.execute(
                "SELECT id FROM memory_versions WHERE memory_id=? AND version=1",
                (memory_id,),
            )
            if await cursor.fetchone():
                await self._conn.execute(
                    "UPDATE episodic_memories SET content_hash=?, version=1 WHERE id=?",
                    (content_hash, memory_id),
                )
                if auto_commit:
                    await self._conn.commit()
                return content_hash
            await self._conn.execute(
                "INSERT INTO memory_versions "
                "(memory_id, version, content_hash, prev_hash, summary_snapshot, created_at) "
                "VALUES (?, 1, ?, '', ?, ?)",
                (memory_id, content_hash, summary[:500], time.time()),
            )
            await self._conn.execute(
                "UPDATE episodic_memories SET content_hash=?, version=1 WHERE id=?",
                (content_hash, memory_id),
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.warning("governance.record_initial_failed", memory_id=memory_id, error=str(e))
        return content_hash

    async def record_version_update(self, memory_id: int, new_summary: str,
                                      auto_commit: bool = True) -> str | None:
        """记录记忆更新版本"""
        new_hash = compute_content_hash(new_summary)
        if not self._conn:
            return new_hash
        try:
            cursor = await self._conn.execute(
                "SELECT version, content_hash FROM episodic_memories WHERE id=?",
                (memory_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            current_version = row[0] or 1
            current_hash = row[1] or ""
            if current_hash == new_hash:
                return current_hash
            new_version = current_version + 1
            await self._conn.execute(
                "INSERT INTO memory_versions "
                "(memory_id, version, content_hash, prev_hash, summary_snapshot, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (memory_id, new_version, new_hash, current_hash, new_summary[:500], time.time()),
            )
            await self._conn.execute(
                "UPDATE episodic_memories SET content_hash=?, version=? WHERE id=?",
                (new_hash, new_version, memory_id),
            )
            if auto_commit:
                await self._conn.commit()
        except Exception as e:
            logger.warning("governance.record_update_failed", memory_id=memory_id, error=str(e))
            return None
        return new_hash

    async def audit_context_consumption(self, response_id: str,
                                          memories: list[dict],
                                          auto_commit: bool = True) -> int:
        """记录审计追踪"""
        if not memories or not self._conn:
            return 0
        now = time.time()
        inserted = 0
        for rank, mem in enumerate(memories):
            mem_id = mem.get("id")
            if mem_id is None:
                continue
            content_hash = mem.get("content_hash", "")
            version = mem.get("version", 1)
            score = float(mem.get("final_score", mem.get("rerank_score",
                          mem.get("rrf_score", mem.get("score", 0.0)))) or 0.0)
            source = mem.get("source_label", "retrieval")
            try:
                await self._conn.execute(
                    "INSERT INTO context_audit_log "
                    "(response_id, memory_id, content_hash, version, score, source, rank, retrieved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (response_id, mem_id, content_hash, version, score, source, rank, now),
                )
                inserted += 1
            except Exception:
                pass
        if inserted and auto_commit:
            await self._conn.commit()
        return inserted

    async def verify_hash_chain(self, memory_id: int) -> dict:
        """验证哈希链完整性"""
        if not self._conn:
            return {"valid": True, "broken_at_version": None, "versions": 0, "detail": "no conn"}
        try:
            cursor = await self._conn.execute(
                "SELECT version, content_hash, prev_hash, summary_snapshot "
                "FROM memory_versions WHERE memory_id=? ORDER BY version",
                (memory_id,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return {"valid": False, "broken_at_version": None, "versions": 0, "detail": "no history"}
            prev_hash = ""
            for row in rows:
                version, content_hash, stored_prev, snapshot = row
                if version == 1 and stored_prev != "":
                    return {"valid": False, "broken_at_version": 1, "versions": len(rows), "detail": "v1 prev_hash not empty"}
                if version > 1 and stored_prev != prev_hash:
                    return {"valid": False, "broken_at_version": version, "versions": len(rows), "detail": "prev_hash mismatch"}
                if snapshot and compute_content_hash(snapshot) != content_hash:
                    return {"valid": False, "broken_at_version": version, "versions": len(rows), "detail": "tampered"}
                prev_hash = content_hash
            return {"valid": True, "broken_at_version": None, "versions": len(rows), "detail": "chain intact"}
        except Exception as e:
            return {"valid": False, "broken_at_version": None, "versions": 0, "detail": str(e)}

    async def reconstruct_context(self, response_id: str) -> list[dict]:
        """Point-in-time 重建上下文"""
        if not self._conn:
            return []
        try:
            cursor = await self._conn.execute(
                "SELECT memory_id, content_hash, version, score, source, rank, retrieved_at "
                "FROM context_audit_log WHERE response_id=? ORDER BY rank",
                (response_id,),
            )
            return [dict(r) for r in await cursor.fetchall()]
        except Exception:
            return []

    @staticmethod
    def new_response_id() -> str:
        return uuid.uuid4().hex
