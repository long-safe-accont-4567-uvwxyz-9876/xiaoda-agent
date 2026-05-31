import os
import time
import aiosqlite
from loguru import logger


class SessionDB:

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def create_session(self, session_id: str, user_openid: str = "",
                             agent_name: str = "nahida",
                             channel_type: str = "", channel_id: str = "",
                             group_openid: str = "") -> str:
        now = time.time()
        await self._conn.execute(
            """INSERT OR IGNORE INTO sessions
               (session_id, user_openid, agent_name, channel_type, channel_id,
                group_openid, message_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (session_id, user_openid, agent_name, channel_type, channel_id,
             group_openid, now, now),
        )
        await self._conn.commit()
        return session_id

    async def touch_session(self, session_id: str, channel_type: str = "",
                            channel_id: str = "", group_openid: str = ""):
        now = time.time()
        cursor = await self._conn.execute(
            """UPDATE sessions SET message_count = message_count + 1,
               updated_at = ?, channel_type = COALESCE(NULLIF(?, ''), channel_type),
               channel_id = COALESCE(NULLIF(?, ''), channel_id),
               group_openid = COALESCE(NULLIF(?, ''), group_openid)
               WHERE session_id = ?""",
            (now, channel_type, channel_id, group_openid, session_id),
        )
        if cursor.rowcount == 0:
            await self._conn.execute(
                """INSERT INTO sessions
                   (session_id, user_openid, agent_name, channel_type, channel_id,
                    group_openid, message_count, created_at, updated_at)
                   VALUES (?, ?, 'nahida', ?, ?, ?, 1, ?, ?)""",
                (session_id, "", channel_type, channel_id, group_openid, now, now),
            )
        await self._conn.commit()

    async def get_session(self, session_id: str) -> dict | None:
        cursor = await self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_recent_sessions(self, limit: int = 20) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM sessions WHERE is_active = 1
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def deactivate_session(self, session_id: str):
        await self._conn.execute(
            "UPDATE sessions SET is_active = 0, updated_at = ? WHERE session_id = ?",
            (time.time(), session_id),
        )
        await self._conn.commit()


class APIUsageDB:

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def insert(self, user_openid: str = "", session_id: str = "",
                     agent_name: str = "nahida", model: str = "",
                     task_type: str = "", prompt_tokens: int = 0,
                     completion_tokens: int = 0, reasoning_tokens: int = 0,
                     cache_hit_tokens: int = 0, cache_miss_tokens: int = 0,
                     latency_ms: int = 0, cost_usd: float = 0.0,
                     status: str = "success") -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        usage_id = f"API-{date_str}-{int(now * 1000) % 1000000:06d}"
        try:
            await self._conn.execute(
                """INSERT INTO api_usage
                   (id, user_openid, session_id, agent_name, model, task_type,
                    prompt_tokens, completion_tokens, reasoning_tokens,
                    cache_hit_tokens, cache_miss_tokens,
                    latency_ms, cost_usd, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (usage_id, user_openid, session_id, agent_name, model, task_type,
                 prompt_tokens, completion_tokens, reasoning_tokens,
                 cache_hit_tokens, cache_miss_tokens,
                 latency_ms, cost_usd, status, now),
            )
            await self._conn.commit()
            return usage_id
        except Exception:
            return ""

    async def batch_insert(self, records: list[dict]):
        if not records:
            return
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        rows = []
        for i, r in enumerate(records):
            usage_id = f"API-{date_str}-{(int(now * 1000) + i) % 1000000:06d}"
            rows.append((
                usage_id,
                r.get("user_openid", ""),
                r.get("session_id", ""),
                r.get("agent_name", "nahida"),
                r.get("model", ""),
                r.get("task_type", ""),
                r.get("prompt_tokens", 0),
                r.get("completion_tokens", 0),
                r.get("reasoning_tokens", 0),
                r.get("cache_hit_tokens", 0),
                r.get("cache_miss_tokens", 0),
                r.get("latency_ms", 0),
                r.get("cost_usd", 0.0),
                r.get("status", "success"),
                r.get("created_at", now),
            ))
        try:
            await self._conn.executemany(
                """INSERT OR IGNORE INTO api_usage
                   (id, user_openid, session_id, agent_name, model, task_type,
                    prompt_tokens, completion_tokens, reasoning_tokens,
                    cache_hit_tokens, cache_miss_tokens,
                    latency_ms, cost_usd, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            await self._conn.commit()
        except Exception as e:
            logger.warning("db.batch_api_usage_failed", error=str(e))

    async def get_daily_stats(self, date_str: str | None = None) -> dict:
        if not date_str:
            date_str = time.strftime("%Y%m%d")
        start = time.mktime(time.strptime(date_str, "%Y%m%d"))
        end = start + 86400
        cursor = await self._conn.execute(
            """SELECT agent_name, model,
                      SUM(prompt_tokens) as prompt_t,
                      SUM(completion_tokens) as completion_t,
                      SUM(reasoning_tokens) as reasoning_t,
                      SUM(cache_hit_tokens) as cache_hit_t,
                      SUM(cache_miss_tokens) as cache_miss_t,
                      SUM(cost_usd) as cost,
                      COUNT(*) as calls,
                      AVG(latency_ms) as avg_latency
               FROM api_usage
               WHERE created_at >= ? AND created_at < ?
               GROUP BY agent_name, model""",
            (start, end),
        )
        rows = await cursor.fetchall()
        breakdown = [dict(r) for r in rows]

        cursor2 = await self._conn.execute(
            """SELECT SUM(prompt_tokens) as total_prompt,
                      SUM(completion_tokens) as total_completion,
                      SUM(reasoning_tokens) as total_reasoning,
                      SUM(cache_hit_tokens) as total_cache_hit,
                      SUM(cache_miss_tokens) as total_cache_miss,
                      SUM(cost_usd) as total_cost,
                      COUNT(*) as total_calls,
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) as error_calls
               FROM api_usage
               WHERE created_at >= ? AND created_at < ?""",
            (start, end),
        )
        totals = await cursor2.fetchone()
        total_all = ((totals["total_cache_hit"] or 0) + (totals["total_cache_miss"] or 0))
        return {
            "date": date_str,
            "total_prompt_tokens": totals["total_prompt"] or 0,
            "total_completion_tokens": totals["total_completion"] or 0,
            "total_reasoning_tokens": totals["total_reasoning"] or 0,
            "cache_hit_tokens": totals["total_cache_hit"] or 0,
            "cache_miss_tokens": totals["total_cache_miss"] or 0,
            "cache_hit_ratio": round((totals["total_cache_hit"] or 0) / total_all, 3) if total_all > 0 else 0.0,
            "total_cost_usd": round(totals["total_cost"] or 0, 6),
            "total_calls": totals["total_calls"] or 0,
            "error_calls": totals["error_calls"] or 0,
            "by_agent_model": breakdown,
        }


class LearningDB:

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def insert_learning(self, category: str, priority: str, summary: str,
                               details: str = "", suggested_action: str = "",
                               source: str = "conversation",
                               pattern_key: str = "") -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        learning_id = f"LRN-{date_str}-{int(now % 10000):04d}"
        try:
            await self._conn.execute(
                """INSERT INTO learnings
                   (learning_id, category, priority, status, summary, details,
                    suggested_action, source, pattern_key, recurrence_count,
                    first_seen, last_seen, created_at)
                   VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (learning_id, category, priority, summary, details,
                 suggested_action, source, pattern_key, now, now, now),
            )
            await self._conn.commit()
            return learning_id
        except Exception:
            return ""

    async def find_learning_by_pattern(self, pattern_key: str) -> dict | None:
        if not pattern_key:
            return None
        cursor = await self._conn.execute(
            "SELECT * FROM learnings WHERE pattern_key=? AND status='pending' LIMIT 1",
            (pattern_key,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def bump_learning_recurrence(self, learning_id: str) -> bool:
        now = time.time()
        cursor = await self._conn.execute(
            """UPDATE learnings
               SET recurrence_count = recurrence_count + 1, last_seen = ?
               WHERE learning_id=?""",
            (now, learning_id),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def resolve_learning(self, learning_id: str, resolution: str = "") -> bool:
        cursor = await self._conn.execute(
            "UPDATE learnings SET status='resolved' WHERE learning_id=?",
            (learning_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def promote_learning(self, learning_id: str, target: str = "system_prompt") -> bool:
        cursor = await self._conn.execute(
            "UPDATE learnings SET status='promoted' WHERE learning_id=?",
            (learning_id,),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def get_promoted_learnings(self) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM learnings WHERE status='promoted' ORDER BY last_seen DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def search_learnings(self, query: str = "", category: str = "",
                                status: str = "", limit: int = 20) -> list[dict]:
        conditions = []
        params = []
        if query:
            conditions.append("summary LIKE ?")
            params.append(f"%{query}%")
        if category:
            conditions.append("category=?")
            params.append(category)
        if status:
            conditions.append("status=?")
            params.append(status)
        if not conditions:
            conditions.append("1=1")
        cursor = await self._conn.execute(
            "SELECT * FROM learnings WHERE " + " AND ".join(conditions) + " ORDER BY created_at DESC LIMIT ?",
            params + [limit],
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_promotable_learnings(self, min_recurrence: int = 3) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM learnings
               WHERE status='pending' AND recurrence_count >= ?
               ORDER BY recurrence_count DESC, last_seen DESC""",
            (min_recurrence,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def insert_error(self, summary: str, error_text: str = "",
                            context: str = "", suggested_fix: str = "",
                            priority: str = "high") -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        error_id = f"ERR-{date_str}-{int(now % 10000):04d}"
        try:
            await self._conn.execute(
                """INSERT INTO errors
                   (error_id, priority, status, summary, error_text, context,
                    suggested_fix, created_at)
                   VALUES (?, ?, 'pending', ?, ?, ?, ?, ?)""",
                (error_id, priority, summary, error_text[:500], context[:300],
                 suggested_fix, now),
            )
            await self._conn.commit()
            return error_id
        except Exception:
            return ""

    async def insert_feature_request(self, capability: str, user_context: str = "",
                                      complexity: str = "medium") -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        request_id = f"FEAT-{date_str}-{int(now % 10000):04d}"
        try:
            await self._conn.execute(
                """INSERT INTO feature_requests
                   (request_id, priority, status, capability, user_context,
                    complexity, frequency, created_at)
                   VALUES (?, 'medium', 'pending', ?, ?, ?, 'first_time', ?)""",
                (request_id, capability, user_context[:300], complexity, now),
            )
            await self._conn.commit()
            return request_id
        except Exception:
            return ""


class ErrorDB:

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn
        conn.row_factory = aiosqlite.Row

    async def insert_error(self, user_openid: str = "", session_id: str = "",
                           agent_name: str = "", error_type: str = "",
                           error_message: str = "", stack_trace: str = "",
                           context: str = "", recovery_action: str = "",
                           recovery_success: bool = False,
                           latency_ms: int = 0) -> str:
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        error_id = f"ERR-{date_str}-{int(now * 1000) % 1000000:06d}"
        try:
            await self._conn.execute(
                """INSERT INTO errors
                   (id, user_openid, session_id, agent_name, error_type,
                    error_message, stack_trace, context, recovery_action,
                    recovery_success, latency_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (error_id, user_openid, session_id, agent_name, error_type,
                 error_message[:2000], stack_trace[:5000], context[:1000],
                 recovery_action, recovery_success, latency_ms, now),
            )
            await self._conn.commit()
            return error_id
        except Exception:
            return ""

    async def get_error_stats(self, hours: int = 24) -> dict:
        cutoff = time.time() - hours * 3600
        cursor = await self._conn.execute(
            """SELECT error_type, COUNT(*) as cnt,
                      SUM(CASE WHEN recovery_success THEN 1 ELSE 0 END) as recovered
               FROM errors WHERE created_at >= ?
               GROUP BY error_type ORDER BY cnt DESC""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return {"hours": hours, "by_type": [dict(r) for r in rows]}

    async def get_recent_errors(self, limit: int = 20) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM errors ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


TABLE_DEFINITIONS = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_openid TEXT DEFAULT '',
    agent_name TEXT DEFAULT 'nahida',
    channel_type TEXT DEFAULT '',
    channel_id TEXT DEFAULT '',
    group_openid TEXT DEFAULT '',
    message_count INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS api_usage (
    id TEXT PRIMARY KEY,
    user_openid TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    agent_name TEXT DEFAULT 'nahida',
    model TEXT DEFAULT '',
    task_type TEXT DEFAULT '',
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cache_hit_tokens INTEGER DEFAULT 0,
    cache_miss_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    status TEXT DEFAULT 'success',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS agent_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    user_openid TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    user_id TEXT,
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS learnings (
    learning_id TEXT PRIMARY KEY,
    category TEXT DEFAULT 'general',
    priority TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'pending',
    summary TEXT NOT NULL,
    details TEXT DEFAULT '',
    suggested_action TEXT DEFAULT '',
    source TEXT DEFAULT 'conversation',
    pattern_key TEXT DEFAULT '',
    recurrence_count INTEGER DEFAULT 1,
    first_seen REAL,
    last_seen REAL,
    resolved_at REAL,
    promoted_at REAL,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS errors (
    id TEXT PRIMARY KEY,
    user_openid TEXT DEFAULT '',
    session_id TEXT DEFAULT '',
    agent_name TEXT DEFAULT '',
    error_type TEXT DEFAULT '',
    error_message TEXT DEFAULT '',
    stack_trace TEXT DEFAULT '',
    context TEXT DEFAULT '',
    recovery_action TEXT DEFAULT '',
    recovery_success INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS knowledge_entities (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT DEFAULT '',
    observations TEXT DEFAULT '[]',
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS knowledge_relations (
    id TEXT PRIMARY KEY,
    from_entity TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    to_entity TEXT NOT NULL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS proactive_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT DEFAULT '',
    message_type TEXT DEFAULT '',
    content TEXT DEFAULT '',
    sent_at REAL
);
CREATE TABLE IF NOT EXISTS data_cleanup_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    rows_deleted INTEGER DEFAULT 0,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS performance_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    metric_value REAL DEFAULT 0.0,
    tags TEXT DEFAULT '{}',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS notebook_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT DEFAULT 'note',
    content TEXT NOT NULL,
    tags TEXT DEFAULT '',
    importance REAL DEFAULT 0.5,
    due_date REAL DEFAULT 0,
    status TEXT DEFAULT 'active',
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS feature_requests (
    request_id TEXT PRIMARY KEY,
    priority TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'pending',
    capability TEXT NOT NULL,
    user_context TEXT DEFAULT '',
    complexity TEXT DEFAULT 'medium',
    frequency TEXT DEFAULT 'first_time',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    tags TEXT DEFAULT '',
    importance REAL DEFAULT 0.5,
    access_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_openid);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_name);
CREATE INDEX IF NOT EXISTS idx_api_usage_user ON api_usage(user_openid);
CREATE INDEX IF NOT EXISTS idx_api_usage_created ON api_usage(created_at);
CREATE INDEX IF NOT EXISTS idx_api_usage_model ON api_usage(model);
CREATE INDEX IF NOT EXISTS idx_events_type ON agent_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_user ON agent_events(user_openid);
CREATE INDEX IF NOT EXISTS idx_events_created ON agent_events(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_learnings_status ON learnings(status);
CREATE INDEX IF NOT EXISTS idx_learnings_pattern ON learnings(pattern_key);
CREATE INDEX IF NOT EXISTS idx_learnings_category ON learnings(category);
CREATE INDEX IF NOT EXISTS idx_errors_type ON errors(error_type);
CREATE INDEX IF NOT EXISTS idx_errors_created ON errors(created_at);
CREATE INDEX IF NOT EXISTS idx_errors_agent ON errors(agent_name);
CREATE INDEX IF NOT EXISTS idx_knowledge_name ON knowledge_entities(name);
CREATE INDEX IF NOT EXISTS idx_relations_from ON knowledge_relations(from_entity);
CREATE INDEX IF NOT EXISTS idx_relations_to ON knowledge_relations(to_entity);
CREATE INDEX IF NOT EXISTS idx_proactive_user ON proactive_messages(user_id);
CREATE INDEX IF NOT EXISTS idx_cleanup_table ON data_cleanup_log(table_name);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON performance_metrics(metric_name);
CREATE INDEX IF NOT EXISTS idx_metrics_created ON performance_metrics(created_at);
CREATE INDEX IF NOT EXISTS idx_notebook_kind ON notebook_entries(kind);
CREATE INDEX IF NOT EXISTS idx_notebook_status ON notebook_entries(status);
CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories(tags);
"""


class Database:

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self.session: SessionDB | None = None
        self.api: APIUsageDB | None = None
        self.learning: LearningDB | None = None
        self.error: ErrorDB | None = None

    async def init(self):
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA cache_size=-32000")
        await self._conn.executescript(TABLE_DEFINITIONS)
        await self._conn.commit()
        self.session = SessionDB(self._conn)
        self.api = APIUsageDB(self._conn)
        self.learning = LearningDB(self._conn)
        self.error = ErrorDB(self._conn)
        logger.info("database.ready", path=self._db_path)

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def insert_message(self, session_id: str, role: str, content: str):
        await self._conn.execute(
            "INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)",
            (session_id, role, content),
        )
        await self._conn.commit()

    async def insert_audit_log(self, event_type: str, user_id: str, detail: str):
        await self._conn.execute(
            "INSERT INTO audit_log (event_type, user_id, detail) VALUES (?, ?, ?)",
            (event_type, user_id, detail),
        )
        await self._conn.commit()

    async def get_messages(self, session_id: str, limit: int = 50) -> list:
        cursor = await self._conn.execute(
            "SELECT role, content, created_at FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        rows = await cursor.fetchall()
        return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in reversed(rows)]

    async def insert_agent_event(self, event_type: str, user_openid: str = "",
                                  session_id: str = "", detail: str = ""):
        await self._conn.execute(
            """INSERT INTO agent_events (event_type, user_openid, session_id, detail, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (event_type, user_openid, session_id, detail, time.time()),
        )
        await self._conn.commit()

    async def insert_performance_metric(self, metric_name: str, metric_value: float,
                                         tags: str = "{}"):
        await self._conn.execute(
            """INSERT INTO performance_metrics (metric_name, metric_value, tags, created_at)
               VALUES (?, ?, ?, ?)""",
            (metric_name, metric_value, tags, time.time()),
        )
        await self._conn.commit()

    async def cleanup_old_data(self, table_name: str, days: int = 30) -> int:
        cutoff = time.time() - days * 86400
        cursor = await self._conn.execute(
            f"DELETE FROM {table_name} WHERE created_at < ?", (cutoff,)
        )
        deleted = cursor.rowcount
        if deleted > 0:
            await self._conn.execute(
                """INSERT INTO data_cleanup_log (table_name, rows_deleted, created_at)
                   VALUES (?, ?, ?)""",
                (table_name, deleted, time.time()),
            )
            await self._conn.commit()
        return deleted

    async def __aenter__(self):
        await self.init()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
