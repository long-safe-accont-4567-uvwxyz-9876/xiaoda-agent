"""LegacyMigrationMixin — v1-v27 历史 schema 迁移函数。

自 db/database.py 拆分（上帝文件 Phase 1）：函数体逐字节搬移，仅缩进调整。
结构契约见 tests/test_db_migration_refactor.py；实例白盒调用
（tests/test_bitemporal_memory.py 等）依赖 mixin 继承保持方法为实例属性。
"""
from __future__ import annotations

import sys
import time
from typing import ClassVar, Any

from loguru import logger

from . import db_workflow


class LegacyMigrationMixin:
    async def _setup_migration_state(self) -> None:
        # 逐条执行 DDL，避免 executescript() 在 vfat 上的隐式 commit 问题
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            )
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS migration_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                dirty INTEGER NOT NULL DEFAULT 0,
                last_version INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            )
        """)
        await self._conn.execute("""
            INSERT OR IGNORE INTO migration_state (id, dirty, last_version, last_error)
            VALUES (1, 0, 0, '')
        """)
        await self._conn.commit()

    async def _recover_dirty_state(self) -> None:
        # Dirty state 检测：上次迁移未完成
        state_row = await self._conn.execute_fetchall(
            "SELECT dirty, last_version, last_error FROM migration_state WHERE id = 1"
        )
        if state_row and state_row[0][0] == 1:
            last_ver = state_row[0][1]
            last_err = state_row[0][2]
            logger.warning(
                f"⚠️ 数据库处于 dirty 状态！上次迁移 v{last_ver} 未完成：{last_err}"
            )
            # 自动修复：尝试重跑失败迁移（所有迁移均有幂等守卫，重跑安全）
            logger.info("database.dirty_auto_retry", version=last_ver)
            try:
                # 清除 dirty 标记，让后续正常迁移流程接管
                await self._conn.execute(
                    "UPDATE migration_state SET dirty = 0, last_version = 0, last_error = '' WHERE id = 1"
                )
                await self._conn.commit()
                logger.info(
                    "database.dirty_cleared_auto",
                    msg=f"已自动清除 v{last_ver} 的 dirty 标记，将重新执行迁移"
                )
            except (OSError, RuntimeError) as e:
                logger.error("database.dirty_auto_retry_failed", error=str(e))
                logger.critical(
                    f"⚠️ 自动修复失败！请手动修复：\n"  # noqa: F541
                    f"  1. python -m db.repair_migration --mark-clean\n"  # noqa: F541
                    f"  2. 或删除 agent.db 重新初始化（会丢失历史数据）\n"  # noqa: F541
                )
                try:
                    await self._conn.close()
                except (OSError, RuntimeError):
                    logger.debug("legacy_migrations.close_before_exit_failed", exc_info=True)
                sys.exit(1)

    async def _current_schema_version(self) -> int:
        row = await self._conn.execute_fetchall("SELECT MAX(version) FROM schema_version")
        return row[0][0] if row and row[0][0] is not None else 0

    async def _check_migration_integrity(self, current: int) -> int:
        # 防御性校验：检查关键列是否真正存在（防止 schema_version 被标记但列未实际添加）
        if current >= 10:
            epi_cols = {r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")}
            # v15 关键列缺失 → 回退 schema_version 到 v9，触发重新迁移
            critical_v15_cols = {"phase", "difficulty", "stability"}
            if current >= 15 and not critical_v15_cols.issubset(epi_cols):
                logger.warning("database.migration_integrity_check_failed",
                               msg=f"schema_version={current} 但缺失关键列 {critical_v15_cols - epi_cols}，回退到 v9 重新迁移")
                # 删除 v10+ 的 schema_version 记录
                await self._conn.execute("DELETE FROM schema_version WHERE version >= 10")
                await self._conn.commit()
                current = 9
            # v18 关键列缺失
            elif current >= 18 and "distill_status" not in epi_cols:
                logger.warning("database.migration_integrity_check_failed",
                               msg="schema_version>=18 但缺失 distill_status 列，回退到 v17 重新迁移")
                await self._conn.execute("DELETE FROM schema_version WHERE version >= 18")
                await self._conn.commit()
                current = 17
        return current

    def _migration_entries(self) -> list:
        """返回 (version, description, migrate_fn) 三元组列表，按 version 升序。"""
        return [
            (1, "temporal_knowledge_graph", self._migrate_v1),
            (2, "conversation_logs.session_id", self._migrate_v2),
            (3, "fts5_index+consolidation_candidates", self._migrate_v3),
            (4, "episodic_memories.source", self._migrate_v4),
            (5, "knowledge_entities_fts_backfill", self._migrate_v5),
            (6, "episodic_memories.access_count", self._migrate_v6),
            (7, "episodic_memories.session_id+embedding_id", self._migrate_v7),
            (8, "episodic_memories.rag_status+rag_synced_at+doc_id", self._migrate_v8),
            (9, "memory_summaries+episodic_memories.distilled", self._migrate_v9),
            (10, "episodic_memories.entities+event_type+metadata_json", self._migrate_v10),
            (11, "memory_recall_notes", self._migrate_v11),
            (12, "episodic_memories.content_hash+version+memory_versions+context_audit_log", self._migrate_v12),
            (13, "mem0_spec+bitemporal_facts_preferences+provenance+memory_edges", self._migrate_v13),
            (14, "v06_cognitive_tables+kg_v2_tables", self._migrate_v14),
            (15, "fsrs_dsr_columns", self._migrate_v15),
            (16, "created_at_columns", self._migrate_v16),
            (17, "greeting_schedules_reminder_type", self._migrate_v17),
            (18, "distill_status_column", self._migrate_v18),
            (19, "episodic_memories.updated_at+touch_trigger", self._migrate_v19),
            (20, "greeting_schedules.user_id_column", self._migrate_v20),
            (21, "knowledge_entities_fts_trigger_drop", self._migrate_v21),
            (22, "fts_single_char_rebuild", self._migrate_v22),
            (23, "bitemporal_profile_fields", self._migrate_v23),
            (24, "profile_event_idempotency", self._migrate_v24),
            (25, "installed_models_table", self._migrate_v25),
            (26, "conversation_logs.request_context_json", self._migrate_v26),
            (27, "workflow_v2_tables", self._migrate_v27),
        ]

    async def _run_migrations(self) -> None:
        """按 version 顺序执行所有数据库迁移。每个迁移独立事务，失败时 fail-fast 阻止启动。"""
        await self._setup_migration_state()
        await self._recover_dirty_state()
        current = await self._current_schema_version()
        current = await self._check_migration_integrity(current)
        for version, desc, migrate_fn in self._migration_entries():
            if current < version:
                await self._apply_migration(version, desc, migrate_fn)
        await self._conn.commit()

    async def _apply_migration(self, version: int, description: str, migrate_fn: Any) -> None:
        """执行单个迁移：标记 dirty → migrate_fn → INSERT schema_version → commit → 清除 dirty。

        失败时不杀进程，下次启动自动重试（dirty自动修复机制）。
        含 SQLITE_BUSY 重试（Windows杀软锁文件常见）。
        注意：不使用显式 BEGIN TRANSACTION，因为迁移函数内部的 executescript()
        会隐式提交当前事务，在 vfat 上会导致死锁/挂起。
        """
        # 确保 migration_state 表存在（防御 vfat 上 executescript 静默失败）
        try:
            await self._conn.execute("SELECT 1 FROM migration_state LIMIT 1")
        except (ImportError, OSError, RuntimeError, ValueError):
            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS migration_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    dirty INTEGER NOT NULL DEFAULT 0,
                    last_version INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
            """)
            await self._conn.execute("""
                INSERT OR IGNORE INTO migration_state (id, dirty, last_version, last_error)
                VALUES (1, 0, 0, '')
            """)
            await self._conn.commit()

        except Exception:
            logger.exception(".db.legacy_migrations._apply_migration_unexpected")
            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS migration_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    dirty INTEGER NOT NULL DEFAULT 0,
                    last_version INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
            """)
            await self._conn.execute("""
                INSERT OR IGNORE INTO migration_state (id, dirty, last_version, last_error)
                VALUES (1, 0, 0, '')
            """)
            await self._conn.commit()

        # 标记 dirty（独立事务，确保迁移失败后 dirty 状态持久化）
        await self._conn.execute(
            "UPDATE migration_state SET dirty = 1, last_version = ?, last_error = '' WHERE id = 1",
            (version,),
        )
        await self._conn.commit()

        _max_retries = 3
        for attempt in range(1, _max_retries + 1):
            try:
                # 不使用 BEGIN TRANSACTION：executescript() 会隐式提交，
                # 在 vfat (DELETE journal_mode) 上显式事务 + 隐式提交会导致挂起
                await migrate_fn()
                await self._conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (version, time.time()),
                )
                await self._conn.commit()
                # 迁移成功：清除 dirty
                await self._conn.execute(
                    "UPDATE migration_state SET dirty = 0, last_version = ?, last_error = '' WHERE id = 1",
                    (version,),
                )
                await self._conn.commit()
                logger.info("database.migration_v{}", version, desc=description)
                return  # 成功，退出重试循环
            except Exception as e:
                err_msg = str(e)
                is_busy = "locked" in err_msg.lower() or "busy" in err_msg.lower()
                if is_busy and attempt < _max_retries:
                    # SQLITE_BUSY: Windows杀软/Defender锁文件，等一会重试
                    import asyncio
                    wait = attempt * 2
                    logger.warning(
                        f"database.migration_v{version}_busy_retry",
                        attempt=attempt, wait_sec=wait, error=err_msg[:100]
                    )
                    await asyncio.sleep(wait)
                    continue
                # 非BUSY或重试耗尽
                # 不需要 ROLLBACK：未使用显式事务，executescript 自行管理原子性
                # 记录错误到 dirty state（独立事务）
                try:
                    await self._conn.execute(
                        "UPDATE migration_state SET dirty = 1, last_version = ?, last_error = ? WHERE id = 1",
                        (version, err_msg[:500]),
                    )
                    await self._conn.commit()
                except (OSError, RuntimeError):
                    logger.warning("database.migration_dirty_record_error", exc_info=True)
                logger.error(
                    f"❌ 数据库迁移 v{version} 失败: {err_msg}\n"
                    f"已标记 dirty 状态，下次启动将自动重试。\n"
                    f"如持续失败可手动修复：\n"
                    f"  1. python -m db.repair_migration --mark-clean\n"
                    f"  2. python -m db.repair_migration --rollback {version}\n"
                )
                raise RuntimeError(
                    f"数据库迁移 v{version} 失败，已标记 dirty，初始化已中止: {err_msg}"
                ) from e

    async def _migrate_v1(self) -> None:
        """v1: knowledge_relations 新增时间字段（valid_from/valid_to/confidence）。"""
        await self._ensure_columns("knowledge_relations", {
            "valid_from": "valid_from REAL DEFAULT 0",
            "valid_to": "valid_to REAL DEFAULT 0",
            "confidence": "confidence REAL DEFAULT 1.0",
        })

    async def _migrate_v2(self) -> None:
        """v2: conversation_logs 新增 session_id 列。"""
        await self._ensure_columns("conversation_logs", {
            "session_id": "session_id TEXT DEFAULT ''",
        })

    async def _migrate_v3(self) -> None:
        """v3: 创建 FTS5 虚拟表 + 回填已有记忆到 FTS 索引 + 创建审计表。"""
        # 创建 FTS5 虚拟表
        await self._conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory_fts USING fts5( id UNINDEXED, summary_index )""")
        # 回填已有记忆数据到 FTS 索引
        rows = await self._conn.execute_fetchall("SELECT id, summary FROM episodic_memories")
        for row in rows:
            from db.fts_utils import _tokenize_for_fts
            tokenized = _tokenize_for_fts(row[1])
            if tokenized.strip():
                await self._conn.execute(
                    "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                    (row[0], tokenized),
                )
        # 创建审计表
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS consolidation_candidates ( id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, source TEXT NOT NULL DEFAULT 'rule', kind TEXT NOT NULL DEFAULT 'fact', summary TEXT NOT NULL, confidence REAL DEFAULT 0.5, importance REAL DEFAULT 0.5, status TEXT NOT NULL DEFAULT 'pending', target_memory_id INTEGER DEFAULT -1, metadata_json TEXT DEFAULT '{}', created_at REAL NOT NULL )""")
        logger.info("database.migration_v3_backfill", rows=len(rows))

    async def _migrate_v4(self) -> None:
        """v4: episodic_memories 新增 source 列。"""
        await self.memory.migrate_add_source_column()

    async def _migrate_v5(self) -> None:
        """v5: 回填 knowledge_entities 数据到 FTS 索引。"""
        cursor = await self._conn.execute("SELECT id, name FROM knowledge_entities")
        rows = await cursor.fetchall()
        from db.fts_utils import _tokenize_for_fts
        for row in rows:
            name_tokenized = _tokenize_for_fts(row["name"]) if row["name"] else ""
            await self._conn.execute(
                "INSERT OR IGNORE INTO knowledge_entities_fts(id, name_index) VALUES (?, ?)",
                (row["id"], name_tokenized),
            )
        logger.info("database.migration_v5", desc="knowledge_entities_fts_backfill", rows=len(rows))

    async def _migrate_v6(self) -> None:
        """v6: episodic_memories 新增 access_count 列。"""
        await self._ensure_columns("episodic_memories", {
            "access_count": "access_count INTEGER DEFAULT 0",
        })

    async def _migrate_v7(self) -> None:
        """v7: 修复旧版 episodic_memories 缺少 session_id 和 embedding_id 列。

        新安装时 CREATE TABLE 已包含这些列，需先检查再添加。
        """
        await self._ensure_columns("episodic_memories", {
            "session_id": "session_id TEXT DEFAULT 'user'",
            "embedding_id": "embedding_id INTEGER DEFAULT -1",
        })

    async def _migrate_v8(self) -> None:
        """v8: episodic_memories 新增 RAG 同步相关列（rag_status/rag_synced_at/doc_id）。"""
        await self._ensure_columns("episodic_memories", {
            "rag_status": "rag_status TEXT DEFAULT 'pending'",
            "rag_synced_at": "rag_synced_at REAL DEFAULT 0",
            "doc_id": "doc_id TEXT DEFAULT ''",
        })

    async def _migrate_v9(self) -> None:
        """v9: P3 记忆蒸馏：新增 memory_summaries 表 + episodic_memories.distilled 列。"""
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_text TEXT NOT NULL,
                created_at REAL NOT NULL,
                memory_count INTEGER DEFAULT 0
            )
        """)
        await self._ensure_columns("episodic_memories", {
            "distilled": "distilled INTEGER DEFAULT 0",
        })

    async def _migrate_v10(self) -> None:
        """v10: 记忆结构化提取：新增 entities/event_type/metadata_json 列。"""
        await self._ensure_columns("episodic_memories", {
            "entities": "entities TEXT DEFAULT ''",
            "event_type": "event_type TEXT DEFAULT ''",
            "metadata_json": "metadata_json TEXT DEFAULT '{}'",
        })

    async def _migrate_v11(self) -> None:
        """v11: 主动检索 B：定时回忆笔记表。

        与 memory_summaries（蒸馏压缩，控制上下文长度）语义不同：
        memory_recall_notes 是按时间窗口 + 重要性筛选后整理出的"回忆笔记"，
        用于主动检索时给 LLM 提供"最近发生了什么"的高密度上下文。
        """
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_recall_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                window_start REAL NOT NULL,
                window_end REAL NOT NULL,
                min_importance REAL DEFAULT 0.6,
                source_memory_ids TEXT DEFAULT '',
                memory_count INTEGER DEFAULT 0,
                title TEXT DEFAULT '',
                summary TEXT NOT NULL,
                tags TEXT DEFAULT ''
            )
        """)

    async def _migrate_v12(self) -> None:
        """v12: ContextNest 上下文治理 — 记忆哈希版本链 + 上下文审计追踪。

        - episodic_memories 新增 content_hash (SHA-256 of summary) + version 列
        - memory_versions 表: 哈希链 (prev_hash → content_hash), tamper-evident
        - context_audit_log 表: 记录每次响应注入了哪些记忆版本, 支持 point-in-time 重建
        """
        await self._ensure_columns("episodic_memories", {
            "content_hash": "content_hash TEXT DEFAULT ''",
            "version": "version INTEGER DEFAULT 1",
        })
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                prev_hash TEXT DEFAULT '',
                summary_snapshot TEXT DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY (memory_id) REFERENCES episodic_memories(id)
            )
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS context_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                response_id TEXT NOT NULL,
                memory_id INTEGER NOT NULL,
                content_hash TEXT DEFAULT '',
                version INTEGER DEFAULT 1,
                score REAL DEFAULT 0.0,
                source TEXT DEFAULT '',
                rank INTEGER DEFAULT 0,
                retrieved_at REAL NOT NULL
            )
        """)
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_response ON context_audit_log(response_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_memory ON context_audit_log(memory_id)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mv_memory ON memory_versions(memory_id, version)"
        )
        # 回填已有记忆的哈希链 (Bug 9 fix): 为 v12 之前创建的行计算 content_hash
        # 并写入 memory_versions v1 记录, 使 Tamper-Evident 校验对历史数据生效
        from memory.context_governance import compute_content_hash
        cursor = await self._conn.execute(
            "SELECT id, summary FROM episodic_memories "
            "WHERE content_hash = '' OR content_hash IS NULL ORDER BY id"
        )
        backfill_rows = await cursor.fetchall()
        now = time.time()
        backfilled = 0
        for row in backfill_rows:
            mem_id, summary = row[0], row[1]
            content_hash = compute_content_hash(summary)
            await self._conn.execute(
                "UPDATE episodic_memories SET content_hash=?, version=1 WHERE id=?",
                (content_hash, mem_id),
            )
            # 幂等: 仅当 memory_versions 不存在该 memory 的 v1 记录时才插入
            exists_cur = await self._conn.execute(
                "SELECT 1 FROM memory_versions WHERE memory_id=? AND version=1",
                (mem_id,),
            )
            if not await exists_cur.fetchone():
                await self._conn.execute(
                    "INSERT INTO memory_versions "
                    "(memory_id, version, content_hash, prev_hash, summary_snapshot, created_at) "
                    "VALUES (?, 1, ?, '', ?, ?)",
                    (mem_id, content_hash, summary[:500], now),
                )
            backfilled += 1
        if backfilled:
            logger.info("database.migration_v12_backfill", rows=backfilled)

    async def _migrate_v13(self) -> None:
        """v13: mem0 SPEC 优化 + 双时态事实/偏好/来源映射/类型化记忆边。

        Part 1 (mem0 SPEC):
        - episodic_memories: user_id/agent_id/is_raw（ALTER TABLE 加列，SQLite 不锁表）
        - memory_entities: 实体存储（与 KG 的 knowledge_entities 职责分离）
        - memory_entities_fts: 实体名称全文索引 + 3 触发器
        - entity_memory_links: 实体↔记忆反向链接
        - idx_episodic_scope: scope 复合索引
        - 回填现有记忆的 user_id/agent_id/is_raw 默认值

        Part 2 (bitemporal):
        - memory_facts: 双时态事实（valid_from/valid_to + learned_at/expired_at）
        - memory_fact_sources: 事实来源映射
        - memory_preferences: 偏好模式（含极性/显式度）
        - memory_preference_sources: 偏好来源映射
        - memory_edges: 类型化记忆边（supersedes/supports/similar/bridge）
        """
        # 1. episodic_memories 新增 3 列（幂等：先检查列是否存在）
        await self._ensure_columns("episodic_memories", {
            "user_id": "user_id TEXT DEFAULT 'default'",
            "agent_id": "agent_id TEXT DEFAULT 'xiaoda'",
            "is_raw": "is_raw INTEGER DEFAULT 0",
        })

        # 2. 回填现有记忆的默认值（确保旧数据有 scope 字段）
        await self._conn.execute(
            "UPDATE episodic_memories SET user_id='default' WHERE user_id IS NULL OR user_id=''"
        )
        await self._conn.execute(
            "UPDATE episodic_memories SET agent_id='xiaoda' WHERE agent_id IS NULL OR agent_id=''"
        )
        await self._conn.execute(
            "UPDATE episodic_memories SET is_raw=0 WHERE is_raw IS NULL"
        )

        # 3. 新建 memory_entities 表
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS memory_entities ( id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, entity_type TEXT DEFAULT 'TOPIC', kind TEXT DEFAULT '', observations TEXT DEFAULT '[]', memory_count INTEGER DEFAULT 0, first_seen REAL NOT NULL, last_seen REAL NOT NULL, metadata_json TEXT DEFAULT '{}', UNIQUE(name, entity_type) )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_entities_name ON memory_entities(name)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_entities_type ON memory_entities(entity_type)""")

        # 4. 新建 memory_entities_fts 虚拟表 + 触发器
        await self._conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS memory_entities_fts USING fts5( id UNINDEXED, name_index )""")
        await self._conn.execute("""CREATE TRIGGER IF NOT EXISTS memory_entities_fts_ai AFTER INSERT ON memory_entities BEGIN INSERT INTO memory_entities_fts(id, name_index) VALUES (new.id, new.name); END""")
        await self._conn.execute("""CREATE TRIGGER IF NOT EXISTS memory_entities_fts_ad AFTER DELETE ON memory_entities BEGIN INSERT INTO memory_entities_fts(memory_entities_fts, id, name_index) VALUES ('delete', old.id, old.name); END""")
        await self._conn.execute("""CREATE TRIGGER IF NOT EXISTS memory_entities_fts_au AFTER UPDATE ON memory_entities BEGIN INSERT INTO memory_entities_fts(memory_entities_fts, id, name_index) VALUES ('delete', old.id, old.name); INSERT INTO memory_entities_fts(id, name_index) VALUES (new.id, new.name); END""")

        # 5. 新建 entity_memory_links 表
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS entity_memory_links ( id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id INTEGER NOT NULL, memory_id INTEGER NOT NULL, confidence REAL DEFAULT 1.0, created_at REAL NOT NULL, FOREIGN KEY (entity_id) REFERENCES memory_entities(id) ON DELETE CASCADE, FOREIGN KEY (memory_id) REFERENCES episodic_memories(id) ON DELETE CASCADE, UNIQUE(entity_id, memory_id) )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_eml_entity ON entity_memory_links(entity_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_eml_memory ON entity_memory_links(memory_id)""")

        # 6. scope 复合索引
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_episodic_scope "
            "ON episodic_memories(user_id, agent_id, is_raw, timestamp DESC)"
        )

        logger.info("database.migration_v13_mem0_spec_done")

        # ── Part 2: bitemporal facts/preferences/provenance/edges ──
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS memory_facts ( id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT NOT NULL, predicate TEXT NOT NULL, object TEXT NOT NULL, object_type TEXT NOT NULL DEFAULT 'text', valid_from REAL, valid_to REAL, learned_at REAL NOT NULL, expired_at REAL, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'rejected', 'uncertain', 'pending_review')), confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1), fact_hash TEXT NOT NULL UNIQUE, superseded_by INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL, FOREIGN KEY (superseded_by) REFERENCES memory_facts(id) )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS memory_fact_sources ( fact_id INTEGER NOT NULL, memory_id INTEGER NOT NULL, evidence_text TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, PRIMARY KEY (fact_id, memory_id), FOREIGN KEY (fact_id) REFERENCES memory_facts(id) ON DELETE CASCADE, FOREIGN KEY (memory_id) REFERENCES episodic_memories(id) )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS memory_preferences ( id INTEGER PRIMARY KEY AUTOINCREMENT, preference_key TEXT NOT NULL, preference_value TEXT NOT NULL, preference_type TEXT NOT NULL DEFAULT 'general', polarity REAL NOT NULL DEFAULT 1.0, scope TEXT NOT NULL DEFAULT 'global', valid_from REAL, valid_to REAL, learned_at REAL NOT NULL, expired_at REAL, status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'superseded', 'rejected', 'uncertain', 'pending_review')), confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1), observed_count INTEGER NOT NULL DEFAULT 1 CHECK(observed_count >= 0), explicitness TEXT NOT NULL DEFAULT 'explicit' CHECK(explicitness IN ('explicit', 'inferred')), superseded_by INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL, FOREIGN KEY (superseded_by) REFERENCES memory_preferences(id) )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS memory_preference_sources ( preference_id INTEGER NOT NULL, memory_id INTEGER NOT NULL, evidence_text TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, PRIMARY KEY (preference_id, memory_id), FOREIGN KEY (preference_id) REFERENCES memory_preferences(id) ON DELETE CASCADE, FOREIGN KEY (memory_id) REFERENCES episodic_memories(id) )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS memory_edges ( id INTEGER PRIMARY KEY AUTOINCREMENT, source_memory_id INTEGER NOT NULL, target_memory_id INTEGER NOT NULL, edge_type TEXT NOT NULL CHECK(edge_type IN ('supersedes', 'supports', 'similar', 'bridge')), weight REAL NOT NULL DEFAULT 1.0, confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1), evidence_json TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(source_memory_id, target_memory_id, edge_type), FOREIGN KEY (source_memory_id) REFERENCES episodic_memories(id), FOREIGN KEY (target_memory_id) REFERENCES episodic_memories(id) )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_facts_current ON memory_facts(subject, predicate, status, valid_to, expired_at)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_facts_as_of ON memory_facts(valid_from, valid_to, learned_at, expired_at)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_fact_sources_memory ON memory_fact_sources(memory_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_preferences_current ON memory_preferences(preference_key, scope, status, valid_to, expired_at)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_preferences_as_of ON memory_preferences(valid_from, valid_to, learned_at, expired_at)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_preference_sources_memory ON memory_preference_sources(memory_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_edges_source ON memory_edges(source_memory_id, edge_type)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_edges_target ON memory_edges(target_memory_id, edge_type)""")

    async def _detect_fts5(self) -> bool:
        """检测 FTS5 可用性（Windows 用户可能缺少 FTS5 扩展）。"""
        try:
            await self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_check USING fts5(x UNINDEXED, y)"
            )
            await self._conn.execute("DROP TABLE IF EXISTS _fts5_check")
            return True
        except Exception:
            logger.warning("database.fts5_not_available - FTS5虚拟表将跳过创建")
            return False

    async def _migrate_v14_cognitive_tables(self, fts5_available: bool) -> None:
        """v14 Part 1 (cognitive)：semantic_memories/memory_connections/bridge_memories/
        memory_revisions/preference_patterns 5 张认知表 + episodic_memories 3 列 + 9 索引。"""
        # 1. episodic_memories 新增 3 列（幂等：先检查列是否存在，镜像 v13 模式）
        await self._ensure_columns("episodic_memories", {
            "salience": "salience REAL DEFAULT 0.5",
            "last_accessed": "last_accessed REAL DEFAULT 0",
            "status": "status TEXT DEFAULT 'active'",
        })

        # 2. 新建 5 张认知表（CREATE TABLE IF NOT EXISTS，天然幂等）
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS semantic_memories ( id INTEGER PRIMARY KEY AUTOINCREMENT, source_memory_id INTEGER, content TEXT NOT NULL, embedding_id INTEGER DEFAULT -1, cluster_id INTEGER DEFAULT -1, salience REAL DEFAULT 0.5, access_count INTEGER DEFAULT 0, last_accessed REAL DEFAULT 0, created_at REAL NOT NULL, emotion_label TEXT DEFAULT '', metadata_json TEXT DEFAULT '{}' )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_semantic_cluster ON semantic_memories(cluster_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_semantic_salience ON semantic_memories(salience)""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS memory_connections ( id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL, target_id INTEGER NOT NULL, weight REAL DEFAULT 0.5, edge_type TEXT NOT NULL DEFAULT 'similar', activation_count INTEGER DEFAULT 0, created_at REAL NOT NULL, last_activated REAL DEFAULT 0 )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_conn_source ON memory_connections(source_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_conn_target ON memory_connections(target_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_conn_type ON memory_connections(edge_type)""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS bridge_memories ( id TEXT PRIMARY KEY, source_memory_id INTEGER NOT NULL, target_memory_id INTEGER NOT NULL, weight REAL NOT NULL, bridge_type TEXT DEFAULT 'semantic', source_session_id TEXT DEFAULT '', target_session_id TEXT DEFAULT '', cross_session INTEGER DEFAULT 0, discovered_at REAL NOT NULL, discovery_reason TEXT DEFAULT 'rem_bridge' )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_bridge_source ON bridge_memories(source_memory_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_bridge_target ON bridge_memories(target_memory_id)""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS memory_revisions ( id INTEGER PRIMARY KEY AUTOINCREMENT, old_memory_id INTEGER NOT NULL, new_memory_id INTEGER NOT NULL, conflict_type TEXT DEFAULT 'numeric_token', revision_chain TEXT DEFAULT '[]', created_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_revisions_old ON memory_revisions(old_memory_id)""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS preference_patterns ( id INTEGER PRIMARY KEY AUTOINCREMENT, pattern_text TEXT NOT NULL, confidence REAL DEFAULT 0.5, source_sessions TEXT DEFAULT '[]', salience REAL DEFAULT 2.0, created_at REAL NOT NULL, last_matched REAL DEFAULT 0, match_count INTEGER DEFAULT 0 )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_preference_salience ON preference_patterns(salience)""")

        logger.info("database.migration_v14_cognitive_tables_done")

    async def _migrate_v14_kg_v2_tables(self, fts5_available: bool) -> None:
        """v14 Part 2 (kg_v2)：kg_episodes/kg_entities_v2/kg_relations_v2/kg_communities 表
        + knowledge_entities/relations 数据迁移 + FTS5 索引回填。"""
        # ── Part 2: kg_v2 tables — 时序事实、实体演化、Episode溯源、社区发现 ──
        # 1. 创建 v2 表
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS kg_episodes ( id TEXT PRIMARY KEY, content TEXT NOT NULL, source_type TEXT DEFAULT 'summary', source_description TEXT DEFAULT '', valid_at REAL NOT NULL, created_at REAL NOT NULL, group_id TEXT DEFAULT 'default' )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_episode_valid_at ON kg_episodes(valid_at)""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS kg_entities_v2 ( id TEXT PRIMARY KEY, name TEXT UNIQUE, kind TEXT DEFAULT '', observations TEXT DEFAULT '[]', summary TEXT DEFAULT '', summary_version INTEGER DEFAULT 0, name_embedding TEXT DEFAULT NULL, community_id TEXT DEFAULT NULL, updated_at REAL NOT NULL, created_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_entity_v2_name ON kg_entities_v2(name)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_entity_v2_community ON kg_entities_v2(community_id)""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS kg_relations_v2 ( id TEXT PRIMARY KEY, from_entity TEXT NOT NULL, relation_type TEXT NOT NULL, to_entity TEXT NOT NULL, fact TEXT DEFAULT '', fact_embedding TEXT DEFAULT NULL, episode_ids TEXT DEFAULT '[]', valid_at REAL DEFAULT NULL, invalid_at REAL DEFAULT NULL, expired_at REAL DEFAULT NULL, is_current INTEGER DEFAULT 1, created_at REAL NOT NULL, updated_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_from ON kg_relations_v2(from_entity)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_to ON kg_relations_v2(to_entity)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_current ON kg_relations_v2(is_current)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_valid_at ON kg_relations_v2(valid_at)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_rel_v2_invalid_at ON kg_relations_v2(invalid_at)""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS kg_communities ( id TEXT PRIMARY KEY, name TEXT NOT NULL, summary TEXT DEFAULT '', member_entities TEXT DEFAULT '[]', name_embedding TEXT DEFAULT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS kg_edge_episode_refs ( edge_id TEXT NOT NULL, episode_id TEXT NOT NULL, PRIMARY KEY (edge_id, episode_id) )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_eer_episode ON kg_edge_episode_refs(episode_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_eer_edge ON kg_edge_episode_refs(edge_id)""")

        # FTS5 虚拟表单独创建（条件守卫：FTS5不可用时降级跳过）
        if fts5_available:
            try:
                await self._conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS kg_entities_v2_fts USING fts5( id UNINDEXED, name_summary )""")
                await self._conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS kg_relations_v2_fts USING fts5( id UNINDEXED, fact )""")
            except Exception as e:
                logger.warning("database.fts5_create_failed", error=str(e))
                fts5_available = False

        # 1b. 幂等添加 community_id 列（修复 name_embedding 语义劫持）
        await self._ensure_columns("kg_entities_v2", {
            "community_id": "community_id TEXT DEFAULT NULL",
        })
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_kg_entity_v2_community ON kg_entities_v2(community_id)"
        )
        # 迁移旧的 name_embedding 中的 community_id 到专用列
        await self._conn.execute(
            """UPDATE kg_entities_v2 SET community_id = name_embedding
               WHERE name_embedding IS NOT NULL
                 AND name_embedding LIKE 'COM-%'"""
        )
        # 清理被劫持的 name_embedding（恢复为 NULL，后续由向量表使用）
        await self._conn.execute(
            """UPDATE kg_entities_v2 SET name_embedding = NULL
               WHERE name_embedding LIKE 'COM-%'"""
        )

        # 2. 迁移 entities: knowledge_entities → kg_entities_v2
        await self._conn.execute("""
            INSERT OR IGNORE INTO kg_entities_v2 (id, name, kind, observations, summary, summary_version, updated_at, created_at)
            SELECT id, name, kind, observations,
                   observations AS summary,
                   0,
                   updated_at,
                   updated_at
            FROM knowledge_entities
            WHERE name NOT IN (SELECT name FROM kg_entities_v2)
        """)

        # 2b. 幂等补齐 knowledge_relations.created_at 列
        # 旧版数据库的 knowledge_relations 表可能缺少 created_at 列（DDL 用 CREATE TABLE IF NOT EXISTS
        # 不会为已存在的表补列，v1 迁移也只加了 valid_from/valid_to/confidence）。
        # v14 数据迁移引用 created_at，缺失会导致 OperationalError。
        await self._ensure_columns("knowledge_relations", {
            "created_at": "created_at REAL DEFAULT 0",
        })

        # 3. 迁移 relations: knowledge_relations → kg_relations_v2
        await self._conn.execute("""
            INSERT OR IGNORE INTO kg_relations_v2 (id, from_entity, relation_type, to_entity, fact, episode_ids, valid_at, invalid_at, expired_at, is_current, created_at, updated_at)
            SELECT id, from_entity, relation_type, to_entity,
                   from_entity || ' ' || relation_type || ' ' || to_entity AS fact,
                   '[]',
                   created_at AS valid_at,
                   NULL,
                   NULL,
                   1,
                   created_at,
                   updated_at
            FROM knowledge_relations
            WHERE id NOT IN (SELECT id FROM kg_relations_v2)
        """)

        # 4. 回填 FTS5 索引 (条件守卫: FTS5不可用时跳过，数据完整性不受影响)
        if fts5_available:
            try:
                await self._conn.execute("""
                    INSERT OR IGNORE INTO kg_entities_v2_fts (id, name_summary)
                    SELECT id, name || ' ' || summary FROM kg_entities_v2
                    WHERE id NOT IN (SELECT id FROM kg_entities_v2_fts)
                """)
                await self._conn.execute("""
                    INSERT OR IGNORE INTO kg_relations_v2_fts (id, fact)
                    SELECT id, fact FROM kg_relations_v2
                    WHERE id NOT IN (SELECT id FROM kg_relations_v2_fts)
                """)
            except Exception as e:
                logger.warning("database.fts5_backfill_failed", error=str(e))
                # FTS5回填失败非致命，KG搜索降级为LIKE查询

    async def _migrate_v14(self) -> None:
        """v14: v0.6 认知架构 + 知识图谱 v2 — 5 张认知表 + episodic_memories 3 列 + 9 索引 + KG v2 表。"""
        _fts5_available = await self._detect_fts5()
        await self._migrate_v14_cognitive_tables(_fts5_available)
        await self._migrate_v14_kg_v2_tables(_fts5_available)

    # FSRS-DSR 迁移列定义（episodic_memories 与 concept_nodes 共用）
    _FSRS_COLUMNS: ClassVar[dict[str, str]] = {
        "difficulty": "difficulty REAL DEFAULT 5.0",
        "stability": "stability REAL DEFAULT 3.0",
        "phase": "phase TEXT DEFAULT 'buffer'",
        "last_review": "last_review REAL DEFAULT 0",
        "reinforcement_count": "reinforcement_count INTEGER DEFAULT 0",
    }

    async def _migrate_v15(self) -> None:
        """v15: FSRS-DSR 记忆模型 — 为 episodic_memories 和 concept_nodes 添加 FSRS 列。

        episodic_memories 新增列:
        - difficulty REAL DEFAULT 5.0   (FSRS 难度 D, 1-10)
        - stability REAL DEFAULT 3.0   (FSRS 稳定性 S, 天)
        - phase TEXT DEFAULT 'buffer'  (记忆阶段: buffer/reinforced/decayed/permanent/archived)
        - last_review REAL DEFAULT 0   (上次复习时间戳)
        - reinforcement_count INTEGER DEFAULT 0  (强化次数)

        concept_nodes 新增列:
        - difficulty REAL DEFAULT 5.0
        - stability REAL DEFAULT 3.0
        - phase TEXT DEFAULT 'buffer'
        - last_review REAL DEFAULT 0
        - reinforcement_count INTEGER DEFAULT 0

        迁移策略:
        - 旧数据统一 S=3.0, phase='buffer'
        - access_count >= 5 的旧数据直接标记为 phase='permanent'
        """
        await self._ensure_columns("episodic_memories", self._FSRS_COLUMNS)
        await self._ensure_columns("concept_nodes", self._FSRS_COLUMNS)

        # 旧数据迁移：access_count >= 5 → permanent
        await self._conn.execute(
            """UPDATE episodic_memories SET phase = 'permanent'
               WHERE access_count >= 5 AND phase = 'buffer'"""
        )
        await self._conn.execute(
            """UPDATE concept_nodes SET phase = 'permanent'
               WHERE access_count >= 5 AND phase = 'buffer'"""
        )

        # 索引
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_epi_phase ON episodic_memories(phase)"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_concept_phase ON concept_nodes(phase)"
        )

        logger.info("database.migration_v15_fsrs_dsr_done")

    async def _migrate_v16(self) -> None:
        """v16: Add created_at REAL column + backfill last_review=0 rows."""
        await self._ensure_columns("concept_nodes", {
            "created_at": "created_at REAL DEFAULT 0",
        })

        await self._ensure_columns("episodic_memories", {
            "created_at": "created_at REAL DEFAULT 0",
        })

        await self._conn.execute("""
            UPDATE concept_nodes
            SET created_at = CAST(
                (julianday(substr(created, 1, 19)) - julianday('1970-01-01')) * 86400 AS REAL)
            WHERE created_at = 0 AND created IS NOT NULL AND created != ''
        """)

        await self._conn.execute("""
            UPDATE episodic_memories
            SET created_at = timestamp
            WHERE created_at = 0 AND timestamp > 0
        """)

        await self._conn.execute("""
            UPDATE episodic_memories
            SET last_review = timestamp
            WHERE last_review = 0 AND timestamp > 0
        """)

        await self._conn.commit()
        logger.info("database.migration_v16_created_at_done")

    async def _migrate_v17(self) -> None:
        """v17: greeting_schedules 扩展 type 约束以支持 'reminder' 类型。

        SQLite 不支持 ALTER CHECK，需重建表。
        """
        # 检查是否已有 reminder 类型的数据（说明已迁移过）
        try:
            existing = await self.fetch_all(
                "SELECT DISTINCT type FROM greeting_schedules")
            types = {r["type"] for r in existing}
            if "reminder" in types:
                logger.info("database.migration_v17_skipped_already_has_reminder")
                return
        except Exception as e:
            logger.warning("database.migration_v17_check_failed", error=str(e))

        # 重建表以更新 CHECK 约束 (含 user_id 列以匹配 v20 schema, 避免列数不匹配)
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS greeting_schedules_v17 ( id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL CHECK(type IN ('fixed','random','reminder')), time TEXT DEFAULT '', window_start TEXT DEFAULT '', window_end TEXT DEFAULT '', count_per_day INTEGER DEFAULT 1, days TEXT NOT NULL DEFAULT '[1,2,3,4,5,6,7]', prompt_hint TEXT DEFAULT '', channels TEXT NOT NULL DEFAULT '["web"]', enabled INTEGER NOT NULL DEFAULT 1, next_fire_times TEXT DEFAULT '[]', drawn_date TEXT DEFAULT '', created_at REAL NOT NULL, user_id TEXT NOT NULL DEFAULT 'default' )""")
        await self._conn.execute("""INSERT OR IGNORE INTO greeting_schedules_v17 SELECT * FROM greeting_schedules""")
        await self._conn.execute("""DROP TABLE IF EXISTS greeting_schedules""")
        await self._conn.execute("""ALTER TABLE greeting_schedules_v17 RENAME TO greeting_schedules""")
        await self._conn.commit()
        logger.info("database.migration_v17_reminder_type_done")

    async def _migrate_v18(self) -> None:
        """v18: 添加 distill_status 列，用于跟踪蒸馏状态（替代 emotion_label 滥用）。"""
        await self._ensure_columns("episodic_memories", {
            "distill_status": "distill_status TEXT DEFAULT ''",
        })
        # 回填：把旧的 emotion_label='distill_failed' 记录标记为 distill_status='failed'
        await self._conn.execute(
            "UPDATE episodic_memories SET distill_status = 'failed' "
            "WHERE emotion_label = 'distill_failed'"
        )
        logger.info("database.migration_v18_distill_status_done")

    async def _migrate_v19(self) -> None:
        """v19: 添加 episodic_memories.updated_at 列 + summary 变更触发器。

        updated_at 字段记录 summary 最后一次内容更新时间，由触发器
        trg_episodic_memories_touch_updated_at 在 summary 被 UPDATE 时自动维护。
        仅在 summary 列变更时更新，其他列（emotion_label、access_count 等）变更不触发。

        SQLite 默认 recursive_triggers=OFF，AFTER UPDATE 内对同表非触发列的
        UPDATE 不会递归触发自身，所以触发器是安全的。
        """
        await self._ensure_columns("episodic_memories", {
            "updated_at": "updated_at REAL DEFAULT 0",
        })

        # 回填：已有记录的 updated_at 初始化为 timestamp（创建时间）
        # 后续 summary 变更时由触发器自动更新
        await self._conn.execute(
            "UPDATE episodic_memories SET updated_at = timestamp "
            "WHERE updated_at = 0 AND timestamp > 0"
        )

        # 创建触发器（CREATE TRIGGER IF NOT EXISTS 幂等）
        await self._conn.execute(
            """CREATE TRIGGER IF NOT EXISTS trg_episodic_memories_touch_updated_at
               AFTER UPDATE OF summary ON episodic_memories
               FOR EACH ROW
               BEGIN
                   UPDATE episodic_memories
                   SET updated_at = CAST(strftime('%s','now') AS REAL)
                   WHERE id = OLD.id;
               END"""
        )
        logger.info("database.migration_v19_updated_at_trigger_done")

    async def _migrate_v20(self) -> None:
        """v20: greeting_schedules 添加 user_id 列, 用于 reminder 用户隔离.

        历史数据回填为 'default' (任何已登录用户均可访问, 兼容旧逻辑);
        新建 reminder 由调用方传入 user_id, 实现按用户隔离 update/delete.
        """
        await self._ensure_columns("greeting_schedules", {
            "user_id": "user_id TEXT NOT NULL DEFAULT 'default'",
        })
        logger.info("database.migration_v20_user_id_added")

    async def _migrate_v21(self) -> None:
        """v21: 废弃 knowledge_entities_fts 触发器，改应用层维护 FTS。

        根因：contentless FTS5 表 knowledge_entities_fts 的 'delete' 命令在 SQLite 3.40
        始终报 SQL logic error（即使列值完全匹配）。原触发器 knowledge_entities_fts_au/ad
        用 old.id (TEXT) 当 FTS5 delete 的 rowid 参数，而 knowledge_entities.id 是 TEXT
        PRIMARY KEY、rowid 是隐式 INTEGER，两者无关 → delete 永远找不到行 → 报错。
        merge_entity 的 UPDATE 因此触发 FTS delete 失败 → 两级降级全失败 → observations
        写不进去 → 称呼等关键信息错乱残留（爸爸/大哥哥/老公大人混用）。

        修复：
        1. DROP 三个 FTS 触发器（改由 db_knowledge.py 应用层用普通 DELETE+INSERT 维护，
           普通 DELETE 在 contentless FTS5 上可用）
        2. 重建 FTS 内容，使 FTS rowid 与主表 rowid 对齐（原有 117 实体仅 56 进了 FTS，
           62 个缺失含「小妲」；缺失实体 UPDATE 时触发器 delete 也报错）
        """
        await self._conn.execute("DROP TRIGGER IF EXISTS knowledge_entities_fts_ai")
        await self._conn.execute("DROP TRIGGER IF EXISTS knowledge_entities_fts_ad")
        await self._conn.execute("DROP TRIGGER IF EXISTS knowledge_entities_fts_au")
        # 重建 FTS 内容：rowid 与主表对齐 + _tokenize_for_fts 预分词
        # 应用层 _sync_entity_fts 也按 rowid + tokenized name 维护，必须保持一致。
        await self._conn.execute("DELETE FROM knowledge_entities_fts")
        # CodeRabbit 根因修复：原版用 `SELECT rowid, id, name FROM knowledge_entities`
        # 直接 INSERT 原始 name。实测 FTS5 unicode61 不会按字符切分 CJK，原始 '小妲'
        # 变成单个不透明 token，MATCH '妲' 命中为 0，字符级搜索全部失效（v5 已分词，
        # v21 重建若用原始 name 会回退退化）。必须与 _migrate_v5/_sync_entity_fts 一致，
        # 用 _tokenize_for_fts 预分词（jieba 分词后空格连接），FTS5 才能按词建可检索索引。
        from db.fts_utils import _tokenize_for_fts
        cursor = await self._conn.execute("SELECT rowid, id, name FROM knowledge_entities")
        rows = await cursor.fetchall()
        for row in rows:
            _rowid, _id, _name = row[0], row[1], row[2]
            _name_index = _tokenize_for_fts(_name) if _name else ""
            await self._conn.execute(
                "INSERT INTO knowledge_entities_fts(rowid, id, name_index) VALUES(?, ?, ?)",
                (_rowid, _id, _name_index),
            )
        logger.info("database.migration_v21", desc="knowledge_entities_fts_trigger_drop",
                    rows=len(rows),
                    hint="FTS 改应用层维护 + _tokenize_for_fts 预分词，修复 SQL logic error 致称呼错乱")

    async def _migrate_v22(self) -> None:
        """v22: 重建 episodic/memory_entities FTS 索引——CJK 单字进入索引。

        根因：fts_utils._extract_fts_keywords 用 min_length=2 过滤单字，纯单字短查询
        （"你是谁"/"陪着我"）分词后全部被过滤，_build_fts_query 返回空 → FTS 短路。
        修复侧（fts_utils）已放行 CJK 单字；本迁移用新 tokenizer 重建存量索引，
        否则已入库记忆的单字在 FTS 里仍命中为 0，单字查询对存量数据依旧无效。
        """
        from db.fts_utils import _tokenize_for_fts
        # 1. episodic_memory_fts（无触发器，应用层手动维护）
        await self._conn.execute("DELETE FROM episodic_memory_fts")
        cursor = await self._conn.execute("SELECT id, summary FROM episodic_memories")
        rows = await cursor.fetchall()
        for row in rows:
            tokenized = _tokenize_for_fts(row[1])
            if tokenized.strip():
                await self._conn.execute(
                    "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                    (row[0], tokenized),
                )
        # 2. memory_entities_fts：触发器改用应用层手动维护（与 v21 同一策略），
        #    避免重建时触发器双重写入（ai 触发器用原始 name，非预分词）。
        await self._conn.execute("DROP TRIGGER IF EXISTS memory_entities_fts_ai")
        await self._conn.execute("DROP TRIGGER IF EXISTS memory_entities_fts_ad")
        await self._conn.execute("DROP TRIGGER IF EXISTS memory_entities_fts_au")
        await self._conn.execute("DELETE FROM memory_entities_fts")
        cursor2 = await self._conn.execute("SELECT id, name FROM memory_entities")
        rows2 = await cursor2.fetchall()
        for row in rows2:
            tokenized = _tokenize_for_fts(row["name"]) if row["name"] else ""
            if tokenized.strip():
                await self._conn.execute(
                    "INSERT INTO memory_entities_fts(id, name_index) VALUES(?, ?)",
                    (row["id"], tokenized),
                )
        logger.info("database.migration_v22", desc="fts_single_char_rebuild",
                    episodic=len(rows), entities=len(rows2))

    async def _migrate_v23(self) -> None:
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS profile_fields (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                field_key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                value_type TEXT NOT NULL,
                valid_from REAL NOT NULL,
                valid_to REAL,
                learned_at REAL NOT NULL,
                expired_at REAL,
                superseded_by INTEGER,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (superseded_by) REFERENCES profile_fields(id)
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_profile_fields_current
            ON profile_fields(user_id, agent_id, namespace, field_key, expired_at)
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_profile_fields_as_of
            ON profile_fields(user_id, agent_id, namespace, field_key,
                              valid_from, valid_to, learned_at, expired_at)
        """)
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS profile_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                field_key TEXT NOT NULL,
                candidate_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                field_id INTEGER,
                recorded_at REAL NOT NULL,
                FOREIGN KEY (field_id) REFERENCES profile_fields(id)
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_profile_events_scope
            ON profile_events(user_id, agent_id, recorded_at)
        """)

    async def _migrate_v24(self) -> None:
        await self._conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_events_idempotency
            ON profile_events(
                user_id, agent_id, namespace, field_key, source_type, source_id
            )
            WHERE status = 'accepted'
        """)

    async def _migrate_v25(self) -> None:
        """v25: installed_models 表 + 内置 BGE 嵌入模型种子（Task 7 持久化模型注册表）。

        幂等保证：
        - CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS 重跑安全。
        - 内置 BGE 条目通过存在性检查 + INSERT OR IGNORE 双重防御，避免
          schema_version 回退后重复插入。
        - 不调用 write_transaction()：迁移框架 _apply_migration 已管理提交节奏，
          在迁移函数内部嵌套 write_transaction 会触发 mid-migration commit，
          干扰 _apply_migration 的 dirty/schema_version 提交顺序。
        """
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS installed_models (
                id TEXT PRIMARY KEY,
                catalog_id TEXT NOT NULL,
                revision TEXT NOT NULL,
                purpose TEXT NOT NULL,
                directory TEXT NOT NULL UNIQUE,
                manifest_checksum TEXT NOT NULL,
                validation_state TEXT NOT NULL,
                ownership TEXT NOT NULL,
                installed_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_installed_models_purpose
            ON installed_models(purpose)
        """)
        await self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_installed_models_catalog_id
            ON installed_models(catalog_id)
        """)
        # 种子内置 BGE 嵌入模型：与 memory/vector_store.py 中
        # _default_local_model_dir() 的项目内路径约定对齐（<root>/models/
        # bge-small-zh-v1.5）。该条目 ownership="bundled"，注册表禁止删除。
        import json as _json
        from datetime import datetime, timezone
        from pathlib import Path
        bundled_id = "builtin:bge-small-zh-v1.5"
        # 幂等守卫 1：存在性检查（避免 schema_version 回退后重复 INSERT）
        cursor = await self._conn.execute(
            "SELECT 1 FROM installed_models WHERE id = ? LIMIT 1",
            (bundled_id,),
        )
        if await cursor.fetchone() is None:
            bundled_directory = str(
                Path(__file__).resolve().parent.parent / "models" / "bge-small-zh-v1.5"
            )
            bundled_metadata = _json.dumps(
                {
                    "source": "builtin",
                    "description": "Bundled BGE small Chinese embedding",
                },
                ensure_ascii=False,
            )
            # 幂等守卫 2：INSERT OR IGNORE 防御并发场景
            await self._conn.execute(
                """
                INSERT OR IGNORE INTO installed_models (
                    id, catalog_id, revision, purpose, directory,
                    manifest_checksum, validation_state, ownership,
                    installed_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundled_id,
                    bundled_id,
                    "0000000",
                    "embedding",
                    bundled_directory,
                    "builtin",
                    "validated",
                    "bundled",
                    datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    bundled_metadata,
                ),
            )

    async def _migrate_v26(self) -> None:
        await self._ensure_columns("conversation_logs", {
            "request_context_json": "request_context_json TEXT DEFAULT '{}'",
        })

    async def _migrate_v27(self) -> None:
        # workflow_v2 表（CREATE TABLE IF NOT EXISTS，幂等）
        await db_workflow.create_schema(self._conn)