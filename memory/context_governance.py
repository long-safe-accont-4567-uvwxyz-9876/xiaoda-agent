"""ContextNest-inspired context governance layer.

Implements two mechanisms from ContextNest (arXiv:2607.02116v1):
- §7 SHA-256 hash-chained version histories for episodic memories (tamper-evident)
- §9 audit traces of agent context consumption (point-in-time reconstruction)

This is a governance layer beneath retrieval; it does not replace RAG.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any

from loguru import logger

# 哈希口径版本（数据库小任务B-4）：
#   v1（历史口径）：对完整 summary 全文计算——但 memory_versions.summary_snapshot
#     按 [:500] 截断存储，>500 字符记忆 verify 时用快照重算必然 mismatch（假阳性）。
#   v2（当前口径）：对「实际存储快照」summary[:_SNAPSHOT_HASH_LIMIT] 计算，
#     写入（record_*）与验证（verify_hash_chain）同一口径，长文本链恒 valid。
# 存量 v1 记录由 migrate_legacy_hashes() 显式修复（不在启动时自动执行）。
HASH_ALGO_VERSION = "v2-snapshot-500"
_SNAPSHOT_HASH_LIMIT = 500


def compute_content_hash(summary: str) -> str:
    """SHA-256 of memory summary. Used as version identity + integrity check.

    口径（HASH_ALGO_VERSION=v2）：超过快照上限的文本按实际会存储的截断快照
    计算——完整文本与其截断快照是同一存储事实，必须得到同一哈希。
    """
    return hashlib.sha256(
        summary[:_SNAPSHOT_HASH_LIMIT].encode("utf-8")).hexdigest()


async def migrate_legacy_hashes(conn: Any) -> int:
    """一次性存量迁移：旧口径（全文哈希）→ 新口径（快照哈希）。

    识别签名：version 行 content_hash == sha256(当前完整 summary) 且
    != 快照口径重算值——即旧代码按全文计算的产物。不满足签名的差异行
    （疑似篡改/未知来源）一律不动，交由 verify_hash_chain 报告，保持
    fail-closed 完整性语义。修复时同步 em.content_hash 与后续版本的
    prev_hash 链接。幂等：新口径记录不满足签名，重复调用返回 0。
    由调用方显式触发，不在启动时自动执行。返回修复的 version 行数。
    """
    repaired = 0
    cursor = await conn.execute(
        "SELECT mv.id, mv.memory_id, mv.content_hash, mv.summary_snapshot, "
        "em.summary, em.content_hash "
        "FROM memory_versions mv JOIN episodic_memories em ON em.id = mv.memory_id"
    )
    rows = await cursor.fetchall()
    prev_remap: dict[tuple[int, str], str] = {}
    for row in rows:
        row_id, memory_id = row[0], row[1]
        stored, snapshot = row[2] or "", row[3] or ""
        summary, em_hash = row[4] or "", row[5] or ""
        target = compute_content_hash(snapshot)
        if stored == target:
            continue  # 已是新口径
        legacy_sig = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        if stored != legacy_sig:
            logger.warning(
                "governance.legacy_hash_skip_unrecognized",
                memory_id=memory_id, detail="哈希既非旧口径签名也非新口径")
            continue
        await conn.execute(
            "UPDATE memory_versions SET content_hash=? WHERE id=?",
            (target, row_id))
        if em_hash == stored:
            await conn.execute(
                "UPDATE episodic_memories SET content_hash=? WHERE id=?",
                (target, memory_id))
        prev_remap[(memory_id, stored)] = target
        repaired += 1
    # 后续版本的 prev_hash 若指向被修复的旧哈希，重接到新哈希保持链连续
    for (memory_id, old_hash), new_hash in prev_remap.items():
        await conn.execute(
            "UPDATE memory_versions SET prev_hash=? "
            "WHERE memory_id=? AND prev_hash=?",
            (new_hash, memory_id, old_hash))
    await conn.commit()
    if repaired:
        logger.info("governance.legacy_hash_migrated",
                    repaired=repaired, algo=HASH_ALGO_VERSION)
    return repaired


class ContextGovernance:
    """Manages memory version chains and context audit trails.

    Wire into MemoryManager:
    - on memory insert → ``record_initial_version``
    - on memory enrichment update → ``record_version_update``
    - on retrieval return → ``audit_context_consumption``
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def record_initial_version(self, memory_id: int, summary: str,
                                       auto_commit: bool = True) -> str:
        """记录记忆初始版本 (version=1, prev_hash="")。

        同时更新 episodic_memories.content_hash 和 version 列。
        幂等: 若 memory_versions 已存在该 memory_id 的 v1 记录则跳过。
        """
        content_hash = compute_content_hash(summary)
        try:
            # 检查是否已有 v1 记录 (幂等)
            cursor = await self._conn.execute(
                "SELECT id FROM memory_versions WHERE memory_id=? AND version=1",
                (memory_id,),
            )
            existing = await cursor.fetchone()
            if existing:
                # 已存在, 只同步 episodic_memories 列 (可能在迁移前创建的旧记忆)
                await self._conn.execute(
                    "UPDATE episodic_memories SET content_hash=?, version=1 WHERE id=?",
                    (content_hash, memory_id),
                )
                if auto_commit:
                    await self._conn.commit()
                return content_hash
            # 写入哈希链创世记录
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
            logger.debug("governance.version_initial", memory_id=memory_id, hash=content_hash[:12])
        except Exception as e:
            logger.warning("governance.record_initial_failed",
                           memory_id=memory_id, error=str(e))
        return content_hash

    async def record_version_update(self, memory_id: int, new_summary: str,
                                      auto_commit: bool = True,
                                      strict: bool = False) -> str | None:
        """记录记忆更新版本: 自增 version, prev_hash = 旧 content_hash。

        用于 _enrich_memory_async 更新 summary 时保持哈希链连续。
        若新摘要哈希与当前相同则跳过 (无变化)。
        """
        new_hash = compute_content_hash(new_summary)
        try:
            # 读取当前 version + content_hash
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
                return current_hash  # 无变化
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
            logger.debug("governance.version_update",
                         memory_id=memory_id, version=new_version, hash=new_hash[:12])
        except Exception as e:
            logger.warning("governance.record_update_failed",
                           memory_id=memory_id, error=str(e))
            if strict:
                raise
            return None
        return new_hash

    async def audit_context_consumption(self, response_id: str,
                                          memories: list[dict],
                                          auto_commit: bool = True) -> int:
        """记录一次响应注入了哪些记忆版本 (ContextNest §9 audit trace)。

        Args:
            response_id: 响应唯一标识 (建议 uuid4 或 conversation_log id)
            memories: retrieve_memories 返回的记忆列表, 每条含 id/score/source 等
        Returns:
            成功写入的审计条数
        """
        if not memories:
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
            except Exception as e:
                logger.debug("governance.audit_insert_failed",
                             memory_id=mem_id, error=str(e))
        if inserted and auto_commit:
            await self._conn.commit()
        return inserted

    async def verify_hash_chain(self, memory_id: int) -> dict:
        """验证某条记忆的哈希链完整性 (tamper-evident check)。

        Returns:
            dict: {valid: bool, broken_at_version: int|None, versions: int, detail: str}
        """
        try:
            cursor = await self._conn.execute(
                "SELECT version, content_hash, prev_hash, summary_snapshot "
                "FROM memory_versions WHERE memory_id=? ORDER BY version",
                (memory_id,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return {"valid": False, "broken_at_version": None,
                        "versions": 0, "detail": "no version history"}
            prev_hash = ""
            for row in rows:
                version, content_hash, stored_prev, snapshot = row
                if version == 1:
                    if stored_prev != "":
                        return {"valid": False, "broken_at_version": 1,
                                "versions": len(rows),
                                "detail": f"v1 prev_hash should be empty, got {stored_prev}"}
                else:
                    if stored_prev != prev_hash:
                        return {"valid": False, "broken_at_version": version,
                                "versions": len(rows),
                                "detail": f"v{version} prev_hash mismatch: "
                                          f"expected {prev_hash[:12]}, got {stored_prev[:12]}"}
                # 验证 snapshot 哈希
                if snapshot:
                    recomputed = compute_content_hash(snapshot)
                    if recomputed != content_hash:
                        return {"valid": False, "broken_at_version": version,
                                "versions": len(rows),
                                "detail": f"v{version} snapshot hash mismatch "
                                          f"(tampered summary)"}
                prev_hash = content_hash
            return {"valid": True, "broken_at_version": None,
                    "versions": len(rows), "detail": "chain intact"}
        except (ImportError, OSError, RuntimeError, ValueError) as e:
            return {"valid": False, "broken_at_version": None,
                    "versions": 0, "detail": f"verify_error: {e}"}

        except Exception as e:
            logger.exception(".memory.context_governance.unexpected")
            return {"valid": False, "broken_at_version": None,
                    "versions": 0, "detail": f"verify_error: {e}"}

    async def reconstruct_context(self, response_id: str) -> list[dict]:
        """Point-in-time 重建某次响应注入的上下文 (ContextNest §9)。

        Returns:
            按 rank 排序的审计记录列表, 含 memory_id/content_hash/version/score/source
        """
        try:
            cursor = await self._conn.execute(
                "SELECT memory_id, content_hash, version, score, source, rank, retrieved_at "
                "FROM context_audit_log WHERE response_id=? ORDER BY rank",
                (response_id,),
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.warning("governance.reconstruct_failed",
                           response_id=response_id, error=str(e))
            return []

    @staticmethod
    def new_response_id() -> str:
        """生成响应唯一标识。"""
        return uuid.uuid4().hex
