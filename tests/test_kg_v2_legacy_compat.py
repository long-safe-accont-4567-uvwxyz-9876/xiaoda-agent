"""KG v2 legacy 分区读兼容测试。

隐私契约（2026-08-24 收紧）：线上库 kg_episodes 现存行挂 legacy group_id='default'
（分区键改为 <user_id>::<agent_id> 之前写入），该分区无归属信息、无法证明属于
任何当前用户。scoped 读（personal/group）对 legacy 分区 fail-closed：
(a) scoped personal/group 召回一律看不到 REL-legacy；
(b) 新分区之间仍严格隔离（不同 user / 不同 agent 不串），隔离断言不放松；
(c) 同一行经新旧两分区同时可达时，显式 admin/maintenance 路径（scope=None）
    下不双计；
(d) legacy 事实只允许 scope=None 的显式 admin/maintenance 路径访问。
"""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from db.database import DatabaseManager
from db.db_kg_v2 import KnowledgeDBV2
from memory.kg_search import LEGACY_PARTITION_KEY, KGSearchEngine
from memory.scope import Scope


async def make_engine(tmp_path, name):
    manager = DatabaseManager(tmp_path / name)
    await manager.init()
    db = KnowledgeDBV2(manager._conn)
    engine = KGSearchEngine(db=db, vector_store=None, conn=manager._conn)
    return manager, db, engine


async def insert_legacy_fact(db, rel_id="REL-legacy", fact="用户喜欢篮球"):
    await db.insert_episode(
        "EP-legacy", fact, "summary", 1000.0, time.time(),
        group_id=LEGACY_PARTITION_KEY,
    )
    await db.insert_relation_v2(
        rel_id, "用户", "喜欢", "篮球", fact, "EP-legacy", 1000.0,
    )
    return rel_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope",
    [
        Scope.personal(user_id="alice"),
        Scope.personal(user_id="bob"),
        Scope.personal(user_id="default"),
        Scope.group(user_id="alice", group_id="group-a"),
    ],
)
async def test_scoped_recall_never_reads_unattributed_legacy_default(
    tmp_path, scope,
):
    manager, db, engine = await make_engine(
        tmp_path, f"legacy_closed_{scope.user_id}_{scope.session_id}.db"
    )
    try:
        await insert_legacy_fact(db)
        results = await engine.search("喜欢篮球", top_k=10, scope=scope)
        assert "REL-legacy" not in {
            row["id"] for row in results if row.get("type") == "relation"
        }
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_unscoped_maintenance_can_read_legacy_default(tmp_path):
    manager, db, engine = await make_engine(tmp_path, "legacy_maintenance.db")
    try:
        await insert_legacy_fact(db)
        results = await engine.search("喜欢篮球", top_k=10, scope=None)
        assert "REL-legacy" in {
            row["id"] for row in results if row.get("type") == "relation"
        }
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_new_user_does_not_see_unattributed_legacy_facts(tmp_path):
    """(a) 隐私契约：新用户的新分区检索不到无法证明归属的 legacy default 事实。"""
    manager, db, engine = await make_engine(tmp_path, "legacy_newuser.db")
    try:
        await insert_legacy_fact(db)
        results = await engine.search(
            "喜欢篮球", top_k=10,
            scope=Scope(user_id="carol", agent_id="xiaoda"),
        )
        assert "REL-legacy" not in {
            r["id"] for r in results if r.get("type") == "relation"
        }
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_qq_group_scope_rejects_unattributed_legacy_facts(tmp_path):
    """QQ 群不能读取无法证明属于本群的 legacy default 事实。"""
    manager, db, engine = await make_engine(tmp_path, "legacy_group.db")
    try:
        await insert_legacy_fact(db)
        scope = Scope.group(user_id="default", group_id="123")
        results = await engine.search("喜欢篮球", top_k=10, scope=scope)
        assert "REL-legacy" not in {
            r["id"] for r in results if r.get("type") == "relation"
        }
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_new_partitions_stay_isolated_from_each_other(tmp_path):
    """(b) legacy fail-closed 不放松新分区隔离：不同 user / 不同 agent 互不串，
    且谁也看不到无归属的 REL-legacy。"""
    manager, db, engine = await make_engine(tmp_path, "legacy_isolation.db")
    try:
        await insert_legacy_fact(db)
        # alice 只在 xiaoda 分区有新事实
        await db.insert_episode(
            "EP-alice", "Alice 喜欢网球", "summary", 2000.0, time.time(),
            group_id="alice::xiaoda",
        )
        await db.insert_relation_v2(
            "REL-alice", "Alice", "喜欢", "网球", "Alice 喜欢网球",
            "EP-alice", 2000.0,
        )

        # 查询词同时命中两条 fact，验证精确的集合差
        xiaoli_results = await engine.search(
            "喜欢", top_k=10, scope=Scope(user_id="alice", agent_id="xiaoli"),
        )
        xiaoli_ids = {r["id"] for r in xiaoli_results if r.get("type") == "relation"}
        assert "REL-legacy" not in xiaoli_ids   # legacy 无归属，fail-closed
        assert "REL-alice" not in xiaoli_ids    # 其他 agent 的新分区不可见

        bob_ids = {
            r["id"]
            for r in await engine.search(
                "喜欢", top_k=10, scope=Scope(user_id="bob", agent_id="xiaoda"),
            )
            if r.get("type") == "relation"
        }
        assert bob_ids == set()                 # bob: 既无自有事实也看不到 legacy/alice

        alice_ids = {
            r["id"]
            for r in await engine.search(
                "喜欢", top_k=10, scope=Scope(user_id="alice", agent_id="xiaoda"),
            )
            if r.get("type") == "relation"
        }
        assert alice_ids == {"REL-alice"}       # alice: 只见自有新事实，不见 legacy
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_no_double_count_when_row_reachable_via_both_partitions(tmp_path):
    """(c) 关系同时挂在 legacy 与新分区的 episode 下时只出现一次（admin 路径）。"""
    manager, db, engine = await make_engine(tmp_path, "legacy_dedup.db")
    try:
        await insert_legacy_fact(db, rel_id="REL-shared", fact="共同事实测试")
        await db.insert_episode(
            "EP-new", "共同事实测试", "summary", 2000.0, time.time(),
            group_id="alice::xiaoda",
        )
        # 同一关系追加新分区 episode 引用 → 新旧两个分区都能 JOIN 到该行
        await db.append_episode_ref("REL-shared", "EP-new")

        results = await engine.search(
            "共同事实测试", top_k=10, scope=None,
        )
        ids = [r["id"] for r in results if r.get("type") == "relation"]
        assert ids.count("REL-shared") == 1
    finally:
        await manager.close()


# ── 实体召回 ──────────────────────────────────────────────────


def _mock_vector_store(entity_hits):
    store = MagicMock()
    store.search_kg_entities = AsyncMock(return_value=entity_hits)
    store.search_kg_relations = AsyncMock(return_value=[])
    return store


async def insert_partition_entity(db, name, summary, group_id, ep_id, rel_id):
    rowid = await db.insert_entity_v2(f"ENT-{name}", name, "概念", [], summary)
    await db.insert_episode(ep_id, summary, "summary", 1000.0, time.time(),
                            group_id=group_id)
    await db.insert_relation_v2(rel_id, name, "属于", "运动", f"{name}{summary}",
                                ep_id, 1000.0)
    return int(rowid)


@pytest.mark.asyncio
async def test_scoped_semantic_returns_entities_within_compat_scope(tmp_path):
    """(d) scoped 语义通道只返回精确分区内的实体；legacy 实体 fail-closed。"""
    manager, db, _ = await make_engine(tmp_path, "entity_semantic.db")
    try:
        legacy_rowid = await insert_partition_entity(
            db, "篮球", "团队运动", LEGACY_PARTITION_KEY, "EP-lg", "REL-lg"
        )
        alice_rowid = await insert_partition_entity(
            db, "网球", "单人运动", "alice::xiaoda", "EP-alice", "REL-alice"
        )
        engine = KGSearchEngine(
            db=db,
            vector_store=_mock_vector_store(
                [(legacy_rowid, 0.1), (alice_rowid, 0.2)]
            ),
            conn=manager._conn,
        )

        # 直接打语义通道，避免其他通道的噪音干扰通道级断言
        carol = await engine._semantic_search(
            "任意查询", k=10, scope_key="carol::xiaoda",
        )
        carol_entities = {r["id"] for r in carol if r.get("type") == "entity"}
        # legacy 实体无归属，fail-closed；alice 私有分区实体对 carol 同样不可见
        assert carol_entities == set()

        alice = await engine._semantic_search(
            "任意查询", k=10, scope_key="alice::xiaoda",
        )
        alice_entities = {r["id"] for r in alice if r.get("type") == "entity"}
        assert alice_entities == {"ENT-网球"}

        unscoped = await engine._semantic_search("任意查询", k=10, scope_key=None)
        unscoped_entities = {r["id"] for r in unscoped if r.get("type") == "entity"}
        # 显式 admin/maintenance 路径（scope=None）才可见 legacy 实体
        assert unscoped_entities == {"ENT-篮球", "ENT-网球"}
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_scoped_fulltext_returns_entities_within_compat_scope(tmp_path):
    """(d) FTS 通道 scoped 实体召回：仅精确分区内命中，legacy fail-closed。"""
    manager, db, engine = await make_engine(tmp_path, "entity_fts.db")
    try:
        await insert_partition_entity(
            db, "篮球", "团队 运动", LEGACY_PARTITION_KEY, "EP-lg", "REL-lg"
        )
        await insert_partition_entity(
            db, "网球", "单人 运动", "bob::xiaoda", "EP-bob", "REL-bob"
        )

        # 关键词"运动"同时命中两个实体的 name+summary
        carol = await engine._fulltext_search("运动", k=5, scope_key="carol::xiaoda")
        carol_entities = {r["id"] for r in carol if r.get("type") == "entity"}
        assert carol_entities == set()

        bob = await engine._fulltext_search("运动", k=5, scope_key="bob::xiaoda")
        bob_entities = {r["id"] for r in bob if r.get("type") == "entity"}
        # bob: 只见自有分区实体；legacy 篮球 fail-closed 不可见
        assert bob_entities == {"ENT-网球"}

        unscoped = await engine._fulltext_search("运动", k=5)
        assert {r["id"] for r in unscoped if r.get("type") == "entity"} == {
            "ENT-篮球", "ENT-网球",
        }
    finally:
        await manager.close()
