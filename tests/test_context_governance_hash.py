"""context_governance 哈希口径测试（数据库小任务B-4）。

根因：>500 字符记忆的 content_hash 用完整 summary 计算，但
memory_versions.summary_snapshot 截断为 500 字符存储。verify_hash_chain 用
snapshot 重算哈希与存储哈希比对 → 长文本必然 mismatch（假阳性）。

修复契约：
1. 引入 HASH_ALGO_VERSION 常量；哈希对「实际存储快照」（summary[:500]）计算，
   写入与验证使用同一口径。
2. 存量（旧口径）不一致记录提供一次性迁移函数 migrate_legacy_hashes，
   由调用方显式触发，不在启动时自动执行。
3. 长文本完整性：>500 字符记忆写入后 verify_hash_chain 必须 valid。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.context_governance import (
    HASH_ALGO_VERSION,
    ContextGovernance,
    compute_content_hash,
)


async def _setup_db(tmp_path):
    import aiosqlite

    db_path = str(tmp_path / "test_governance_hash.db")
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript("""
        CREATE TABLE episodic_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            summary TEXT NOT NULL,
            importance REAL DEFAULT 0.5,
            emotion_label TEXT DEFAULT '',
            session_id TEXT DEFAULT 'user',
            embedding_id INTEGER DEFAULT -1,
            source TEXT DEFAULT 'user',
            access_count INTEGER DEFAULT 0,
            distilled INTEGER DEFAULT 0,
            content_hash TEXT DEFAULT '',
            version INTEGER DEFAULT 1
        );
        CREATE TABLE memory_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            prev_hash TEXT DEFAULT '',
            summary_snapshot TEXT DEFAULT '',
            created_at REAL NOT NULL
        );
        CREATE TABLE context_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            response_id TEXT NOT NULL,
            memory_id INTEGER NOT NULL,
            content_hash TEXT DEFAULT '',
            version INTEGER DEFAULT 1,
            score REAL DEFAULT 0.0,
            source TEXT DEFAULT '',
            rank INTEGER DEFAULT 0,
            retrieved_at REAL NOT NULL
        );
    """)
    await conn.commit()
    return conn


LONG_SUMMARY = ("用户喜欢喝美式咖啡并且每次都加双份浓缩。" * 60)  # >500 字符
assert len(LONG_SUMMARY) > 500


def test_hash_algo_version_constant_exists():
    """HASH_ALGO_VERSION 常量存在且为非空字符串/整数。"""
    assert HASH_ALGO_VERSION
    assert isinstance(HASH_ALGO_VERSION, (str, int))


def test_compute_content_hash_matches_stored_snapshot_semantics():
    """compute_content_hash 对实际会存储的快照口径计算：
    超过快照上限时，完整文本与截断快照的哈希必须一致（同一存储事实）。
    """
    snapshot = LONG_SUMMARY[:500]
    assert compute_content_hash(LONG_SUMMARY) == compute_content_hash(snapshot)


async def test_long_text_initial_version_chain_valid(tmp_path):
    """>500 字符记忆写入后哈希链验证必须 valid（此前假阳性）。"""
    conn = await _setup_db(tmp_path)
    gov = ContextGovernance(conn)
    cur = await conn.execute(
        "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
        (time.time(), LONG_SUMMARY),
    )
    mid = cur.lastrowid
    result = await gov.record_initial_version(mid, LONG_SUMMARY)
    stored = await (
        await conn.execute(
            "SELECT content_hash, version FROM episodic_memories WHERE id=?", (mid,)
        )
    ).fetchone()
    assert stored["content_hash"] == result

    check = await gov.verify_hash_chain(mid)
    assert check["valid"], f"长文本哈希链应 valid: {check['detail']}"

    snap_row = await (
        await conn.execute(
            "SELECT summary_snapshot FROM memory_versions "
            "WHERE memory_id=? AND version=1",
            (mid,),
        )
    ).fetchone()
    assert len(snap_row["summary_snapshot"]) <= 500
    # 快照与哈希同源：重算快照哈希等于存储哈希
    assert compute_content_hash(snap_row["summary_snapshot"]) == result
    await conn.close()


async def test_long_text_update_chain_valid(tmp_path):
    """长文本更新 v1→v2 后链仍然连续且 valid。

    注：content_hash 绑定实际存储快照（前 500 字符），因此更新必须落在
    快照窗口内才会产生新版本——窗口外的尾部追加对存储事实无影响，
    不产生版本行是口径一致的正确行为。
    """
    conn = await _setup_db(tmp_path)
    gov = ContextGovernance(conn)
    cur = await conn.execute(
        "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
        (time.time(), LONG_SUMMARY),
    )
    mid = cur.lastrowid
    await gov.record_initial_version(mid, LONG_SUMMARY)

    # 变更落在快照窗口内（前缀插入），存储口径可感知
    longer_v2 = "更正：用户改喝双份冰美式。" + LONG_SUMMARY
    new_hash = await gov.record_version_update(mid, longer_v2)
    assert new_hash is not None
    check = await gov.verify_hash_chain(mid)
    assert check["valid"], f"更新后长文本链应 valid: {check['detail']}"
    assert check["versions"] == 2
    await conn.close()


async def test_migrate_legacy_hashes_repairs_old_full_text_hashes(tmp_path):
    """存量迁移函数：旧口径（按完整 summary 计算的哈希）被识别并修复为新口径；
    函数可显式调用（不在启动自动跑），幂等。
    """
    from memory.context_governance import migrate_legacy_hashes

    conn = await _setup_db(tmp_path)
    gov = ContextGovernance(conn)

    cur = await conn.execute(
        "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
        (time.time(), LONG_SUMMARY),
    )
    mid = cur.lastrowid
    legacy_hash = __import__("hashlib").sha256(
        LONG_SUMMARY.encode("utf-8")
    ).hexdigest()
    await conn.execute(
        "UPDATE episodic_memories SET content_hash=?, version=1 WHERE id=?",
        (legacy_hash, mid),
    )
    await conn.execute(
        "INSERT INTO memory_versions "
        "(memory_id, version, content_hash, prev_hash, summary_snapshot, created_at) "
        "VALUES (?, 1, ?, '', ?, ?)",
        (mid, legacy_hash, LONG_SUMMARY[:500], time.time()),
    )
    await conn.commit()

    # 迁移前：假阳性（invalid）
    before = await gov.verify_hash_chain(mid)
    assert not before["valid"], "前置条件：旧口径长文本应报 invalid"

    repaired = await migrate_legacy_hashes(conn)
    assert repaired >= 1
    row = await (
        await conn.execute(
            "SELECT content_hash FROM episodic_memories WHERE id=?", (mid,)
        )
    ).fetchone()
    assert row["content_hash"] == compute_content_hash(LONG_SUMMARY[:500])

    after = await gov.verify_hash_chain(mid)
    assert after["valid"], f"迁移后应 valid: {after['detail']}"

    # 幂等：再跑一遍不再有变更
    assert await migrate_legacy_hashes(conn) == 0
    await conn.close()


async def test_migrate_keeps_short_text_untouched(tmp_path):
    """短文本（<=快照上限）新旧口径哈希相同，迁移不应改动它们。"""
    from memory.context_governance import migrate_legacy_hashes

    conn = await _setup_db(tmp_path)
    short = "用户喜欢美式咖啡"
    cur = await conn.execute(
        "INSERT INTO episodic_memories (timestamp, summary) VALUES (?, ?)",
        (time.time(), short),
    )
    mid = cur.lastrowid
    gov = ContextGovernance(conn)
    await gov.record_initial_version(mid, short)

    assert await migrate_legacy_hashes(conn) == 0
    row = await (
        await conn.execute(
            "SELECT content_hash FROM episodic_memories WHERE id=?", (mid,)
        )
    ).fetchone()
    assert row["content_hash"] == compute_content_hash(short)
    await conn.close()
