import asyncio
import contextlib
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from config import DATA_DIR

from .conversation_logs import ConversationLogMixin
from .db_analytics import AnalyticsDB
from .db_kg_v2 import KnowledgeDBV2
from .db_knowledge import KnowledgeDB
from .db_learning import LearningDB
from .db_local_ai import LocalAIDB, transaction_lock_for
from .db_memory import MemoryDB
from .db_notebook import NotebookDB
from .db_temporal_memory import TemporalMemoryDB
from .ddl_schema import DDLMixin
from .index_manager import build_default_index_manager
from .legacy_migrations import LegacyMigrationMixin
from .lifecycle_sessions import LifecycleSessionMixin
from .profile_store import ProfileStore
from .session_store import SessionStoreMixin

DB_DIR = DATA_DIR
DB_PATH = DB_DIR / "agent.db"
CURRENT_SCHEMA_VERSION = 28


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
                logger.debug("database.detect_fs_type_windows_error", exc_info=True)
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
        logger.debug("database.detect_fs_type_error", exc_info=True)
    return ""


class DatabaseManager(LegacyMigrationMixin, DDLMixin, ConversationLogMixin, LifecycleSessionMixin, SessionStoreMixin):
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

    async def _close_if_present(self, conn: Any, event: str) -> None:
        """幂等关闭旧连接；关闭失败只记 debug，不阻断 init 重连。"""
        if conn is None:
            return
        try:
            await conn.close()
        except (OSError, RuntimeError):
            logger.debug(event, exc_info=True)

    def _ensure_writable_dir(self) -> None:
        """只读文件系统检测：SQLite 在只读目录上打开不报错，但后续写入失败，提前探测。"""
        try:
            _db_dir = self.db_path.parent
            _probe_file = _db_dir / ".db_write_probe"
            _probe_file.write_text("probe", encoding="utf-8")
            _probe_file.unlink(missing_ok=True)
        except (OSError, PermissionError) as e:
            logger.critical("database.readonly_fs db_path={} error={}", self.db_path, e)
            raise OSError(
                f"数据库目录不可写: {self.db_path.parent}. "
                f"请检查文件系统权限或卸载/重新挂载外置存储。"
            ) from e

    async def _init_readonly_conn(self) -> None:
        """初始化独立只读连接（供 restore_from_db 使用）；失败回退主连接，不阻断启动。"""
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

    async def init(self) -> None:
        # 幂等性：如果已有活跃连接，先关闭旧连接再创建新连接
        await self._close_if_present(self._profile_conn, "database.init_close_old_profile_connection_error")
        self._profile_conn = None
        self.profiles = None
        await self._close_if_present(self._conn, "database.init_close_old_connection_error")
        self._conn = None

        # 只读文件系统检测：提前检测，避免在只读文件系统上打开连接导致静默失败
        # SQLite 在只读文件系统上打开连接不会报错，但后续写入会失败
        self._ensure_writable_dir()

        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        # vfat/exfat 不支持 WAL 共享内存和 FTS5 delete 命令，必须用 DELETE 模式
        # NTFS 完全支持 WAL 和 FTS5，不需要特殊处理
        fs_type = _detect_fs_type(self.db_path)
        self._is_fat_fs = fs_type in ("vfat", "fat", "msdos", "exfat", "fat32")
        if self._is_fat_fs:
            logger.info("database.fat_fs_detected fs={} → 使用 DELETE journal_mode, 禁用 FTS5 触发器", fs_type)
        await self._setup_pragmas(self._is_fat_fs)
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
        await self._close_if_present(self._readonly_conn, "database.init_close_old_readonly_error")
        self._readonly_conn = None
        await self._init_readonly_conn()
        # 只读连接池（检索并发分流）：WAL 下多连接可并发读
        # 幂等：重复 init() 先关闭旧池
        await self._setup_read_pool()
        logger.info("database.ready", path=str(self.db_path))

    async def _setup_pragmas(self, is_fat_fs: bool) -> None:
        """配置 SQLite PRAGMA；每个失败只记 warning 并继续，不阻断启动。"""
        # busy_timeout 必须最先设置，防止后续 PRAGMA 因锁竞争失败
        # 5000→15000：greeting_scheduler/memory_encoding/portrait 等后台任务并发写入时，
        # 5s 不够等待锁释放，导致 create_session 失败 → QQ 消息处理中断，用户收不到回复
        try:
            await self._conn.execute("PRAGMA busy_timeout=15000")
        except (OSError, RuntimeError) as e:
            logger.warning("PRAGMA busy_timeout 失败: {}", e)
        journal_mode_sql = "PRAGMA journal_mode=DELETE" if is_fat_fs else "PRAGMA journal_mode=WAL"
        pragmas = [
            "PRAGMA foreign_keys=ON",
            journal_mode_sql,
            "PRAGMA synchronous=NORMAL",
            "PRAGMA cache_size=-20000",
            "PRAGMA temp_store=MEMORY",
        ]
        if not is_fat_fs:
            pragmas.append("PRAGMA mmap_size=67108864")
            pragmas.append("PRAGMA wal_autocheckpoint=10000")
        for pragma_sql in pragmas:
            try:
                await self._conn.execute(pragma_sql)
            except (OSError, RuntimeError) as e:
                logger.warning("PRAGMA 失败: {} - {}", pragma_sql, e)
        try:
            cursor = await self._conn.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            mode = row[0] if row else "unknown"
            expected = "delete" if is_fat_fs else "wal"
            if mode.lower() != expected:
                logger.warning("journal_mode 未生效，期望={} 当前={}", expected, mode)
            else:
                logger.info("database.journal_mode={}", mode)
        except (OSError, RuntimeError) as e:
            logger.warning("验证 journal_mode 失败: {}", e)

    async def _setup_read_pool(self) -> None:
        """初始化只读连接池；单个连接失败记 warning 并停止。"""
        if self._read_pool:
            for _c in self._read_pool:
                await self._close_if_present(_c, "database.setup_read_pool_close_old_error")
            self._read_pool = []
        for _ in range(self._READ_POOL_SIZE):
            try:
                _rc = await aiosqlite.connect(self._db_ro_uri(), uri=True)
                _rc.row_factory = aiosqlite.Row
                await _rc.execute("PRAGMA query_only=1")
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

    async def _apply_composite_indexes(self) -> None:
        """创建内置复合索引 (幂等)

        与 _create_indexes (单列索引) 互补, 专注于 WHERE 多列组合的复合索引。
        使用 IndexManager.apply() 内部以 CREATE INDEX IF NOT EXISTS 保证幂等。
        """
        try:
            mgr = build_default_index_manager()
            count = await mgr.apply(self._conn)
            logger.info("database.composite_indexes applied={}", count)
        except (OSError, RuntimeError) as e:
            # 复合索引失败不应阻塞数据库初始化
            logger.warning("database.composite_indexes_failed: {}", e)

    async def commit(self) -> None:
        if self._conn:
            try:
                await self._conn.commit()
            except (OSError, RuntimeError) as e:
                logger.warning("database.commit_failed: {}", e)

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
                logger.warning("database.rollback_failed: {}", e)

    @contextlib.asynccontextmanager
    async def write_transaction(self):
        """串行化多语句写事务，根治并发脏事务。

        根因：aiosqlite 单条连接共享事务状态。两个后台任务并发执行
        auto_commit=False 多语句序列时，A 的 commit() 会提交 B 未完成的半事务，
        或 B 的 rollback() 会回滚 A 已写的数据 → 脏事务/数据丢失/SQL logic error
        （历史"上下文丢失/大面积卡顿58s"的真正根因，shield(rollback)/readonly_conn
        只是治标）。本方法用连接级 asyncio.Lock（transaction_lock_for）串行化
        所有多语句写事务，与裸 LocalAIDB 回退事务共享同一把锁，从源头杜绝交叉。

        语义：
        - 进入时获取连接级锁（transaction_lock_for），标记 _committed=False
        - yield 连接给调用方执行多条 auto_commit=False 写语句
        - 正常退出 → commit() + _committed=True
        - 异常/取消/超时 → asyncio.shield(rollback())（cancel 传播但 rollback 不中断）
        - finally 释放锁

        单语句 auto_commit=True 操作无需本方法（aiosqlite 单条 execute 自身原子）。
        """
        async with transaction_lock_for(self._conn):
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
                        logger.warning("database.write_transaction_rollback_failed: {}", e)
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
        """关闭所有连接（幂等，任一连接关闭失败不阻断后续关闭，避免泄漏）。"""
        await self._close_if_present(self._profile_conn, "database.close_profile_conn_error")
        self._profile_conn = None
        await self._close_if_present(self._conn, "database.close_main_conn_error")
        self._conn = None
        await self._close_if_present(self._readonly_conn, "database.close_readonly_conn_error")
        self._readonly_conn = None
        for _rc in self._read_pool:
            await self._close_if_present(_rc, "database.close_read_pool_conn_error")
        self._read_pool = []

    def get_read_conn(self) -> aiosqlite.Connection:
        """从只读连接池取一个连接（round-robin）。池空时回退主连接（保留原行为）。"""
        if not self._read_pool:
            return self._conn
        conn = self._read_pool[self._read_idx % len(self._read_pool)]
        self._read_idx += 1
        return conn


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
        try:
            return [dict(r) for r in rows]
        except (TypeError, ValueError) as e:
            # 根因诊断：dict(r) 失败通常是 self._conn.row_factory 未设为 aiosqlite.Row，
            # 此时 execute_fetchall 返回 tuple（而非支持 keys() 的 Row），dict(tuple) 抛
            # TypeError「cannot convert dictionary update sequence element #0 to a sequence」。
            # 常见于 __new__ 构造的最小实例或测试用裸 aiosqlite.connect（未设 row_factory）。
            # 修复方式：connect 后设置 conn.row_factory = aiosqlite.Row；
            #           或改用 _conn.execute_fetchall 直接按索引 row[0]/row[1] 取值。
            _rf = getattr(self._conn, "row_factory", None)
            _first = rows[0] if rows else None
            logger.error(
                "db.fetch_all.row_to_dict_failed",
                error=str(e),
                row_factory=repr(_rf),
                row_type=type(_first).__name__ if _first is not None else "none",
                sample_row=repr(_first)[:200] if _first is not None else "",
                sql=sql[:120],
                hint="请确认 self._conn.row_factory = aiosqlite.Row；裸连接请改用 _conn.execute_fetchall 按索引取值",
            )
            raise

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    async def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        """幂等批量添加列：一次 PRAGMA 检查，缺失列逐个 ALTER TABLE ADD COLUMN。

        columns 形如 {"salience": "salience REAL DEFAULT 0.5"}，键为列名，值为列定义。
        表名为内部硬编码调用，无用户输入，不存在 SQL 注入面。
        用 execute_fetchall + row[1] 读取列名（兼容无 row_factory 的裸连接）。
        """
        rows = await self._conn.execute_fetchall(f"PRAGMA table_info({table})")
        existing = {row[1] for row in rows}
        for name, spec in columns.items():
            if name not in existing:
                await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")

    async def execute(self, sql: str, params: tuple = (), auto_commit: bool = True) -> int:
        """通用写语句。INSERT 返回 lastrowid，UPDATE/DELETE 返回 rowcount。"""
        if auto_commit:
            async with transaction_lock_for(self._conn):
                cur = await self._conn.execute(sql, params)
                await self._conn.commit()
        else:
            cur = await self._conn.execute(sql, params)
        # INSERT 返回 lastrowid，UPDATE/DELETE 返回 rowcount
        if sql.strip().upper().startswith("INSERT"):
            return cur.lastrowid or 0
        return cur.rowcount
