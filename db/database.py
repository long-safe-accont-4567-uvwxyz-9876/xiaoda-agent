import json
import sys
import time
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
from .index_manager import build_default_index_manager
from .session_store import (
    SessionInfo,
    SessionSummaryEntry,
    fold_session_summary,
)


DB_DIR = DATA_DIR
DB_PATH = DB_DIR / "agent.db"
CURRENT_SCHEMA_VERSION = 12


def _detect_fs_type(path: Path) -> str:
    """检测路径所在文件系统类型（如 ext4/vfat/exfat/ntfs）。失败返回空串。"""
    try:
        p = path.resolve()
        # Windows: 用 ctypes 获取卷文件系统类型
        if sys.platform == "win32":
            try:
                import ctypes
                root = ctypes.create_unicode_buffer(260)
                ctypes.windll.kernel32.GetVolumePathNameW(str(p), root, 260)
                fs_buf = ctypes.create_unicode_buffer(260)
                ctypes.windll.kernel32.GetVolumeInformationW(
                    root, None, 0, None, None, None, fs_buf, 260)
                return fs_buf.value.lower()
            except (OSError, ValueError):
                logger.debug("database.detect_fs_type_windows_error: {}", exc_info=True)
                return ""
        # Linux: 读取 /proc/mounts
        while not p.is_mount() and p != p.parent:
            p = p.parent
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == str(p):
                    return parts[2]
    except (OSError, ValueError):
        logger.debug("database.detect_fs_type_error: {}", exc_info=True)
    return ""


class DatabaseManager:
    """管理 SQLite 数据库连接与各子 DB 模块的生命周期。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None
        self.memory: MemoryDB | None = None
        self.notebook: NotebookDB | None = None
        self.learning: LearningDB | None = None
        self.knowledge: KnowledgeDB | None = None
        self.analytics: AnalyticsDB | None = None

    async def init(self) -> None:
        # 幂等性：如果已有活跃连接，先关闭旧连接再创建新连接
        if self._conn is not None:
            try:
                await self._conn.close()
            except (OSError, RuntimeError):
                logger.debug("database.init_close_old_connection_error: {}", exc_info=True)
            self._conn = None
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        # busy_timeout 必须最先设置，防止后续 PRAGMA 因锁竞争失败
        try:
            await self._conn.execute("PRAGMA busy_timeout=5000")
        except (OSError, RuntimeError) as e:
            logger.warning(f"PRAGMA busy_timeout 失败: {e}")
        # vfat/exfat 不支持 WAL 共享内存和 FTS5 delete 命令，必须用 DELETE 模式
        # NTFS 完全支持 WAL 和 FTS5，不需要特殊处理
        fs_type = _detect_fs_type(self.db_path)
        self._is_fat_fs = fs_type in ("vfat", "fat", "msdos", "exfat", "fat32")
        if self._is_fat_fs:
            logger.info(f"database.fat_fs_detected fs={fs_type} → 使用 DELETE journal_mode, 禁用 FTS5 触发器")
        journal_mode_sql = "PRAGMA journal_mode=DELETE" if self._is_fat_fs else "PRAGMA journal_mode=WAL"
        pragmas = [
            journal_mode_sql,
            "PRAGMA synchronous=NORMAL",
            "PRAGMA cache_size=-20000",      # ~20MB
            "PRAGMA temp_store=MEMORY",
        ]
        # mmap 在 vfat 上不可靠，仅在非 fat 文件系统启用
        if not self._is_fat_fs:
            pragmas.append("PRAGMA mmap_size=67108864")  # 64MB
        for pragma_sql in pragmas:
            try:
                await self._conn.execute(pragma_sql)
            except (OSError, RuntimeError) as e:
                logger.warning(f"PRAGMA 失败: {pragma_sql} - {e}")
        # 验证 journal_mode
        try:
            cursor = await self._conn.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            mode = row[0] if row else "unknown"
            expected = "delete" if self._is_fat_fs else "wal"
            if mode.lower() != expected:
                logger.warning(f"journal_mode 未生效，期望={expected} 当前={mode}")
            else:
                logger.info(f"database.journal_mode={mode}")
        except (OSError, RuntimeError) as e:
            logger.warning(f"验证 journal_mode 失败: {e}")
        self.memory = MemoryDB(self._conn)
        await self._create_tables()
        # Phase 6: 创建复合索引 (P2 性能优化)
        # 必须在 _create_tables 之后, 因为复合索引依赖迁移后的列 (如 confidence/session_id)
        await self._apply_composite_indexes()
        self.notebook = NotebookDB(self._conn)
        self.learning = LearningDB(self._conn)
        self.knowledge = KnowledgeDB(self._conn)
        self.analytics = AnalyticsDB(self._conn)
        logger.info("database.ready", path=str(self.db_path))

    async def _apply_composite_indexes(self) -> None:
        """创建内置复合索引 (幂等)

        与 _create_indexes (单列索引) 互补, 专注于 WHERE 多列组合的复合索引。
        使用 IndexManager.apply() 内部以 CREATE INDEX IF NOT EXISTS 保证幂等。
        """
        try:
            mgr = build_default_index_manager()
            count = await mgr.apply(self._conn)
            logger.info(f"database.composite_indexes applied={count}")
        except (OSError, RuntimeError) as e:
            # 复合索引失败不应阻塞数据库初始化
            logger.warning(f"database.composite_indexes_failed: {e}")

    async def commit(self) -> None:
        if self._conn:
            await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _run_migrations(self) -> None:
        """按 version 顺序执行所有数据库迁移。每个迁移独立事务，失败时 fail-fast 阻止启动。"""
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS migration_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                dirty INTEGER NOT NULL DEFAULT 0,
                last_version INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT ''
            );
            INSERT OR IGNORE INTO migration_state (id, dirty, last_version, last_error)
            VALUES (1, 0, 0, '');
        """)

        # Dirty state 检测：上次迁移未完成则阻止启动
        state_row = await self._conn.execute_fetchall(
            "SELECT dirty, last_version, last_error FROM migration_state WHERE id = 1"
        )
        if state_row and state_row[0][0] == 1:
            last_ver = state_row[0][1]
            last_err = state_row[0][2]
            logger.critical(
                f"⚠️ 数据库处于 dirty 状态！上次迁移 v{last_ver} 未完成：{last_err}\n"
                f"应用无法启动以避免读到不匹配的 schema。\n"
                f"修复方案：\n"
                f"  1. 检查错误原因并修复数据库\n"
                f"  2. 如已手动修复: python -m db.repair_migration --mark-clean\n"
                f"  3. 如需回滚: python -m db.repair_migration --rollback {last_ver}\n"
            )
            # 关闭连接后退出，避免连接泄漏
            try:
                await self._conn.close()
            except (OSError, RuntimeError):
                logger.warning("database.migration_dirty_close_error: {}", exc_info=True)
            sys.exit(1)

        row = await self._conn.execute_fetchall("SELECT MAX(version) FROM schema_version")
        current = row[0][0] if row and row[0][0] is not None else 0

        # (version, description, migrate_fn) 三元组列表
        migrations = [
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
        ]
        for version, desc, migrate_fn in migrations:
            if current < version:
                await self._apply_migration(version, desc, migrate_fn)
        await self._conn.commit()

    async def _apply_migration(self, version: int, description: str, migrate_fn: Any) -> None:
        """执行单个迁移：标记 dirty → BEGIN TRANSACTION → migrate_fn → INSERT schema_version → commit → 清除 dirty。

        失败时 ROLLBACK 并 fail-fast（sys.exit(1)），dirty state 持久化阻止下次启动。
        """
        # 标记 dirty（独立事务，确保迁移失败后 dirty 状态持久化）
        await self._conn.execute(
            "UPDATE migration_state SET dirty = 1, last_version = ?, last_error = '' WHERE id = 1",
            (version,),
        )
        await self._conn.commit()

        try:
            await self._conn.execute("BEGIN TRANSACTION")
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
            logger.info(f"database.migration_v{version}", desc=description)
        except (OSError, RuntimeError) as e:
            err_msg = str(e)
            # 不需要 ROLLBACK：未使用显式事务，executescript 自行管理原子性
            # 记录错误到 dirty state（独立事务）
            try:
                await self._conn.execute(
                    "UPDATE migration_state SET dirty = 1, last_version = ?, last_error = ? WHERE id = 1",
                    (version, err_msg[:500]),
                )
                await self._conn.commit()
            except (OSError, RuntimeError):
                logger.warning("database.migration_dirty_record_error: {}", exc_info=True)
            logger.critical(
                f"❌ 数据库迁移 v{version} 失败: {err_msg}\n"
                f"数据库处于 dirty 状态，应用无法启动。\n"
                f"修复方案：\n"
                f"  1. 检查错误原因\n"
                f"  2. 如需回滚: python -m db.repair_migration --rollback {version}\n"
                f"  3. 如已手动修复: python -m db.repair_migration --mark-clean\n"
            )
            try:
                await self._conn.close()
            except (OSError, RuntimeError):
                logger.warning("database.migration_failure_close_error: {}", exc_info=True)
            sys.exit(1)

    async def _migrate_v1(self) -> None:
        """v1: knowledge_relations 新增时间字段（valid_from/valid_to/confidence）。"""
        await self._conn.executescript("""
            ALTER TABLE knowledge_relations ADD COLUMN valid_from REAL DEFAULT 0;
            ALTER TABLE knowledge_relations ADD COLUMN valid_to REAL DEFAULT 0;
            ALTER TABLE knowledge_relations ADD COLUMN confidence REAL DEFAULT 1.0;
        """)

    async def _migrate_v2(self) -> None:
        """v2: conversation_logs 新增 session_id 列。"""
        await self._conn.execute(
            "ALTER TABLE conversation_logs ADD COLUMN session_id TEXT DEFAULT ''")

    async def _migrate_v3(self) -> None:
        """v3: 创建 FTS5 虚拟表 + 回填已有记忆到 FTS 索引 + 创建审计表。"""
        # 创建 FTS5 虚拟表
        await self._conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS episodic_memory_fts USING fts5(
                id UNINDEXED,
                summary_index
            );
        """)
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
        cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "access_count" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN access_count INTEGER DEFAULT 0"
            )

    async def _migrate_v7(self) -> None:
        """v7: 修复旧版 episodic_memories 缺少 session_id 和 embedding_id 列。

        新安装时 CREATE TABLE 已包含这些列，需先检查再添加。
        """
        cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "session_id" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN session_id TEXT DEFAULT 'user'"
            )
        if "embedding_id" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN embedding_id INTEGER DEFAULT -1"
            )

    async def _migrate_v8(self) -> None:
        """v8: episodic_memories 新增 RAG 同步相关列（rag_status/rag_synced_at/doc_id）。"""
        cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "rag_status" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN rag_status TEXT DEFAULT 'pending'"
            )
        if "rag_synced_at" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN rag_synced_at REAL DEFAULT 0"
            )
        if "doc_id" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN doc_id TEXT DEFAULT ''"
            )

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
        cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "distilled" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN distilled INTEGER DEFAULT 0"
            )

    async def _migrate_v10(self) -> None:
        """v10: 记忆结构化提取：新增 entities/event_type/metadata_json 列。"""
        cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "entities" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN entities TEXT DEFAULT ''"
            )
        if "event_type" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN event_type TEXT DEFAULT ''"
            )
        if "metadata_json" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN metadata_json TEXT DEFAULT '{}'"
            )

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
        cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "content_hash" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN content_hash TEXT DEFAULT ''"
            )
        if "version" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN version INTEGER DEFAULT 1"
            )
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
    # SQL 注入防护：允许的 SQL 前缀白名单（仅 SELECT / PRAGMA 只读操作）
    _READONLY_PREFIXES = ("SELECT", "PRAGMA")

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        """通用只读查询，供 Web UI 等外部层使用。返回 dict 列表。"""
        if not self._conn:
            return []
        # 安全校验：仅允许 SELECT / PRAGMA，拦截写操作
        normalized = sql.strip().upper()
        if not any(normalized.startswith(p) for p in self._READONLY_PREFIXES):
            logger.error("db.fetch_all.blocked_write_sql", sql=sql[:120])
            raise ValueError(f"fetch_all 仅允许只读查询(SELECT/PRAGMA)，收到：{normalized[:30]}")
        rows = await self._conn.execute_fetchall(sql, params)
        return [dict(r) for r in rows]

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: tuple = (), auto_commit: bool = True) -> int:
        """通用写语句。INSERT 返回 lastrowid，UPDATE/DELETE 返回 rowcount。"""
        cur = await self._conn.execute(sql, params)
        if auto_commit:
            await self._conn.commit()
        # INSERT 返回 lastrowid，UPDATE/DELETE 返回 rowcount
        if sql.strip().upper().startswith("INSERT"):
            return cur.lastrowid or 0
        return cur.rowcount

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

    async def _ddl_memory_tables(self) -> None:
        """建表：对话/记忆/笔记相关表。"""
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
                rag_status TEXT DEFAULT 'pending',
                rag_synced_at REAL DEFAULT 0,
                doc_id TEXT DEFAULT '',
                source TEXT DEFAULT 'user',
                access_count INTEGER DEFAULT 0,
                distilled INTEGER DEFAULT 0,
                entities TEXT DEFAULT '',
                event_type TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS memory_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_text TEXT NOT NULL,
                created_at REAL NOT NULL,
                memory_count INTEGER DEFAULT 0
            );

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
            );        """)

    async def _ddl_schedule_api_tables(self) -> None:
        """建表：调度/API/会话相关表。"""
        await self._ddl_schedule_greeting_tables()
        await self._ddl_api_media_tables()
        await self._ddl_session_agent_tables()

    async def _ddl_schedule_greeting_tables(self) -> None:
        """建表：调度与问候相关表（cron_last_run / greeting_schedules / greeting_log）。"""
        await self._conn.executescript("""

            CREATE TABLE IF NOT EXISTS cron_last_run (
                task_name TEXT PRIMARY KEY,
                last_run REAL NOT NULL
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
            );        """)

    async def _ddl_api_media_tables(self) -> None:
        """建表：API 用量/媒体任务/健康报告相关表。"""
        await self._conn.executescript("""

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
            );        """)

    async def _ddl_session_agent_tables(self) -> None:
        """建表：会话与事件相关表（sessions / agent_events）。"""
        await self._conn.executescript("""

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
            );        """)

    async def _ddl_knowledge_tables(self) -> None:
        """建表：知识图谱/FTS5 相关表。"""
        await self._conn.executescript("""


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
            );        """)

    async def _ddl_learning_error_tables(self) -> None:
        """建表：学习/错误/清理相关表。"""
        await self._conn.executescript("""


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

            CREATE TABLE IF NOT EXISTS cleanup_config (
                table_name TEXT PRIMARY KEY,
                retention_days INTEGER NOT NULL,
                date_column TEXT NOT NULL DEFAULT 'timestamp',
                enabled INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS tool_error_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_name TEXT NOT NULL,
                pattern TEXT NOT NULL,
                rule_text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                hit_count INTEGER DEFAULT 0
            );        """)

    async def _create_indexes(self) -> None:
        """Phase 3: 创建所有索引（含依赖迁移列的索引）。"""
        await self._conn.executescript("""
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
            CREATE INDEX IF NOT EXISTS idx_learnings_cat ON learnings(category);
            CREATE INDEX IF NOT EXISTS idx_learnings_status ON learnings(status);
            CREATE INDEX IF NOT EXISTS idx_learnings_pattern ON learnings(pattern_key);
            CREATE INDEX IF NOT EXISTS idx_errors_status ON errors(status);
            CREATE INDEX IF NOT EXISTS idx_featreq_status ON feature_requests(status);
            CREATE INDEX IF NOT EXISTS idx_session_entries_sid ON session_entries(session_id);
            CREATE INDEX IF NOT EXISTS idx_session_entries_created ON session_entries(created_at);
            CREATE INDEX IF NOT EXISTS idx_tool_error_rules_tool ON tool_error_rules(tool_name);
        """)

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
            logger.warning(f"插入默认清理策略失败: {e}")

    async def _setup_fts5_triggers(self) -> None:
        """Phase 5: FTS5 触发器管理。vfat/exfat 上禁用（delete 命令不工作）。"""
        if getattr(self, "_is_fat_fs", False):
            # 删除可能存在的触发器（防止之前版本创建的触发器残留）
            for trig in ["knowledge_entities_fts_ai", "knowledge_entities_fts_ad", "knowledge_entities_fts_au"]:
                try:
                    await self._conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
                except (OSError, RuntimeError):
                    logger.debug("database.fts5_trigger_drop_error: {}", exc_info=True)
            logger.info("database.fts5_triggers_disabled (vfat)")
            return
        # 非 fat 文件系统：创建 FTS5 触发器
        try:
            await self._conn.executescript("""
                CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_ai AFTER INSERT ON knowledge_entities BEGIN
                    INSERT INTO knowledge_entities_fts(id, name_index) VALUES (new.id, new.name);
                END;
                CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_ad AFTER DELETE ON knowledge_entities BEGIN
                    INSERT INTO knowledge_entities_fts(knowledge_entities_fts, id, name_index)
                    VALUES ('delete', old.id, old.name);
                END;
                CREATE TRIGGER IF NOT EXISTS knowledge_entities_fts_au AFTER UPDATE ON knowledge_entities BEGIN
                    INSERT INTO knowledge_entities_fts(knowledge_entities_fts, id, name_index)
                    VALUES ('delete', old.id, old.name);
                    INSERT INTO knowledge_entities_fts(id, name_index) VALUES (new.id, new.name);
                END;
            """)
        except (OSError, RuntimeError) as e:
            logger.warning(f"database.fts5_trigger_failed: {e} — FTS搜索将降级为LIKE查询")
    async def insert_conversation_log(self, user_id: str, source: str,
                                       user_message: str, assistant_reply: str,
                                       emotion_label: str = "", model_used: str = "",
                                       session_id: str = "",
                                       auto_commit: bool = True) -> None:
        await self._conn.execute(
            """INSERT INTO conversation_logs
               (timestamp, user_id, source, user_message, assistant_reply, emotion_label, model_used, session_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), user_id, source, user_message, assistant_reply, emotion_label, model_used, session_id),
        )
        if auto_commit:
            await self._conn.commit()

    async def insert_audit_log(self, event_type: str, user_id: str = "", detail: str = "",
                                auto_commit: bool = True) -> None:
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
                              auto_commit: bool = True) -> None:
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
                               auto_commit: bool = True) -> None:
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
                                 auto_commit: bool = True) -> None:
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
                                cache_hit: int = 0, cache_miss: int = 0) -> None:
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
        """按 cleanup_config 表中的策略清理过期数据。返回各表删除行数。"""
        result: dict[str, int] = {}
        if not self._conn:
            return result
        try:
            cursor = await self._conn.execute(
                "SELECT table_name, retention_days, date_column FROM cleanup_config WHERE enabled=1"
            )
            configs = await cursor.fetchall()
        except (OSError, ValueError):
            logger.debug("database.cleanup_config_read_error: {}", exc_info=True)
            return result

        now = time.time()
        for row in configs:
            table_name = row["table_name"]
            retention_days = row["retention_days"]
            date_column = row["date_column"]
            cutoff = now - retention_days * 86400
            try:
                # 白名单校验表名和列名，防止 SQL 注入
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
            except (OSError, RuntimeError) as e:
                logger.warning("database.cleanup_failed", table=table_name, error=str(e))
                result[table_name] = 0

        if auto_commit:
            try:
                await self._conn.commit()
            except (OSError, RuntimeError) as e:
                logger.warning(f"清理过期数据提交事务失败: {e}")
        return result

    # ── SessionStoreProtocol 实现 ──────────────────────────────────

    async def append_session_entry(self, session_id: str, entry: dict[str, Any]) -> None:
        """追加一条会话条目，并增量折叠摘要"""
        now = time.time()
        entry_json = json.dumps(entry, ensure_ascii=False)
        await self._conn.execute(
            """INSERT INTO session_entries (session_id, entry_json, created_at)
               VALUES (?, ?, ?)""",
            (session_id, entry_json, now),
        )

        # 加载已有摘要
        prev_summary = await self._load_summary_entry(session_id)

        # 增量折叠
        new_summary = fold_session_summary(prev_summary, session_id, entry)
        new_summary.mtime = int(now * 1000)

        # 持久化摘要
        await self._conn.execute(
            """INSERT OR REPLACE INTO session_summaries (session_id, mtime, summary_data)
               VALUES (?, ?, ?)""",
            (session_id, new_summary.mtime, json.dumps(new_summary.data, ensure_ascii=False)),
        )
        await self._conn.commit()

    async def load_session(self, session_id: str) -> list[dict[str, Any]] | None:
        """加载完整会话条目列表"""
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
        """列出所有会话（含增量摘要信息）"""
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

            # 尝试从增量摘要中获取更丰富的信息
            summary_data = {}
            try:
                summary_data = json.loads(row["summary_data"]) if row["summary_data"] else {}
            except (json.JSONDecodeError, TypeError):
                logger.debug("database.summary_data_parse_failed", exc_info=True)

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
        """删除会话及其所有条目和摘要"""
        await self._conn.execute("DELETE FROM session_entries WHERE session_id=?", (session_id,))
        await self._conn.execute("DELETE FROM session_summaries WHERE session_id=?", (session_id,))
        await self._conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        await self._conn.commit()

    async def rename_session(self, session_id: str, new_title: str) -> None:
        """重命名会话（更新 custom_title）"""
        # 更新 sessions 表的 summary
        await self._conn.execute(
            "UPDATE sessions SET summary=? WHERE id=?",
            (new_title, session_id),
        )
        # 更新增量摘要中的 custom_title
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
        """为会话添加标签"""
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
        """Fork 一个会话，返回新会话 ID"""
        # 加载原始会话条目
        entries = await self.load_session(session_id)
        if entries is None:
            return None

        # 获取原始会话信息
        cursor = await self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,))
        orig = await cursor.fetchone()
        if not orig:
            return None

        # 创建新会话
        now = time.time()
        date_str = time.strftime("%Y%m%d", time.localtime(now))
        new_id = f"SES-{date_str}-{int(now % 100000):05d}"

        await self._conn.execute(
            """INSERT INTO sessions (id, user_openid, summary, turn_count, total_cost_usd,
               cache_hit_tokens, cache_miss_tokens, started_at, ended_at, status)
               VALUES (?, ?, ?, 0, 0, 0, 0, ?, ?, 'active')""",
            (new_id, orig["user_openid"], f"Fork of {session_id}", now, now),
        )

        # 复制所有条目
        for entry in entries:
            entry_json = json.dumps(entry, ensure_ascii=False)
            await self._conn.execute(
                """INSERT INTO session_entries (session_id, entry_json, created_at)
                   VALUES (?, ?, ?)""",
                (new_id, entry_json, now),
            )

        # 复制摘要
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
        """从 session_summaries 表加载摘要条目"""
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
