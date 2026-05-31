import time
import aiosqlite
from pathlib import Path
from loguru import logger
from config import DATA_DIR
from db_memory import MemoryDB
from db_notebook import NotebookDB
from db_learning import LearningDB
from db_knowledge import KnowledgeDB
from db_analytics import AnalyticsDB


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
        await self._create_tables()
        self.memory = MemoryDB(self._conn)
        self.notebook = NotebookDB(self._conn)
        self.learning = LearningDB(self._conn)
        self.knowledge = KnowledgeDB(self._conn)
        self.analytics = AnalyticsDB(self._conn)
        logger.info("database.ready", path=str(self.db_path))

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
                await self._conn.executescript("""
                    ALTER TABLE knowledge_relations ADD COLUMN valid_from REAL DEFAULT 0;
                    ALTER TABLE knowledge_relations ADD COLUMN valid_to REAL DEFAULT 0;
                    ALTER TABLE knowledge_relations ADD COLUMN confidence REAL DEFAULT 1.0;
                """)
            except Exception:
                pass
            await self._conn.execute("INSERT INTO schema_version (version, applied_at) VALUES (1, ?)", (time.time(),))
            logger.info("database.migration_v1", desc="temporal_knowledge_graph")

        await self._conn.commit()

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
                embedding_id INTEGER DEFAULT -1
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
        """)

        await self._run_migrations()
        await self._conn.commit()

    async def insert_conversation_log(self, user_id: str, source: str,
                                       user_message: str, assistant_reply: str,
                                       emotion_label: str = "", model_used: str = ""):
        await self._conn.execute(
            """INSERT INTO conversation_logs
               (timestamp, user_id, source, user_message, assistant_reply, emotion_label, model_used)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), user_id, source, user_message, assistant_reply, emotion_label, model_used),
        )
        await self._conn.commit()

    async def insert_audit_log(self, event_type: str, user_id: str = "", detail: str = ""):
        await self._conn.execute(
            """INSERT INTO audit_logs (timestamp, event_type, user_id, detail)
               VALUES (?, ?, ?, ?)""",
            (time.time(), event_type, user_id, detail),
        )
        await self._conn.commit()

    async def create_session(self, user_openid: str = "") -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        session_id = f"SES-{date_str}-{int(now % 100000):05d}"
        await self._conn.execute(
            """INSERT INTO sessions
               (id, user_openid, started_at, status)
               VALUES (?, ?, ?, 'active')""",
            (session_id, user_openid, now),
        )
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
                              cache_hit: int = 0, cache_miss: int = 0):
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
        await self._conn.commit()

    async def archive_session(self, session_id: str, summary: str = ""):
        now = time.time()
        await self._conn.execute(
            """UPDATE sessions
               SET status='archived', summary=?, ended_at=?
               WHERE id=?""",
            (summary, now, session_id),
        )
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

    async def auto_archive_stale_sessions(self, idle_seconds: int = 3600) -> int:
        cutoff = time.time() - idle_seconds
        cursor = await self._conn.execute(
            """UPDATE sessions
               SET status='archived', ended_at=?
               WHERE status='active' AND ended_at > 0 AND ended_at < ?""",
            (time.time(), cutoff),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def get_cron_last_run(self, task_name: str) -> float | None:
        cursor = await self._conn.execute(
            "SELECT last_run FROM cron_last_run WHERE task_name=?", (task_name,)
        )
        row = await cursor.fetchone()
        return row["last_run"] if row else None

    async def set_cron_last_run(self, task_name: str, ts: float | None = None):
        ts = ts or time.time()
        await self._conn.execute(
            """INSERT OR REPLACE INTO cron_last_run (task_name, last_run) VALUES (?, ?)""",
            (task_name, ts),
        )
        await self._conn.commit()