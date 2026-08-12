import asyncio
import contextlib
import json
import sqlite3
import sys
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from config import DATA_DIR

from . import db_workflow
from .db_analytics import AnalyticsDB
from .db_kg_v2 import KnowledgeDBV2
from .db_knowledge import KnowledgeDB
from .db_learning import LearningDB
from .db_local_ai import LocalAIDB
from .db_memory import MemoryDB
from .db_notebook import NotebookDB
from .db_temporal_memory import TemporalMemoryDB
from .index_manager import build_default_index_manager
from .profile_store import ProfileStore
from .session_store import (
    SessionInfo,
    SessionSummaryEntry,
    fold_session_summary,
)

DB_DIR = DATA_DIR
DB_PATH = DB_DIR / "agent.db"
CURRENT_SCHEMA_VERSION = 27


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

    def _db_ro_uri(self) -> str:
        """生成 SQLite 只读连接 URI（跨平台安全）。

        Windows 路径含盘符+反斜杠（C:\\data\\agent.db），若用 f"file:{path}" 拼接，
        SQLite URI 会把 "C:" 解析为 authority 导致连接失败 → 读池/只读连接失效 →
        检索回退主写连接（本次阻塞修复在 Windows 上失效）。Path.as_uri() 生成
        file:///C:/data/agent.db 标准形式；Linux 生成 file:///home/...，统一安全。
        """
        p = self.db_path if self.db_path.is_absolute() else self.db_path.resolve()
        return p.as_uri() + "?mode=ro"

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None
        # 独立只读连接：供主请求关键路径(restore_from_db)使用，WAL模式下只读连接
        # 不被写阻塞，永不受主连接脏事务/长操作影响(上下文丢失根因修复)
        self._readonly_conn: aiosqlite.Connection | None = None
        self._profile_conn: aiosqlite.Connection | None = None
        # 只读连接池：记忆检索 7 路通道并发时若全部排队同一个主连接(self._conn，
        # aiosqlite 单连接串行执行器)，总耗时 = 各通道耗时之和（实测 7.4s=73+1018+
        # 5541+762 之和铁证）。为检索分流到独立只读连接，让通道真正并行，
        # 总耗时收敛到最慢通道（P0 性能根因修复 2026-08-07）。
        self._read_pool: list[aiosqlite.Connection] = []
        self._read_idx = 0
        self._READ_POOL_SIZE = 8
        # 写事务串行化锁：aiosqlite 单连接共享事务状态，多个后台任务并发执行
        # auto_commit=False 多语句序列时，A 的 commit() 会提交 B 未完成的半事务，
        # B 的 rollback() 会回滚 A 已写的数据 → 脏事务/数据丢失/SQL logic error
        # （历史"上下文丢失/卡顿58s"的真正根因）。所有多语句写事务必须经过
        # write_transaction() 获取此锁，单语句 auto_commit=True 操作无需加锁（aiosqlite 原子）。
        self._write_tx_lock: asyncio.Lock = asyncio.Lock()
        self._write_tx_active: ContextVar[bool] = ContextVar(
            f"database_write_tx_active_{id(self)}", default=False
        )
        self._profile_write_lock: asyncio.Lock = asyncio.Lock()
        self.memory: MemoryDB | None = None
        self.notebook: NotebookDB | None = None
        self.learning: LearningDB | None = None
        self.knowledge: KnowledgeDB | None = None
        self.analytics: AnalyticsDB | None = None
        self.temporal: TemporalMemoryDB | None = None
        self.profiles: ProfileStore | None = None
        self.kg_v2: KnowledgeDBV2 | None = None
        self.local_ai: LocalAIDB | None = None

    async def init(self) -> None:
        # 幂等性：如果已有活跃连接，先关闭旧连接再创建新连接
        if self._profile_conn is not None:
            try:
                await self._profile_conn.close()
            except (OSError, RuntimeError):
                logger.debug("database.init_close_old_profile_connection_error", exc_info=True)
            self._profile_conn = None
            self.profiles = None
        if self._conn is not None:
            try:
                await self._conn.close()
            except (OSError, RuntimeError):
                logger.debug("database.init_close_old_connection_error: {}", exc_info=True)
            self._conn = None

        # 只读文件系统检测：提前检测，避免在只读文件系统上打开连接导致静默失败
        # SQLite 在只读文件系统上打开连接不会报错，但后续写入会失败
        try:
            _db_dir = self.db_path.parent
            _probe_file = _db_dir / ".db_write_probe"
            _probe_file.write_text("probe", encoding="utf-8")
            _probe_file.unlink(missing_ok=True)
        except (OSError, PermissionError) as e:
            logger.critical(f"database.readonly_fs db_path={self.db_path} error={e}")
            raise OSError(
                f"数据库目录不可写: {self.db_path.parent}. "
                f"请检查文件系统权限或卸载/重新挂载外置存储。"
            ) from e

        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        # busy_timeout 必须最先设置，防止后续 PRAGMA 因锁竞争失败
        # 5000→15000：greeting_scheduler/memory_encoding/portrait 等后台任务并发写入时，
        # 5s 不够等待锁释放，导致 create_session 失败 → QQ 消息处理中断，用户收不到回复
        try:
            await self._conn.execute("PRAGMA busy_timeout=15000")
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
            "PRAGMA foreign_keys=ON",
            journal_mode_sql,
            "PRAGMA synchronous=NORMAL",
            "PRAGMA cache_size=-20000",      # ~20MB
            "PRAGMA temp_store=MEMORY",
        ]
        # mmap 在 vfat 上不可靠，仅在非 fat 文件系统启用
        if not self._is_fat_fs:
            pragmas.append("PRAGMA mmap_size=67108864")  # 64MB
            # WAL checkpoint 阈值 1000→10000 页（4MB→40MB）：
            # agent.db 位于 U 盘时，每 4MB 触发一次 checkpoint 会在外置盘上
            # 频繁写回 208MB 主库 + fsync，造成检索/写入偶发秒级阻塞（连锁排队根因）。
            # 40MB 阈值把 checkpoint 频率降到 1/10，仅在写入高峰后触发一次，大幅减少对
            # U 盘 IO 的周期干扰。
            pragmas.append("PRAGMA wal_autocheckpoint=10000")
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
        self.temporal = TemporalMemoryDB(self._conn, self.write_transaction)
        self._profile_conn = await aiosqlite.connect(str(self.db_path))
        self._profile_conn.row_factory = aiosqlite.Row
        await self._profile_conn.execute("PRAGMA busy_timeout=15000")
        await self._profile_conn.execute("PRAGMA foreign_keys=ON")
        self.profiles = ProfileStore(self._profile_conn, self.profile_write_transaction)
        self.kg_v2 = KnowledgeDBV2(self._conn)
        # LocalAIDB 必须在 _run_migrations 之后初始化（迁移 v25 才会创建表），
        # 但 init() 顺序是先 _create_tables（含迁移）再赋值子模块，所以此处安全。
        self.local_ai = LocalAIDB(self._conn, self.write_transaction)
        # 初始化独立只读连接(供 restore_from_db 使用)
        # WAL 模式下只读连接可与主连接并发读，永不被写事务阻塞
        # CodeRabbit 修复：init() 可能被重复调用，先关闭旧 _readonly_conn 防止连接泄漏
        if self._readonly_conn is not None:
            try:
                await self._readonly_conn.close()
            except (OSError, RuntimeError):
                logger.debug("database.init_close_old_readonly_error", exc_info=True)
            self._readonly_conn = None
        try:
            self._readonly_conn = await aiosqlite.connect(
                self._db_ro_uri(), uri=True)
            self._readonly_conn.row_factory = aiosqlite.Row
            await self._readonly_conn.execute("PRAGMA query_only=1")
            await self._readonly_conn.execute("PRAGMA busy_timeout=2000")
            logger.info("database.readonly_conn_ready")
        except Exception as e:
            # 只读连接初始化失败不阻塞启动，restore 回退到主连接(保留原行为)
            logger.warning("database.readonly_conn_init_failed", error=str(e))
            self._readonly_conn = None
        # 只读连接池（检索并发分流）：WAL 下多连接可并发读
        # 幂等：重复 init() 先关闭旧池
        if self._read_pool:
            for _c in self._read_pool:
                try:
                    await _c.close()
                except (OSError, RuntimeError):
                    pass
            self._read_pool = []
        for _ in range(self._READ_POOL_SIZE):
            try:
                _rc = await aiosqlite.connect(self._db_ro_uri(), uri=True)
                _rc.row_factory = aiosqlite.Row
                await _rc.execute("PRAGMA query_only=1")
                # 6000（原 5000）：检索 7 路通道并发 + 后台任务共享只读池，
                # 适度放宽锁等待避免偶发失败；仍 < 检索超时 8s，防止超时取消后
                # 孤儿 SQL 长时间占用连接线程（导致后续 DB 操作连锁排队）
                await _rc.execute("PRAGMA busy_timeout=6000")
                self._read_pool.append(_rc)
            except Exception as e:
                logger.warning("database.read_pool_conn_failed", error=str(e))
                break
        self._read_idx = 0
        if self._read_pool:
            logger.info("database.read_pool_ready", size=len(self._read_pool))
        if self.memory is not None:
            self.memory._read_pool = self._read_pool
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
            try:
                await self._conn.commit()
            except (OSError, RuntimeError) as e:
                logger.warning(f"database.commit_failed: {e}")

    async def rollback(self) -> None:
        """回滚当前事务，清理脏事务残留。

        根因：aiosqlite 单连接共享事务状态，auto_commit=False 操作异常/超时未 rollback
        会残留脏事务，导致后续 DB 操作在脏事务上卡死 58s(上下文丢失/卡顿根因)。
        所有 auto_commit=False 路径失败时必须调用此方法。
        """
        if self._conn:
            try:
                await self._conn.rollback()
            except (OSError, RuntimeError) as e:
                logger.warning(f"database.rollback_failed: {e}")

    @contextlib.asynccontextmanager
    async def write_transaction(self):
        """串行化多语句写事务，根治并发脏事务。

        根因：aiosqlite 单条连接共享事务状态。两个后台任务并发执行
        auto_commit=False 多语句序列时，A 的 commit() 会提交 B 未完成的半事务，
        或 B 的 rollback() 会回滚 A 已写的数据 → 脏事务/数据丢失/SQL logic error
        （历史"上下文丢失/大面积卡顿58s"的真正根因，shield(rollback)/readonly_conn
        只是治标）。本方法用 asyncio.Lock 串行化所有多语句写事务，从源头杜绝交叉。

        语义：
        - 进入时获取 _write_tx_lock，标记 _committed=False
        - yield 连接给调用方执行多条 auto_commit=False 写语句
        - 正常退出 → commit() + _committed=True
        - 异常/取消/超时 → asyncio.shield(rollback())（cancel 传播但 rollback 不中断）
        - finally 释放锁

        单语句 auto_commit=True 操作无需本方法（aiosqlite 单条 execute 自身原子）。
        """
        async with self._write_tx_lock:
            token = self._write_tx_active.set(True)
            _committed = False
            try:
                yield self._conn
                await self._conn.commit()
                _committed = True
            finally:
                if not _committed:
                    try:
                        await asyncio.shield(self._conn.rollback())
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning(f"database.write_transaction_rollback_failed: {e}")
                self._write_tx_active.reset(token)

    @contextlib.asynccontextmanager
    async def profile_write_transaction(self):
        async with self._profile_write_lock:
            committed = False
            try:
                yield self._profile_conn
                await self._profile_conn.commit()
                committed = True
            finally:
                if not committed:
                    await asyncio.shield(self._profile_conn.rollback())

    async def get_conversations_readonly(self, start_ts: float, end_ts: float,
                                          user_id: str = "", limit: int = 50) -> list[dict]:
        """用独立只读连接查询最近对话，供 restore_from_db 使用。

        根因修复：restore_from_db 原用主连接查询，当主连接被后台任务脏事务/长操作占用时
        排队超时 10s → 上下文丢失 → 牛头不对马嘴。改用独立只读连接，WAL 模式下只读
        不被写阻塞，永不受主连接状态影响。只读连接失败时回退主连接(不破坏原行为)。

        CodeRabbit 修复：原 `conn = self._readonly_conn or self._conn` 在只读连接
        存在但执行失败(连接断开/数据库锁)时不回退主连接。改为先尝试只读连接，
        失败则回退主连接重试，保证查询可用性。
        """
        params: list = [start_ts, end_ts]
        where = "WHERE timestamp >= ? AND timestamp <= ?"
        if user_id:
            where += " AND user_id = ?"
            params.append(user_id)
        params.append(limit)
        sql = (
            f"SELECT timestamp, user_message, assistant_reply FROM conversation_logs "
            f"{where} ORDER BY timestamp DESC LIMIT ?"
        )
        # 优先只读连接，失败回退主连接
        if self._readonly_conn is not None:
            try:
                cursor = await self._readonly_conn.execute(sql, params)
                rows = await cursor.fetchall()
                result = [dict(r) for r in rows]
                result.reverse()
                return result
            except Exception as e:
                logger.warning("database.readonly_query_failed_fallback_to_main",
                               error=str(e))
        # 回退主连接（只读连接不可用或查询失败）
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        result = [dict(r) for r in rows]
        result.reverse()
        return result

    async def close(self) -> None:
        if self._profile_conn:
            await self._profile_conn.close()
            self._profile_conn = None
        if self._conn:
            await self._conn.close()
            self._conn = None
        if self._readonly_conn:
            try:
                await self._readonly_conn.close()
            except (OSError, RuntimeError):
                pass
            self._readonly_conn = None
        for _rc in self._read_pool:
            try:
                await _rc.close()
            except (OSError, RuntimeError):
                pass
        self._read_pool = []

    def get_read_conn(self) -> aiosqlite.Connection:
        """从只读连接池取一个连接（round-robin）。池空时回退主连接（保留原行为）。"""
        if not self._read_pool:
            return self._conn
        conn = self._read_pool[self._read_idx % len(self._read_pool)]
        self._read_idx += 1
        return conn

    async def _run_migrations(self) -> None:
        """按 version 顺序执行所有数据库迁移。每个迁移独立事务，失败时 fail-fast 阻止启动。"""
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
                    f"⚠️ 自动修复失败！请手动修复：\n"
                    f"  1. python -m db.repair_migration --mark-clean\n"
                    f"  2. 或删除 agent.db 重新初始化（会丢失历史数据）\n"
                )
                try:
                    await self._conn.close()
                except (OSError, RuntimeError):
                    pass
                sys.exit(1)

        row = await self._conn.execute_fetchall("SELECT MAX(version) FROM schema_version")
        current = row[0][0] if row and row[0][0] is not None else 0

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
        for version, desc, migrate_fn in migrations:
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
        except Exception:
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
                logger.info(f"database.migration_v{version}", desc=description)
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
                    logger.warning("database.migration_dirty_record_error: {}", exc_info=True)
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
        await self._conn.execute("""ALTER TABLE knowledge_relations ADD COLUMN valid_from REAL DEFAULT 0""")
        await self._conn.execute("""ALTER TABLE knowledge_relations ADD COLUMN valid_to REAL DEFAULT 0""")
        await self._conn.execute("""ALTER TABLE knowledge_relations ADD COLUMN confidence REAL DEFAULT 1.0""")

    async def _migrate_v2(self) -> None:
        """v2: conversation_logs 新增 session_id 列。"""
        await self._conn.execute(
            "ALTER TABLE conversation_logs ADD COLUMN session_id TEXT DEFAULT ''")

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
        cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "user_id" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN user_id TEXT DEFAULT 'default'"
            )
        if "agent_id" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN agent_id TEXT DEFAULT 'xiaoda'"
            )
        if "is_raw" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN is_raw INTEGER DEFAULT 0"
            )

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

    async def _migrate_v14(self) -> None:
        """v14: v0.6 认知架构 + 知识图谱 v2 — 5 张认知表 + episodic_memories 3 列 + 9 索引 + KG v2 表。

        Part 1 (cognitive):
        - semantic_memories: 语义记忆（consolidation 后的长期记忆）
        - memory_connections: 记忆连接图
        - bridge_memories: 桥接记忆
        - memory_revisions: 冲突修订链
        - preference_patterns: 偏好模式
        - episodic_memories: salience/last_accessed/status（ALTER TABLE 加列，幂等守卫）
        - 9 个索引（CREATE INDEX IF NOT EXISTS，天然幂等）

        Part 2 (kg_v2):
        - kg_episodes/kg_entities_v2/kg_relations_v2/kg_communities
        - 数据迁移: knowledge_entities → kg_entities_v2, knowledge_relations → kg_relations_v2
        - FTS5 索引回填（含 FTS5 可用性检测，降级为空表）
        """
        # 0. 检测 FTS5 可用性（Windows 用户可能缺少 FTS5 扩展）
        _fts5_available = True
        try:
            await self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_check USING fts5(x UNINDEXED, y)"
            )
            await self._conn.execute("DROP TABLE IF EXISTS _fts5_check")
        except Exception:
            _fts5_available = False
            logger.warning("database.fts5_not_available - FTS5虚拟表将跳过创建")

        # 1. episodic_memories 新增 3 列（幂等：先检查列是否存在，镜像 v13 模式）
        cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "salience" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN salience REAL DEFAULT 0.5"
            )
        if "last_accessed" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN last_accessed REAL DEFAULT 0"
            )
        if "status" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN status TEXT DEFAULT 'active'"
            )

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
        if _fts5_available:
            try:
                await self._conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS kg_entities_v2_fts USING fts5( id UNINDEXED, name_summary )""")
                await self._conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS kg_relations_v2_fts USING fts5( id UNINDEXED, fact )""")
            except Exception as e:
                logger.warning("database.fts5_create_failed", error=str(e))
                _fts5_available = False

        # 1b. 幂等添加 community_id 列（修复 name_embedding 语义劫持）
        kg_cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(kg_entities_v2)")]
        if "community_id" not in kg_cols:
            await self._conn.execute(
                "ALTER TABLE kg_entities_v2 ADD COLUMN community_id TEXT DEFAULT NULL"
            )
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
        kr_cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(knowledge_relations)")]
        if "created_at" not in kr_cols:
            await self._conn.execute(
                "ALTER TABLE knowledge_relations ADD COLUMN created_at REAL DEFAULT 0"
            )

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
        if _fts5_available:
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
        epi_cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "difficulty" not in epi_cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN difficulty REAL DEFAULT 5.0"
            )
        if "stability" not in epi_cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN stability REAL DEFAULT 3.0"
            )
        if "phase" not in epi_cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN phase TEXT DEFAULT 'buffer'"
            )
        if "last_review" not in epi_cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN last_review REAL DEFAULT 0"
            )
        if "reinforcement_count" not in epi_cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN reinforcement_count INTEGER DEFAULT 0"
            )

        concept_cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(concept_nodes)")]
        if "difficulty" not in concept_cols:
            await self._conn.execute(
                "ALTER TABLE concept_nodes ADD COLUMN difficulty REAL DEFAULT 5.0"
            )
        if "stability" not in concept_cols:
            await self._conn.execute(
                "ALTER TABLE concept_nodes ADD COLUMN stability REAL DEFAULT 3.0"
            )
        if "phase" not in concept_cols:
            await self._conn.execute(
                "ALTER TABLE concept_nodes ADD COLUMN phase TEXT DEFAULT 'buffer'"
            )
        if "last_review" not in concept_cols:
            await self._conn.execute(
                "ALTER TABLE concept_nodes ADD COLUMN last_review REAL DEFAULT 0"
            )
        if "reinforcement_count" not in concept_cols:
            await self._conn.execute(
                "ALTER TABLE concept_nodes ADD COLUMN reinforcement_count INTEGER DEFAULT 0"
            )

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
        concept_cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(concept_nodes)")]
        if "created_at" not in concept_cols:
            await self._conn.execute(
                "ALTER TABLE concept_nodes ADD COLUMN created_at REAL DEFAULT 0"
            )

        epi_cols = [r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")]
        if "created_at" not in epi_cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN created_at REAL DEFAULT 0"
            )

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
        cols = {r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")}
        if "distill_status" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN distill_status TEXT DEFAULT ''"
            )
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
        cols = {r["name"] for r in await self.fetch_all("PRAGMA table_info(episodic_memories)")}
        if "updated_at" not in cols:
            await self._conn.execute(
                "ALTER TABLE episodic_memories ADD COLUMN updated_at REAL DEFAULT 0"
            )

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
        cols = {r["name"] for r in await self.fetch_all(
            "PRAGMA table_info(greeting_schedules)")}
        if "user_id" not in cols:
            await self._conn.execute(
                "ALTER TABLE greeting_schedules ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'"
            )
            logger.info("database.migration_v20_user_id_added")
        else:
            logger.info("database.migration_v20_skipped_column_exists")

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
        columns = await self._conn.execute_fetchall(
            "PRAGMA table_info(conversation_logs)"
        )
        if "request_context_json" not in {row[1] for row in columns}:
            await self._conn.execute(
                "ALTER TABLE conversation_logs "
                "ADD COLUMN request_context_json TEXT DEFAULT '{}'"
            )

    async def _migrate_v27(self) -> None:
        # workflow_v2 表（CREATE TABLE IF NOT EXISTS，幂等）
        await db_workflow.create_schema(self._conn)

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
        if auto_commit:
            async with self._write_tx_lock:
                cur = await self._conn.execute(sql, params)
                await self._conn.commit()
        else:
            cur = await self._conn.execute(sql, params)
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
                updated_at REAL DEFAULT 0
            )
        """)

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
            logger.warning(f"创建 memory_child_chunks_fts 失败: {e}")

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
            logger.warning(f"插入默认清理策略失败: {e}")

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
                    logger.debug("database.fts5_trigger_drop_error: {}", exc_info=True)
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
            logger.warning(f"database.fts5_trigger_failed: {e} — FTS搜索将降级为LIKE查询")
    async def insert_conversation_log(self, user_id: str, source: str,
                                       user_message: str, assistant_reply: str,
                                       emotion_label: str = "", model_used: str = "",
                                       session_id: str = "",
                                       auto_commit: bool = True,
                                       request_context_json: str = "{}") -> None:
        await self._conn.execute(
            """INSERT INTO conversation_logs
               (timestamp, user_id, source, user_message, assistant_reply, emotion_label, model_used, session_id, request_context_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), user_id, source, user_message, assistant_reply, emotion_label,
             model_used, session_id, request_context_json),
        )
        if auto_commit:
            await self._conn.commit()

    async def get_recent_replies(self, user_id: str, source: str = "",
                                  limit: int = 5) -> list[str]:
        """查询指定用户最近的回复（跨对话去重，替代易失的内存缓存）。

        根因修复（用户反馈"每段对话80%一样"）：
          原去重机制依赖内存变量 _recent_replies，存在两个致命问题：
          1. 服务重启后内存清空 → 去重历史丢失 → 对相同输入生成相同回复
          2. 去 key 用 session_id，而 session_id 不稳定：
             - 微信 adapter 根本没传 session_id（空串）
             - QQ c2c 的 session_id 每小时换一次（SES-YYYYMMDD-XXXXX）
             → key 频繁变化 → 内存缓存命中失败 → 去重失效
          修复：从 conversation_logs 按 user_id（稳定标识 wechat_xxx/qq_xxx）查询
                最近 N 条非空回复，确保重启后/换 session 后去重状态不丢失。

        Args:
            user_id: 稳定用户标识（wechat_{openid} / qq_{openid}），与写库时的 user_id 一致
            source: 消息来源过滤（qq_c2c/wechat_c2c 等），空串则不限来源
            limit: 返回最近 N 条回复（按时间倒序，最新在前）

        Returns:
            回复文本列表（最新在前），查询失败返回空列表（降级跳过去重，不阻塞主流程）
        """
        try:
            if source:
                cursor = await self._conn.execute(
                    "SELECT assistant_reply FROM conversation_logs "
                    "WHERE user_id=? AND source=? AND assistant_reply != '' "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (user_id, source, limit),
                )
            else:
                cursor = await self._conn.execute(
                    "SELECT assistant_reply FROM conversation_logs "
                    "WHERE user_id=? AND assistant_reply != '' "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (user_id, limit),
                )
            rows = await cursor.fetchall()
            # rows 按 timestamp DESC，最新在前；返回时保持最新在前
            return [row[0] for row in rows if row[0]]
        except (OSError, RuntimeError, sqlite3.Error) as e:
            logger.warning("database.get_recent_replies_failed user_id={} error={}",
                           user_id[:24] if user_id else "", str(e)[:200])
            return []

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
        sql = "INSERT OR REPLACE INTO cron_last_run (task_name, last_run) VALUES (?, ?)"
        if not auto_commit or self._write_tx_active.get():
            await self._conn.execute(sql, (task_name, ts))
            return
        async with self.write_transaction() as conn:
            await conn.execute(sql, (task_name, ts))

    async def log_conversation(self, user_id: str, source: str,
                                user_message: str, assistant_reply: str,
                                emotion_label: str = "", model_used: str = "",
                                session_id: str = "", cost_usd: float = 0,
                                cache_hit: int = 0, cache_miss: int = 0) -> None:
        async with self.write_transaction():
            await self.insert_conversation_log(
                user_id=user_id, source=source,
                user_message=user_message, assistant_reply=assistant_reply,
                emotion_label=emotion_label, model_used=model_used,
                session_id=session_id,
                auto_commit=False,
            )
            if session_id:
                await self.update_session(
                    session_id, cost_usd=cost_usd,
                    cache_hit=cache_hit, cache_miss=cache_miss,
                    auto_commit=False,
                )

    async def cleanup_expired_data(self, auto_commit: bool = True) -> dict[str, int]:
        """按 cleanup_config 表中的策略清理过期数据。返回各表删除行数。

        P0 根治（2026-08-05 rework）：
        1) 用独立 aiosqlite 连接执行（与主连接隔离），避免 cleanup 的长 DELETE 拖慢
           主路径的读/写（主连接命中"独占线程"的 45s 静默期历史阻塞根因）。
        2) 每删一张表立即 commit（逐表提交），释放 SQLite 单写锁。
           原实现所有表的 DELETE 合并在一个长事务里、最后才 commit，长时间独占总写锁，
           与 instinct/记忆编码等并发写时 → 其他写者等锁超时 "database is locked"
           （14:37:45 实证）。逐表提交让写锁在每个 DELETE 之间释放，锁竞争降为短暂可容忍。
        WAL 支持多连接并发，synchronous=NORMAL 让 commit 不 fsync（WAL 仅 checkpoint 时 fsync）。
        """
        import aiosqlite as _aiosqlite
        result: dict[str, int] = {}
        if not self.db_path:
            return result

        _cleanup_t0 = time.time()
        _w_conn = None
        try:
            _w_conn = await _aiosqlite.connect(str(self.db_path))
            _w_conn.row_factory = _aiosqlite.Row
            await _w_conn.execute("PRAGMA journal_mode=WAL")
            # busy_timeout 5000→20000：与主连接一致，避免后台 cleanup 与主连接/
            # instinct/curator 并发写时 5s 等锁不够 → "database is locked"（14:37:45 实证）。
            await _w_conn.execute("PRAGMA busy_timeout=20000")
            await _w_conn.execute("PRAGMA synchronous=NORMAL")
            await _w_conn.execute("PRAGMA cache_size=-20000")
            await _w_conn.execute("PRAGMA temp_store=MEMORY")
            try:
                cursor = await _w_conn.execute(
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
                    del_cursor = await _w_conn.execute(
                        f'DELETE FROM "{table_name}" WHERE "{date_column}" < ? AND "{date_column}" > 0',
                        (cutoff,),
                    )
                    deleted = del_cursor.rowcount
                    result[table_name] = deleted
                    if deleted > 0:
                        logger.info("database.cleanup", table=table_name,
                                    deleted=deleted, retention_days=retention_days)
                    # 根治（2026-08-05）：每删一张表立即 commit，释放写锁。
                    # 原实现所有表的 DELETE 合并在一个长事务里，最后才 commit，
                    # 长时间独占总写锁 → 并发写（instinct/记忆编码/主路径）等锁超时
                    # "database is locked"（14:37:45 实证）。逐表提交让写锁在每个
                    # DELETE 之间释放，其他写者得以插入，锁竞争降为短暂、可容忍。
                    if auto_commit:
                        try:
                            await _w_conn.commit()
                        except (OSError, RuntimeError) as e:
                            logger.warning(f"database.cleanup_commit_failed table={table_name} error={e}")
                except (OSError, RuntimeError) as e:
                    logger.warning("database.cleanup_failed", table=table_name, error=str(e))
                    result[table_name] = 0

            if auto_commit:
                try:
                    await _w_conn.commit()
                except (OSError, RuntimeError) as e:
                    logger.warning(f"清理过期数据提交事务失败: {e}")

            _cleanup_ms = int((time.time() - _cleanup_t0) * 1000)
            if _cleanup_ms > 2000:
                logger.warning("database.cleanup_slow elapsed_ms={}", _cleanup_ms)
            return result
        except Exception as e:
            logger.warning("database.cleanup_conn_failed error={}", str(e))
            return result
        finally:
            if _w_conn is not None:
                with contextlib.suppress(Exception):
                    await _w_conn.close()

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
