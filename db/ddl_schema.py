"""DDLMixin — 建表/索引/清理策略/FTS5 触发器 DDL。

自 db/database.py 拆分（上帝文件 Phase 2）：函数体逐字节搬移，仅缩进调整。
_create_tables 编排入口一并搬入（依赖 LegacyMigrationMixin._run_migrations，
由 DatabaseManager 继承组合）。
"""
from __future__ import annotations

from loguru import logger

from .db_memory_reconciliation import create_schema as create_reconciliation_schema


class DDLMixin:
    async def _create_tables(self) -> None:
        """创建所有表、运行迁移、创建索引、初始化清理策略与 FTS5 触发器。"""
        # ── Phase 1: 建表（仅 DDL，不含依赖新列的索引）─────────
        await self._create_tables_ddl()
        # ── Phase 2: 迁移（在建表之后、索引创建之前执行）────────
        # 重要：迁移必须在这里执行，因为旧数据库可能缺少 session_id 等列，
        # 而后续的索引创建依赖这些列存在。
        # 如果把 _run_migrations 放在 executescript 末尾，索引创建会先于迁移执行，
        # 导致 "no such column: session_id" 错误，且迁移永远无法到达。
        await self._run_migrations()
        # ── Phase 3: 创建索引（含依赖迁移列的索引）──────────────────
        await self._create_indexes()
        # ── Phase 4: 插入默认清理策略（仅当表为空时）──────────────
        await self._seed_cleanup_config()
        # ── Phase 5: FTS5 触发器管理（vfat 上禁用）──────────────
        await self._setup_fts5_triggers()
        await self._conn.commit()

    async def _create_tables_ddl(self) -> None:
        """Phase 1: 建表 DDL。按领域分组调用，便于维护。"""
        await self._ddl_memory_tables()
        await self._ddl_schedule_api_tables()
        await self._ddl_knowledge_tables()
        await self._ddl_learning_error_tables()
        await self._ddl_concept_tables()

    async def _ddl_memory_tables(self) -> None:
        """建表：对话/记忆/笔记相关表。

        注意：逐条执行 DDL，避免 executescript() 在 vfat 上触发隐式 commit 导致 database is locked。
        """
        # conversation_logs
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS conversation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                user_id TEXT DEFAULT '',
                source TEXT DEFAULT 'qq',
                user_message TEXT DEFAULT '',
                assistant_reply TEXT DEFAULT '',
                emotion_label TEXT DEFAULT '',
                model_used TEXT DEFAULT ''
            )
        """)

        # audit_logs
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                user_id TEXT DEFAULT '',
                detail TEXT DEFAULT ''
            )
        """)

        # episodic_memories
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS episodic_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                summary TEXT NOT NULL,
                importance REAL DEFAULT 0.5,
                emotion_label TEXT DEFAULT '',
                session_id TEXT DEFAULT 'user',
                embedding_id INTEGER DEFAULT -1,
                rag_status TEXT DEFAULT 'pending',
                rag_synced_at REAL DEFAULT 0,
                doc_id TEXT DEFAULT '',
                source TEXT DEFAULT 'user',
                access_count INTEGER DEFAULT 0,
                distilled INTEGER DEFAULT 0,
                entities TEXT DEFAULT '',
                event_type TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}',
                content_hash TEXT DEFAULT '',
                version INTEGER DEFAULT 1,
                user_id TEXT DEFAULT 'default',
                agent_id TEXT DEFAULT 'xiaoda',
                is_raw INTEGER DEFAULT 0,
                updated_at REAL DEFAULT 0,
                memory_type TEXT DEFAULT 'event',
                classification_status TEXT DEFAULT 'pending',
                classification_version INTEGER DEFAULT 0,
                classified_at REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                superseded_by INTEGER
            )
        """)
        await create_reconciliation_schema(self._conn)

        # 触发器：summary 变更时自动维护 updated_at
        # SQLite 默认 recursive_triggers=OFF，AFTER UPDATE 内对同表非触发列的
        # UPDATE 不会递归触发自身，安全。
        await self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_episodic_memories_touch_updated_at
            AFTER UPDATE OF summary ON episodic_memories
            FOR EACH ROW
            BEGIN
                UPDATE episodic_memories
                SET updated_at = CAST(strftime('%s','now') AS REAL)
                WHERE id = OLD.id;
            END
        """)

        # memory_summaries
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_text TEXT NOT NULL,
                created_at REAL NOT NULL,
                memory_count INTEGER DEFAULT 0
            )
        """)

        # memory_recall_notes
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

        # user_portrait
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS user_portrait (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                source_ids TEXT DEFAULT '',
                change_log TEXT DEFAULT '',
                created_at REAL NOT NULL
            )
        """)

        # notebook_entries
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS notebook_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT 'note',
                content TEXT NOT NULL,
                tags TEXT DEFAULT '',
                importance REAL DEFAULT 0.5,
                due_date REAL DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)

        # proactive_messages
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS proactive_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                message_type TEXT NOT NULL,
                content TEXT NOT NULL,
                sent_at REAL NOT NULL
            )
        """)

        # memory_child_chunks
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_child_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                embed_content TEXT DEFAULT '',
                chunk_type TEXT NOT NULL DEFAULT 'segment',
                importance REAL DEFAULT 0.5,
                overlap_hash TEXT DEFAULT '',
                created_at REAL NOT NULL,
                FOREIGN KEY (parent_id) REFERENCES episodic_memories(id) ON DELETE CASCADE
            )
        """)
        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_child_parent ON memory_child_chunks(parent_id)")
        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_child_type ON memory_child_chunks(chunk_type)")
        # FTS5 虚拟表单独执行（不能与 executescript 中的普通 DDL 混合在 vfat 上）
        try:
            await self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memory_child_chunks_fts "
                "USING fts5(content, tokenize='unicode61')"
            )
        except Exception as e:
            logger.warning("创建 memory_child_chunks_fts 失败: {}", e)

    async def _ddl_schedule_api_tables(self) -> None:
        """建表：调度/API/会话相关表。"""
        await self._ddl_schedule_greeting_tables()
        await self._ddl_api_media_tables()
        await self._ddl_session_agent_tables()

    async def _ddl_schedule_greeting_tables(self) -> None:
        """建表：调度与问候相关表（cron_last_run / greeting_schedules / greeting_log）。"""
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS cron_last_run ( task_name TEXT PRIMARY KEY, last_run REAL NOT NULL )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS greeting_schedules ( id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL CHECK(type IN ('fixed','random','reminder')), time TEXT DEFAULT '', window_start TEXT DEFAULT '', window_end TEXT DEFAULT '', count_per_day INTEGER DEFAULT 1, days TEXT NOT NULL DEFAULT '[1,2,3,4,5,6,7]', prompt_hint TEXT DEFAULT '', channels TEXT NOT NULL DEFAULT '["web"]', enabled INTEGER NOT NULL DEFAULT 1, next_fire_times TEXT DEFAULT '[]', drawn_date TEXT DEFAULT '', created_at REAL NOT NULL, user_id TEXT NOT NULL DEFAULT 'default' )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS greeting_log ( id INTEGER PRIMARY KEY AUTOINCREMENT, schedule_id INTEGER DEFAULT 0, fired_at REAL NOT NULL, content TEXT DEFAULT '', channel TEXT DEFAULT 'web', reason TEXT DEFAULT '' )""")

    async def _ddl_api_media_tables(self) -> None:
        """建表：API 用量/媒体任务/健康报告相关表。"""
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS api_usage ( id TEXT PRIMARY KEY, user_openid TEXT DEFAULT '', session_id TEXT DEFAULT '', model TEXT DEFAULT '', task_type TEXT DEFAULT '', prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0, cache_hit_tokens INTEGER DEFAULT 0, cache_miss_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, created_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS media_tasks ( id TEXT PRIMARY KEY, kind TEXT NOT NULL, prompt TEXT DEFAULT '', params TEXT DEFAULT '{}', status TEXT NOT NULL DEFAULT 'queued', progress REAL DEFAULT 0, result_path TEXT DEFAULT '', error TEXT DEFAULT '', created_at REAL NOT NULL, finished_at REAL )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS health_reports ( id INTEGER PRIMARY KEY AUTOINCREMENT, run_at REAL NOT NULL, passed INTEGER DEFAULT 0, total INTEGER DEFAULT 0, detail TEXT NOT NULL DEFAULT '[]' )""")

    async def _ddl_session_agent_tables(self) -> None:
        """建表：会话与事件相关表（sessions / agent_events）。"""
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS sessions ( id TEXT PRIMARY KEY, user_openid TEXT DEFAULT '', summary TEXT DEFAULT '', turn_count INTEGER DEFAULT 0, total_cost_usd REAL DEFAULT 0, cache_hit_tokens INTEGER DEFAULT 0, cache_miss_tokens INTEGER DEFAULT 0, started_at REAL NOT NULL, ended_at REAL DEFAULT 0, status TEXT DEFAULT 'active' )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS agent_events ( id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL, user_openid TEXT DEFAULT '', session_id TEXT DEFAULT '', detail TEXT DEFAULT '', created_at REAL NOT NULL )""")

    async def _ddl_knowledge_tables(self) -> None:
        """建表：知识图谱/FTS5 相关表。"""
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_entities ( id TEXT PRIMARY KEY, name TEXT UNIQUE, kind TEXT DEFAULT '', observations TEXT DEFAULT '[]', updated_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_entities_fts USING fts5( id UNINDEXED, name_index )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_relations ( id TEXT PRIMARY KEY, from_entity TEXT, relation_type TEXT, to_entity TEXT, created_at REAL DEFAULT 0, updated_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS consolidation_candidates ( id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL, source TEXT NOT NULL DEFAULT 'rule', kind TEXT NOT NULL DEFAULT 'fact', summary TEXT NOT NULL, confidence REAL DEFAULT 0.5, importance REAL DEFAULT 0.5, status TEXT NOT NULL DEFAULT 'pending', target_memory_id INTEGER DEFAULT -1, metadata_json TEXT DEFAULT '{}', created_at REAL NOT NULL )""")

    async def _ddl_learning_error_tables(self) -> None:
        """建表：学习/错误/清理相关表。"""
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS learnings ( id INTEGER PRIMARY KEY AUTOINCREMENT, learning_id TEXT NOT NULL UNIQUE, category TEXT NOT NULL DEFAULT 'insight', priority TEXT NOT NULL DEFAULT 'low', status TEXT NOT NULL DEFAULT 'pending', area TEXT DEFAULT 'backend', summary TEXT NOT NULL, details TEXT DEFAULT '', suggested_action TEXT DEFAULT '', source TEXT DEFAULT 'conversation', pattern_key TEXT DEFAULT '', recurrence_count INTEGER DEFAULT 1, first_seen REAL NOT NULL, last_seen REAL NOT NULL, created_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS errors ( id INTEGER PRIMARY KEY AUTOINCREMENT, error_id TEXT NOT NULL UNIQUE, priority TEXT NOT NULL DEFAULT 'high', status TEXT NOT NULL DEFAULT 'pending', area TEXT DEFAULT 'backend', summary TEXT NOT NULL, error_text TEXT DEFAULT '', context TEXT DEFAULT '', suggested_fix TEXT DEFAULT '', reproducible TEXT DEFAULT 'unknown', created_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS feature_requests ( id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE, priority TEXT NOT NULL DEFAULT 'medium', status TEXT NOT NULL DEFAULT 'pending', area TEXT DEFAULT 'backend', capability TEXT NOT NULL, user_context TEXT DEFAULT '', complexity TEXT DEFAULT 'medium', suggested_impl TEXT DEFAULT '', frequency TEXT DEFAULT 'first_time', created_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS session_entries ( id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, entry_json TEXT NOT NULL, created_at REAL NOT NULL )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS session_summaries ( session_id TEXT PRIMARY KEY, mtime INTEGER NOT NULL DEFAULT 0, summary_data TEXT NOT NULL DEFAULT '{}' )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS cleanup_config ( table_name TEXT PRIMARY KEY, retention_days INTEGER NOT NULL, date_column TEXT NOT NULL DEFAULT 'timestamp', enabled INTEGER DEFAULT 1 )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS tool_error_rules ( id INTEGER PRIMARY KEY AUTOINCREMENT, tool_name TEXT NOT NULL, pattern TEXT NOT NULL, rule_text TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, hit_count INTEGER DEFAULT 0 )""")

    async def _ddl_concept_tables(self) -> None:
        """建表：概念图（扩散激活记忆系统）。"""
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS concept_nodes ( id TEXT PRIMARY KEY, text TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0, peak_weight REAL NOT NULL DEFAULT 1.0, confidence REAL NOT NULL DEFAULT 1.0, access_count INTEGER NOT NULL DEFAULT 0, keys TEXT NOT NULL DEFAULT '[]', layer TEXT NOT NULL DEFAULT 'hippocampus', created TEXT NOT NULL, last_accessed TEXT NOT NULL, valid_from TEXT NOT NULL, valid_to TEXT, superseded_by TEXT, history TEXT NOT NULL DEFAULT '[]', origin TEXT NOT NULL DEFAULT '{}', source_mem_id INTEGER, embedding BLOB, difficulty REAL NOT NULL DEFAULT 5.0, stability REAL NOT NULL DEFAULT 3.0, phase TEXT NOT NULL DEFAULT 'buffer', last_review REAL NOT NULL DEFAULT 0, reinforcement_count INTEGER NOT NULL DEFAULT 0 )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS concept_edges ( source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL DEFAULT 'related', weight REAL NOT NULL DEFAULT 1.0, created TEXT NOT NULL, PRIMARY KEY (source_id, target_id) )""")
        await self._conn.execute("""CREATE TABLE IF NOT EXISTS concept_meta ( key TEXT PRIMARY KEY, value TEXT NOT NULL )""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_concept_node_keys ON concept_nodes(keys)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_concept_node_layer ON concept_nodes(layer)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_concept_node_weight ON concept_nodes(weight)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_concept_node_valid ON concept_nodes(valid_to)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_concept_edge_source ON concept_edges(source_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_concept_edge_target ON concept_edges(target_id)""")

    async def _create_indexes(self) -> None:
        """Phase 3: 创建所有索引（含依赖迁移列的索引）。"""
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversation_logs(timestamp)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_mem_ts ON episodic_memories(timestamp)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_mem_importance ON episodic_memories(importance)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_portrait_created ON user_portrait(created_at)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_notebook_kind ON notebook_entries(kind)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_notebook_status ON notebook_entries(status)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_notebook_due ON notebook_entries(due_date)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_proactive_user ON proactive_messages(user_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_api_usage_ts ON api_usage(created_at)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_api_usage_user ON api_usage(user_openid)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_openid)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_events_type ON agent_events(event_type)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_events_ts ON agent_events(created_at)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_entity_name ON knowledge_entities(name)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_entity_updated ON knowledge_entities(updated_at)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_rel_from ON knowledge_relations(from_entity)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_rel_to ON knowledge_relations(to_entity)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_conv_user ON conversation_logs(user_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_conv_source ON conversation_logs(source)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_episodic_session ON episodic_memories(session_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_logs(event_type)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation_logs(session_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_kg_rel_type ON knowledge_relations(relation_type)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_media_status ON media_tasks(status)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_learnings_cat ON learnings(category)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_learnings_status ON learnings(status)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_learnings_pattern ON learnings(pattern_key)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_errors_status ON errors(status)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_featreq_status ON feature_requests(status)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_session_entries_sid ON session_entries(session_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_session_entries_created ON session_entries(created_at)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_tool_error_rules_tool ON tool_error_rules(tool_name)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_episodic_scope ON episodic_memories(user_id, agent_id, is_raw, timestamp DESC)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_episodic_memory_type ON episodic_memories(memory_type)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_episodic_classification_pending ON episodic_memories(user_id, agent_id, classification_status, id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_episodic_active_scope ON episodic_memories(user_id, agent_id, status, is_raw, id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_entities_name ON memory_entities(name)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_memory_entities_type ON memory_entities(entity_type)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_eml_entity ON entity_memory_links(entity_id)""")
        await self._conn.execute("""CREATE INDEX IF NOT EXISTS idx_eml_memory ON entity_memory_links(memory_id)""")

    async def _seed_cleanup_config(self) -> None:
        """Phase 4: 插入默认清理策略（仅当 cleanup_config 表为空时）。"""
        try:
            cursor = await self._conn.execute("SELECT COUNT(*) FROM cleanup_config")
            row = await cursor.fetchone()
            if row[0] == 0:
                await self._conn.executemany(
                    "INSERT INTO cleanup_config (table_name, retention_days, date_column) VALUES (?, ?, ?)",
                    [
                        ("audit_logs", 90, "timestamp"),
                        ("api_usage", 30, "created_at"),
                        ("sessions", 180, "ended_at"),
                    ],
                )
        except (OSError, RuntimeError) as e:
            logger.warning("插入默认清理策略失败: {}", e)

    async def _setup_fts5_triggers(self) -> None:
        """Phase 5: FTS5 触发器管理。vfat/exfat 上禁用（delete 命令不工作）。"""
        if getattr(self, "_is_fat_fs", False):
            # 删除可能存在的触发器（防止之前版本创建的触发器残留）
            for trig in ["knowledge_entities_fts_ai", "knowledge_entities_fts_ad", "knowledge_entities_fts_au",
                         "kg_entities_v2_fts_ai", "kg_entities_v2_fts_ad", "kg_entities_v2_fts_au",
                         "kg_relations_v2_fts_ai", "kg_relations_v2_fts_ad", "kg_relations_v2_fts_au"]:
                try:
                    await self._conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
                except (OSError, RuntimeError):
                    logger.debug("database.fts5_trigger_drop_error", exc_info=True)
            logger.info("database.fts5_triggers_disabled (vfat)")
            return
        # 非 fat 文件系统：创建 FTS5 触发器
        # 注意：每个触发器必须是一个完整的 SQL 语句，包含 BEGIN...END
        try:
            # knowledge_entities_fts 触发器已废弃，改应用层维护（db_knowledge._sync_entity_fts）。
            # 根因：contentless FTS5 的 'delete' 命令在 SQLite 3.40 始终报 SQL logic error
            # （即使列值完全匹配），坏触发器导致 merge_entity UPDATE 失败 → observations
            # 写不进 → 称呼信息错乱残留（爸爸/大哥哥/老公大人混用）。
            # FTS rowid 与主表 rowid 对齐，由应用层 DELETE+INSERT 维护（普通 DELETE 可用）。
            # 每次启动主动 DROP，防止历史版本创建的坏触发器残留。
            for _trig in ("knowledge_entities_fts_ai", "knowledge_entities_fts_ad", "knowledge_entities_fts_au"):
                await self._conn.execute(f"DROP TRIGGER IF EXISTS {_trig}")
            # v2 triggers: DROP first to replace any prior broken versions on upgrade
            for trig in ["kg_entities_v2_fts_ai", "kg_entities_v2_fts_ad", "kg_entities_v2_fts_au",
                         "kg_relations_v2_fts_ai", "kg_relations_v2_fts_ad", "kg_relations_v2_fts_au"]:
                await self._conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
            # kg_entities_v2 triggers
            await self._conn.execute("""CREATE TRIGGER kg_entities_v2_fts_ai AFTER INSERT ON kg_entities_v2 BEGIN INSERT INTO kg_entities_v2_fts(id, name_summary) VALUES (new.id, new.name || ' ' || new.summary); END""")
            await self._conn.execute("""CREATE TRIGGER kg_entities_v2_fts_ad AFTER DELETE ON kg_entities_v2 BEGIN DELETE FROM kg_entities_v2_fts WHERE id = old.id; END""")
            await self._conn.execute("""CREATE TRIGGER kg_entities_v2_fts_au AFTER UPDATE ON kg_entities_v2 BEGIN DELETE FROM kg_entities_v2_fts WHERE id = old.id; INSERT INTO kg_entities_v2_fts(id, name_summary) VALUES (new.id, new.name || ' ' || new.summary); END""")
            # kg_relations_v2 triggers
            await self._conn.execute("""CREATE TRIGGER kg_relations_v2_fts_ai AFTER INSERT ON kg_relations_v2 BEGIN INSERT INTO kg_relations_v2_fts(id, fact) VALUES (new.id, new.fact); END""")
            await self._conn.execute("""CREATE TRIGGER kg_relations_v2_fts_ad AFTER DELETE ON kg_relations_v2 BEGIN DELETE FROM kg_relations_v2_fts WHERE id = old.id; END""")
            await self._conn.execute("""CREATE TRIGGER kg_relations_v2_fts_au AFTER UPDATE ON kg_relations_v2 BEGIN DELETE FROM kg_relations_v2_fts WHERE id = old.id; INSERT INTO kg_relations_v2_fts(id, fact) VALUES (new.id, new.fact); END""")
        except (OSError, RuntimeError) as e:
            logger.warning("database.fts5_trigger_failed: {} — FTS搜索将降级为LIKE查询", e)
