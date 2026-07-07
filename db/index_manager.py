"""数据库复合索引管理器 (P2 性能)

参考:
- db/idempotent_migrator.py 的幂等模式
- SQLite CREATE INDEX IF NOT EXISTS 语义

特性:
- IndexDef 声明式索引定义 (table, columns, name, unique)
- register() 注册索引, apply() 批量幂等创建
- verify() 用 PRAGMA index_list + index_info 验证索引存在
- 失败时单条跳过 + 日志告警, 不影响其他索引
- 与 db/database.py._create_indexes() 互补: 本模块负责"复合索引"

复合索引设计依据 (基于 db/db_*.py 实际查询模式):
- episodic_memories: WHERE session_id+timestamp, importance+timestamp, access_count+timestamp
- conversation_logs: WHERE session_id+timestamp, user_id+source (替代原 spec 的 role)
- knowledge_entities: WHERE kind+updated_at (替代原 spec 的 type+confidence)
- knowledge_relations: WHERE from_entity+to_entity+confidence
- learnings: WHERE status+created_at (替代原 spec 的 learning_records+user_id)
- notebook_entries: WHERE status+created_at (替代原 spec 的 notes+user_id)

注: 任务 spec 中的部分列名 (role/type/source_id/target_id/last_accessed/user_id)
与实际 schema 不符, 已按真实列名适配; 表名 learning_records/notes 实际为
learnings/notebook_entries, 同样已适配。
"""
from __future__ import annotations

from dataclasses import dataclass

import aiosqlite
from loguru import logger


@dataclass(frozen=True)
class IndexDef:
    """单条索引定义"""
    table: str
    columns: list[str]
    name: str
    unique: bool = False

    def to_sql(self) -> str:
        """生成 CREATE INDEX 语句 (IF NOT EXISTS 保证幂等)"""
        unique_kw = "UNIQUE " if self.unique else ""
        cols = ", ".join(self.columns)
        return f"CREATE {unique_kw}INDEX IF NOT EXISTS {self.name} ON {self.table}({cols})"


class IndexManager:
    """复合索引管理器

    用法:
        mgr = IndexManager()
        mgr.register(IndexDef("episodic_memories", ["session_id", "timestamp"], "idx_em_session_ts"))
        created = await mgr.apply(conn)
        exists = await mgr.verify(conn, "episodic_memories", ["session_id", "timestamp"])
    """

    def __init__(self) -> None:
        self._indexes: list[IndexDef] = []

    def register(self, index: IndexDef) -> None:
        """注册一条索引定义 (重复 name 自动去重, 后注册覆盖前注册)"""
        # 同名索引: 替换
        self._indexes = [i for i in self._indexes if i.name != index.name]
        self._indexes.append(index)

    def list_indexes(self) -> list[IndexDef]:
        """返回已注册的索引列表 (拷贝)"""
        return list(self._indexes)

    async def apply(self, conn: aiosqlite.Connection) -> int:
        """创建所有已注册的索引 (幂等: CREATE INDEX IF NOT EXISTS)

        单条失败不影响其他索引, 仅记录 warning 日志。
        返回本次实际创建 (或已存在) 的索引数。
        """
        created = 0
        for idx in self._indexes:
            try:
                await conn.execute(idx.to_sql())
                created += 1
            except Exception as e:
                # 列不存在 / 表不存在等: 跳过但不抛
                logger.warning(
                    f"IndexManager.create_failed name={idx.name} "
                    f"table={idx.table} cols={idx.columns} error={e}"
                )
        await conn.commit()
        if created:
            logger.info(f"IndexManager.applied created_or_existing={created} total={len(self._indexes)}")
        return created

    async def verify(self, conn: aiosqlite.Connection, table: str,
                     columns: list[str]) -> bool:
        """验证某表上是否存在覆盖指定列 (顺序敏感) 的索引

        通过 PRAGMA index_list + index_info 检查所有索引, 匹配列顺序。
        """
        # 1. 获取该表所有索引
        cursor = await conn.execute(f"PRAGMA index_list({table})")
        idx_rows = await cursor.fetchall()
        if not idx_rows:
            return False

        target_cols = list(columns)

        for idx_row in idx_rows:
            # row 结构: (seq, name, unique, origin, partial)
            idx_name = idx_row[1]
            # 2. 查询该索引的列
            cur2 = await conn.execute(f"PRAGMA index_info({idx_name})")
            info_rows = await cur2.fetchall()
            # info_row 结构: (seqno, cid, name)
            idx_cols = [r[2] for r in info_rows]
            # 3. 前缀匹配: 索引列以目标列为前缀 (或完全相等)
            if (len(idx_cols) >= len(target_cols)
                    and idx_cols[: len(target_cols)] == target_cols):
                return True
        return False


def build_default_index_manager() -> IndexManager:
    """构造内置常用复合索引的 IndexManager

    索引命名约定: idx_<表缩写>_<列缩写>_<...>
    """
    mgr = IndexManager()

    # ── episodic_memories: 时间 + 重要性 + 会话过滤 ──
    mgr.register(IndexDef(
        "episodic_memories", ["session_id", "timestamp"],
        "idx_em_session_ts",
    ))
    mgr.register(IndexDef(
        "episodic_memories", ["importance", "timestamp"],
        "idx_em_importance_ts",
    ))
    mgr.register(IndexDef(
        "episodic_memories", ["access_count", "timestamp"],
        "idx_em_access_ts",
    ))

    # ── conversation_logs: 会话 + 时间; 用户 + 来源 ──
    mgr.register(IndexDef(
        "conversation_logs", ["session_id", "timestamp"],
        "idx_conv_session_ts",
    ))
    mgr.register(IndexDef(
        "conversation_logs", ["user_id", "source"],
        "idx_conv_user_source",
    ))

    # ── knowledge_entities: 类型 + 更新时间 ──
    mgr.register(IndexDef(
        "knowledge_entities", ["kind", "updated_at"],
        "idx_ke_kind_updated",
    ))

    # ── knowledge_relations: 实体对 + 置信度 ──
    mgr.register(IndexDef(
        "knowledge_relations", ["from_entity", "to_entity", "confidence"],
        "idx_krel_pair_conf",
    ))

    # ── learnings: 状态 + 创建时间 ──
    mgr.register(IndexDef(
        "learnings", ["status", "created_at"],
        "idx_lrn_status_created",
    ))

    # ── notebook_entries: 状态 + 创建时间 ──
    mgr.register(IndexDef(
        "notebook_entries", ["status", "created_at"],
        "idx_note_status_created",
    ))

    return mgr
