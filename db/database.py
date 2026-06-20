import json
import time
import uuid
from typing import Any
import aiosqlite
from pathlib import Path
from loguru import logger
from config import DATA_DIR
from .db_memory import MemoryDB
from .db_notebook import NotebookDB
from .db_learning import LearningDB
from .db_knowledge import KnowledgeDB
from .db_analytics import AnalyticsDB
from .session_store import (
    SessionInfo,
    SessionSummaryEntry,
    SessionStoreProtocol,
    fold_session_summary,
    summary_to_session_info,
)


DB_DIR = DATA_DIR
DB_PATH = DB_DIR / "agent.db"


class DatabaseManager:

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None
        self.memory: MemoryDB | None = None
        self.notebook: NotebookDB | None = None
        self.learning: LearningDB | None = None
        self.knowledge: KnowledgeDB | None = None
        self.analytics: AnalyticsDB | None = None

    async def init(self):
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        for pragma_sql in [
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA cache_size=-20000",
            "PRAGMA mmap_size=67108864",
            "PRAGMA temp_store=MEMORY",
        ]:
            try:
                await self._conn.execute(pragma_sql)
            except Exception as e:
                logger.warning(f"PRAGMA 失败: {pragma_sql} - {e}")
        try:
            cursor = await self._conn.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            mode = row[0] if row else "unknown"
            if mode.lower() != "wal":
                logger.warning(f"journal_mode 未生效，当前: {mode}")
            else:
                logger.info("database.wal_enabled")
        except Exception as e:
            logger.warning(f"验证 journal_mode 失败: {e}")
        self.memory = MemoryDB(self._conn)
        await self._create_tables()
        self.notebook = NotebookDB(self._conn)
        self.learning = LearningDB(self._conn)
        self.knowledge = KnowledgeDB(self._conn)
        self.analytics = AnalyticsDB(self._conn)
        logger.info("database.ready", path=str(self.db_path))

    async def commit(self):
        if self._conn:
            await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _run_migrations(self):
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
        """)
        row = await self._conn.execute_fetchall("SELECT MAX(version) FROM schema_version")
        current = row[0][0] if row and row[0][0] is not None else 0

        if current < 1:
            try:
                await self._conn.execute("BEGIN TRANSACTION")
                await self._conn.executescript("""
                    ALTER TABLE knowledge_relations ADD COLUMN valid_from REAL DEFAULT 0;
                    ALTER TABLE knowledge_relations ADD COLUMN valid_to REAL DEFAULT 0;
                    ALTER TABLE knowledge_relations ADD COLUMN confidence REAL DEFAULT 1.0;
                """)
                await self._conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, ?)", (time.time(),))
                await self._conn.commit()
                logger.info("database.migration_v1", desc="temporal_knowledge_graph")
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                logger.error(f"数据库迁移 v1 失败: {e}")

        if current < 2:
            try:
                await self._conn.execute("BEGIN TRANSACTION")
                await self._conn.execute(
                    "ALTER TABLE conversation_logs ADD COLUMN session_id TEXT DEFAULT ''")
                await self._conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (2, ?)", (time.time(),))
                await self._conn.commit()
                logger.info("database.migration_v2", desc="conversation_logs.session_id")
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                logger.error(f"数据库迁移 v2 失败: {e}")

        if current < 3:
            try:
                await self._conn.execute("BEGIN TRANSACTION")
                await self._conn.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory_fts USING fts5(
                        id UNINDEXED,
                        summary_index
                    );
                """)
                rows = await self._conn.execute_fetchall("SELECT id, summary FROM episodic_memories")
                for row in rows:
                    from memory.memory_manager import _tokenize_for_fts
                    tokenized = _tokenize_for_fts(row[1])
                    if tokenized.strip():
                        await self._conn.execute(
                            "INSERT INTO episodic_memory_fts(id, summary_index) VALUES(?, ?)",
                            (row[0], tokenized),
                        )
                await self._conn.executescript("""
                    CREATE TABLE IF NOT EXISTS consolidation_candidates (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        source TEXT NOT NULL DEFAULT 'rule',
                        kind TEXT NOT NULL DEFAULT 'fact',
                        summary TEXT NOT NULL,
                        confidence REAL DEFAULT 0.5,
                        importance REAL DEFAULT 0.5,
                        status TEXT NOT NULL DEFAULT 'pending',
                        target_memory_id INTEGER DEFAULT -1,
                        metadata_json TEXT DEFAULT '{}',
                        created_at REAL NOT NULL
                    );
                """)
                logger.info("database.migration_v3_backfill", rows=len(rows))
                await self._conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (3, ?)", (time.time(),))
                await self._conn.commit()
                logger.info("database.migration_v3", desc="fts5_index+consolidation_candidates")
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                logger.error(f"数据库迁移 v3 失败: {e}")

        if current < 4:
            try:
                await self._conn.execute("BEGIN TRANSACTION")
                await self.memory.migrate_add_source_column()
                await self._conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (4, ?)", (time.time(),))
                await self._conn.commit()
                logger.info("database.migration_v4", desc="episodic_memories.source")
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                logger.error(f"数据库迁移 v4 失败: {e}")

        if current < 5:
            try:
                await self._conn.execute("BEGIN TRANSACTION")
                cursor = await self._conn.execute("SELECT id, name FROM knowledge_entities")
                rows = await cursor.fetchall()
                from memory.memory_manager import _tokenize_for_fts
                for row in rows:
                    name_tokenized = _tokenize_for_fts(row["name"]) if row["name"] else ""
                    await self._conn.execute(
                        "INSERT OR IGNORE INTO knowledge_entities_fts(id, name_index) VALUES (?, ?)",
                        (row["id"], name_tokenized),
                    )
                await self._conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (5, ?)", (time.time(),))
                await self._conn.commit()
                logger.info("database.migration_v5", desc="knowledge_entities_fts_backfill", rows=len(rows))
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                logger.error(f"数据库迁移 v5 失败: {e}")

        if current < 6:
            try:
                await self._conn.execute("BEGIN TRANSACTION")
                await self._conn.execute(
                    "ALTER TABLE episodic_memories ADD COLUMN access_count INTEGER DEFAULT 0"
                )
                await self._conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (6, ?)", (time.time(),))
                await self._conn.commit()
                logger.info("database.migration_v6", desc="episodic_memories.access_count")
            except Exception as e:
                await self._conn.execute("ROLLBACK")
                logger.error(f"数据库迁移 v6 失败: {e}")

        await self._conn.commit()

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        if not self._conn:
            return []
        rows = await self._conn.execute_fetchall(sql, params)
        return [dict(r) for r in rows]

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: tuple = (), auto_commit: bool = True) -> int:
        cur = await self._conn.execute(sql, params)
        if auto_commit:
            await self._conn.commit()
        return cur.lastrowid or 0

    async def _create_tables(self):
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                user_id TEXT DEFAULT '',
                source TEXT DEFAULT 'qq',
                user_message TEXT DEFAULT '',
                assistant_reply TEXT DEFAULT '',
                emotion_label TEXT DEFAULT '',
                model_used TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                user_id TEXT DEFAULT '',
                detail TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS episodic_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                summary TEXT NOT NULL,
                importance REAL DEFAULT 0.5,
                emotion_label TEXT DEFAULT '',
                session_id TEXT DEFAULT 'user',
                embedding_id INTEGER DEFAULT -1,
                source TEXT DEFAULT 'user',
                access_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cron_last_run (
                task_name TEXT PRIMARY KEY,
                last_run REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_portrait (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                version INTEGER DEFAULT 1,
                source_ids TEXT DEFAULT '',
                change_log TEXT DEFAULT '',
                created_at REAL NOT NULL
            );

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
            );

            CREATE TABLE IF NOT EXISTS proactive_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                message_type TEXT NOT NULL,
                content TEXT NOT NULL,
                sent_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_usage (
                id TEXT PRIMARY KEY,
                user_openid TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                model TEXT DEFAULT '',
                task_type TEXT DEFAULT '',
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                cache_hit_tokens INTEGER DEFAULT 0,
                cache_miss_tokens INTEGER DEFAULT 0,
                cost_usd REAL DEFAULT 0,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS greeting_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('fixed','random')),
                time TEXT DEFAULT '',
                window_start TEXT DEFAULT '',
                window_end TEXT DEFAULT '',
                count_per_day INTEGER DEFAULT 1,
                days TEXT NOT NULL DEFAULT '[1,2,3,4,5,6,7]',
                prompt_hint TEXT DEFAULT '',
                channels TEXT NOT NULL DEFAULT '["web"]',
                enabled INTEGER NOT NULL DEFAULT 1,
                next_fire_times TEXT DEFAULT '[]',
                drawn_date TEXT DEFAULT '',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS greeting_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER DEFAULT 0,
                fired_at REAL NOT NULL,
                content TEXT DEFAULT '',
                channel TEXT DEFAULT 'web',
                reason TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS media_tasks (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                prompt TEXT DEFAULT '',
                params TEXT DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                progress REAL DEFAULT 0,
                result_path TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at REAL NOT NULL,
                finished_at REAL
            );

            CREATE TABLE IF NOT EXISTS health_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at REAL NOT NULL,
                passed INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0,
                detail TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                user_openid TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                turn_count INTEGER DEFAULT 0,
                total_cost_usd REAL DEFAULT 0,
                cache_hit_tokens INTEGER DEFAULT 0,
                cache_miss_tokens INTEGER DEFAULT 0,
                started_at REAL NOT NULL,
                ended_at REAL DEFAULT 0,
                status TEXT DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS agent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                user_openid TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                detail TEXT DEFAULT '',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_entities (
                id TEXT PRIMARY KEY,
                name TEXT UNIQUE,
                kind TEXT DEFAULT '',
                observations TEXT DEFAULT '[]',
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS knowledge_relations (
                id TEXT PRIMARY KEY,
                from_entity TEXT,
                relation_type TEXT,
                to_entity TEXT,
                updated_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_conv_ts ON conversation_logs(timestamp);
            CREATE INDEX IF NOT EXISTS idx_mem_ts ON episodic_memories(timestamp);
            CREATE INDEX IF NOT EXISTS idx_mem_importance ON episodic_memories(importance);
            CREATE INDEX IF NOT EXISTS idx_portrait_created ON user_portrait(created_at);
            CREATE INDEX IF NOT EXISTS idx_notebook_kind ON notebook_entries(kind);
            CREATE INDEX IF NOT EXISTS idx_notebook_status ON notebook_entries(status);
            CREATE INDEX IF NOT EXISTS idx_notebook_due ON notebook_entries(due_date);
            CREATE INDEX IF NOT EXISTS idx_proactive_user ON proactive_messages(user_id);
            CREATE INDEX IF NOT EXISTS idx_api_usage_ts ON api_usage(created_at);
            CREATE INDEX IF NOT EXISTS idx_api_usage_user ON api_usage(user_openid);
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_openid);
            CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
            CREATE INDEX IF NOT EXISTS idx_events_type ON agent_events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_ts ON agent_events(created_at);
            CREATE INDEX IF NOT EXISTS idx_kg_entity_name ON knowledge_entities(name);
            CREATE INDEX IF NOT EXISTS idx_kg_entity_updated ON knowledge_entities(updated_at);
            CREATE INDEX IF NOT EXISTS idx_kg_rel_from ON knowledge_relations(from_entity);
            CREATE INDEX IF NOT EXISTS idx_kg_rel_to ON knowledge_relations(to_entity);
            CREATE INDEX IF NOT EXISTS idx_conv_user ON conversation_logs(user_id);
            CREATE INDEX IF NOT EXISTS idx_conv_source ON conversation_logs(source);
            CREATE INDEX IF NOT EXISTS idx_episodic_session ON episodic_memories(session_id);
            CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_logs(event_type);
            CREATE INDEX IF NOT EXISTS idx_conv_session ON conversation_logs(session_id);
            CREATE INDEX IF NOT EXISTS idx_kg_rel_type ON knowledge_relations(relation_type);
            CREATE INDEX IF NOT EXISTS idx_media_status ON media_tasks(status);

            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory_fts USING fts5(
                id UNINDEXED,
                summary_index
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_entities_fts USING fts5(
                id UNINDEXED,
                name_index
            );

            CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_ai AFTER INSERT ON knowledge_entities BEGIN
                INSERT INTO knowledge_entities_fts(id, name_index)
                VALUES (new.id, new.name);
            END;
            CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_ad AFTER DELETE ON knowledge_entities BEGIN
                INSERT INTO knowledge_entities_fts(knowledge_entities_fts, id, name_index)
                VALUES ('delete', old.id, old.name);
            END;
            CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_au AFTER UPDATE ON knowledge_entities BEGIN
                INSERT INTO knowledge_entities_fts(knowledge_entities_fts, id, name_index)
                VALUES ('delete', old.id, old.name);
                INSERT INTO knowledge_entities_fts(id, name_index)
                VALUES (new.id, new.name);
            END;

            CREATE TABLE IF NOT EXISTS consolidation_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                source TEXT NOT NULL DEFAULT 'rule',
                kind TEXT NOT NULL DEFAULT 'fact',
                summary TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                importance REAL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'pending',
                target_memory_id INTEGER DEFAULT -1,
                metadata_json TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS learnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                learning_id TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL DEFAULT 'insight',
                priority TEXT NOT NULL DEFAULT 'low',
                status TEXT NOT NULL DEFAULT 'pending',
                area TEXT DEFAULT 'backend',
                summary TEXT NOT NULL,
                details TEXT DEFAULT '',
                suggested_action TEXT DEFAULT '',
                source TEXT DEFAULT 'conversation',
                pattern_key TEXT DEFAULT '',
                recurrence_count INTEGER DEFAULT 1,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_id TEXT NOT NULL UNIQUE,
                priority TEXT NOT NULL DEFAULT 'high',
                status TEXT NOT NULL DEFAULT 'pending',
                area TEXT DEFAULT 'backend',
                summary TEXT NOT NULL,
                error_text TEXT DEFAULT '',
                context TEXT DEFAULT '',
                suggested_fix TEXT DEFAULT '',
                reproducible TEXT DEFAULT 'unknown',
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feature_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                priority TEXT NOT NULL DEFAULT 'medium',
                status TEXT NOT NULL DEFAULT 'pending',
                area TEXT DEFAULT 'backend',
                capability TEXT NOT NULL,
                user_context TEXT DEFAULT '',
                complexity TEXT DEFAULT 'medium',
                suggested_impl TEXT DEFAULT '',
                frequency TEXT DEFAULT 'first_time',
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_learnings_cat ON learnings(category);
            CREATE INDEX IF NOT EXISTS idx_learnings_status ON learnings(status);
            CREATE INDEX IF NOT EXISTS idx_learnings_pattern ON learnings(pattern_key);
            CREATE INDEX IF NOT EXISTS idx_errors_status ON errors(status);
            CREATE INDEX IF NOT EXISTS idx_featreq_status ON feature_requests(status);

            CREATE TABLE IF NOT EXISTS session_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                entry_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS session_summaries (
                session_id TEXT PRIMARY KEY,
                mtime INTEGER NOT NULL DEFAULT 0,
                summary_data TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_session_entries_sid ON session_entries(session_id);
            CREATE INDEX IF NOT EXISTS idx_session_entries_created ON session_entries(created_at);

            CREATE TABLE IF NOT EXISTS cleanup_config (
                table_name TEXT PRIMARY KEY,
                retention_days INTEGER NOT NULL,
                date_column TEXT NOT NULL DEFAULT 'timestamp',
                enabled INTEGER DEFAULT 1
            );
        """)

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
        except Exception as e:
            logger.warning(f"插入默认清理策略失败: {e}")

        await self._run_migrations()
        await self._conn.commit()

    async def insert_conversation_log(self, user_id: str, source: str,
                                       user_message: str, assistant_reply: str,
                                       emotion_label: str = "", model_used: str = "",
                                       session_id: str = "",
                                       auto_commit: bool = True):
        await self._conn.execute(
            """INSERT INTO conversation_logs
               (timestamp, user_id, source, user_message, assistant_reply, emotion_label, model_used, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), user_id, source, user_message, assistant_reply, emotion_label, model_used, session_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def insert_audit_log(self, event_type: str, user_id: str = "", detail: str = "",
                                auto_commit: bool = True):
        await self._conn.execute(
            """INSERT INTO audit_logs (timestamp, event_type, user_id, detail)
               VALUES (?, ?, ?, ?)""",
            (time.time(), event_type, user_id, detail),
        )
        if auto_commit:
            await self._conn.commit()

    async def create_session(self, user_openid: str = "", auto_commit: bool = True) -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        session_id = f"SES-{date_str}-{int(now % 100000):05d}"
        await self._conn.execute(
            """INSERT INTO sessions
               (id, user_openid, started_at, ended_at, status)
               VALUES (?, ?, ?, ?, 'active')""",
            (session_id, user_openid, now, now),
        )
        if auto_commit:
            await self._conn.commit()
        return session_id

    async def get_active_session(self, user_openid: str, idle_seconds: int = 1800) -> dict | None:
        cutoff = time.time() - idle_seconds
        cursor = await self._conn.execute(
            """SELECT * FROM sessions
               WHERE user_openid=? AND status='active' AND ended_at >= ?
               ORDER BY ended_at DESC LIMIT 1""",
            (user_openid, cutoff),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_session(self, session_id: str, cost_usd: float = 0,
                              cache_hit: int = 0, cache_miss: int = 0,
                              auto_commit: bool = True):
        now = time.time()
        await self._conn.execute(
            """UPDATE sessions
               SET turn_count = turn_count + 1,
                   total_cost_usd = total_cost_usd + ?,
                   cache_hit_tokens = cache_hit_tokens + ?,
                   cache_miss_tokens = cache_miss_tokens + ?,
                   ended_at = ?
               WHERE id=?""",
            (cost_usd, cache_hit, cache_miss, now, session_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def archive_session(self, session_id: str, summary: str = "",
                               auto_commit: bool = True):
        now = time.time()
        await self._conn.execute(
            """UPDATE sessions
               SET status='archived', summary=?, ended_at=?
               WHERE id=?""",
            (summary, now, session_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_archived_sessions(self, user_openid: str = "", limit: int = 10) -> list[dict]:
        if user_openid:
            cursor = await self._conn.execute(
                """SELECT * FROM sessions
                   WHERE user_openid=? AND status='archived'
                   ORDER BY ended_at DESC LIMIT ?""",
                (user_openid, limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT * FROM sessions
                   WHERE status='archived'
                   ORDER BY ended_at DESC LIMIT ?""",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_active_sessions(self, limit: int = 10) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM sessions
               WHERE status='active'
               ORDER BY ended_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def auto_archive_stale_sessions(self, idle_seconds: int = 3600,
                                           auto_commit: bool = True) -> int:
        cutoff = time.time() - idle_seconds
        cursor = await self._conn.execute(
            """UPDATE sessions
               SET status='archived', ended_at=?
               WHERE status='active' AND ended_at > 0 AND ended_at < ?""",
            (time.time(), cutoff),
        )
        if auto_commit:
            await self._conn.commit()
        return cursor.rowcount

    async def get_cron_last_run(self, task_name: str) -> float | None:
        cursor = await self._conn.execute(
            "SELECT last_run FROM cron_last_run WHERE task_name=?", (task_name,)
        )
        row = await cursor.fetchone()
        return row["last_run"] if row else None

    async def set_cron_last_run(self, task_name: str, ts: float | None = None,
                                 auto_commit: bool = True):
        ts = ts or time.time()
        await self._conn.execute(
            """INSERT OR REPLACE INTO cron_last_run (task_name, last_run) VALUES (?, ?)""",
            (task_name, ts),
        )
        if auto_commit:
            await self._conn.commit()

    async def log_conversation(self, user_id: str, source: str,
                                user_message: str, assistant_reply: str,
                                emotion_label: str = "", model_used: str = "",
                                session_id: str = "", cost_usd: float = 0,
                                cache_hit: int = 0, cache_miss: int = 0):
        await self.insert_conversation_log(
            user_id=user_id, source=source,
            user_message=user_message, assistant_reply=assistant_reply,
            emotion_label=emotion_label, model_used=model_used,
            auto_commit=False,
        )
        if session_id:
            await self.update_session(
                session_id, cost_usd=cost_usd,
                cache_hit=cache_hit, cache_miss=cache_miss,
                auto_commit=False,
            )
        await self._conn.commit()

    async def cleanup_expired_data(self, auto_commit: bool = True) -> dict[str, int]:
        result: dict[str, int] = {}
        if not self._conn:
            return result
        try:
            cursor = await self._conn.execute(
                "SELECT table_name, retention_days, date_column FROM cleanup_config WHERE enabled=1"
            )
            configs = await cursor.fetchall()
        except Exception:
            return result

        now = time.time()
        for row in configs:
            table_name = row["table_name"]
            retention_days = row["retention_days"]
            date_column = row["date_column"]
            cutoff = now - retention_days * 86400
            try:
                import re
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', table_name):
                    logger.warning("database.cleanup_invalid_table", table=table_name)
                    continue
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', date_column):
                    logger.warning("database.cleanup_invalid_column", column=date_column)
                    continue
                del_cursor = await self._conn.execute(
                    f'DELETE FROM "{table_name}" WHERE "{date_column}" < ? AND "{date_column}" > 0',
                    (cutoff,),
                )
                deleted = del_cursor.rowcount
                result[table_name] = deleted
                if deleted > 0:
                    logger.info("database.cleanup", table=table_name,
                                deleted=deleted, retention_days=retention_days)
            except Exception as e:
                logger.warning("database.cleanup_failed", table=table_name, error=str(e))
                result[table_name] = 0

        if auto_commit:
            try:
                await self._conn.commit()
            except Exception as e:
                logger.warning(f"清理过期数据提交事务失败: {e}")
        return result

    async def append_session_entry(self, session_id: str, entry: dict[str, Any]) -> None:
        now = time.time()
        entry_json = json.dumps(entry, ensure_ascii=False)
        await self._conn.execute(
            """INSERT INTO session_entries (session_id, entry_json, created_at)
               VALUES (?, ?, ?)""",
            (session_id, entry_json, now),
        )
        prev_summary = await self._load_summary_entry(session_id)
        new_summary = fold_session_summary(prev_summary, session_id, entry)
        new_summary.mtime = int(now * 1000)
        await self._conn.execute(
            """INSERT OR REPLACE INTO session_summaries (session_id, mtime, summary_data)
               VALUES (?, ?, ?)""",
            (session_id, new_summary.mtime, json.dumps(new_summary.data, ensure_ascii=False)),
        )
        await self._conn.commit()

    async def load_session(self, session_id: str) -> list[dict[str, Any]] | None:
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
            sid = row["id"]
            summary_text = row["summary"] or ""
            mtime = row["mtime"] or int((row["ended_at"] or row["started_at"] or 0) * 1000)

            summary_data = {}
            try:
                summary_data = json.loads(row["summary_data"]) if row["summary_data"] else {}
            except (json.JSONDecodeError, TypeError):
                pass

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
                continue

            results.append(SessionInfo(
                session_id=sid,
                summary=display_summary,
                last_modified=mtime,
                custom_title=custom_title,
                first_prompt=first_prompt,
                tag=summary_data.get("tag"),
                created_at=summary_data.get("created_at"),
            ))
        return results

    async def delete_session(self, session_id: str) -> None:
        await self._conn.execute("DELETE FROM session_entries WHERE session_id=?", (session_id,))
        await self._conn.execute("DELETE FROM session_summaries WHERE session_id=?", (session_id,))
        await self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        await self._conn.commit()

    async def rename_session(self, session_id: str, new_title: str) -> None:
        await self._conn.execute(
            "UPDATE sessions SET summary=? WHERE id=?",
            (new_title, session_id),
        )
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
        entries = await self.load_session(session_id)
        if entries is None:
            return None

        cursor = await self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
        orig = await cursor.fetchone()
        if not orig:
            return None

        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        new_id = f"SES-{date_str}-{int(now % 100000):05d}"

        await self._conn.execute(
            """INSERT INTO sessions (id, user_openid, summary, turn_count, total_cost_usd,
               cache_hit_tokens, cache_miss_tokens, started_at, ended_at, status)
               VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?, 'active')""",
            (new_id, orig["user_openid"], f"Fork of {session_id}", now, now),
        )

        for entry in entries:
            entry_json = json.dumps(entry, ensure_ascii=False)
            await self._conn.execute(
                """INSERT INTO session_entries (session_id, entry_json, created_at)
                   VALUES (?, ?, ?)""",
                (new_id, entry_json, now),
            )

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
