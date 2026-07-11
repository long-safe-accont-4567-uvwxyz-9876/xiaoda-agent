"""Scope 三级隔离 — user_id/session_id/agent_id 作用域控制。

用于 mem0 SPEC 优化的记忆隔离：
- user_id: 用户标识（默认 'default'，单用户桌面应用）
- session_id: 会话标识（复用已有字段，会话级隔离）
- agent_id: Agent 标识（xiaoda/xiaoli/xiaolian/xiaoke）
"""
from dataclasses import dataclass


@dataclass
class Scope:
    """记忆隔离的三级 scope。

    默认值对应单用户桌面应用场景：
    - user_id='default': 单用户
    - session_id='user': 默认会话
    - agent_id='xiaoda': 默认 agent
    """
    user_id: str = "default"
    session_id: str = "user"
    agent_id: str = "xiaoda"

    def to_sql_filter(self, table: str = "episodic_memories") -> str:
        """生成 SQL WHERE 子句（user_id + agent_id 过滤）。

        注意：默认不含 session_id 过滤（跨会话检索是最常见场景）。
        session_id 过滤由调用方通过 session_only=True 参数触发。

        Args:
            table: 表名前缀，默认 'episodic_memories'

        Returns:
            SQL WHERE 子句字符串，如 "episodic_memories.user_id = 'default' AND episodic_memories.agent_id = 'xiaoda'"
        """
        return (
            f"{table}.user_id = '{self.user_id}' "
            f"AND {table}.agent_id = '{self.agent_id}'"
        )

    def to_sql_params(self) -> list[str]:
        """返回参数化 SQL 的参数列表（用于 WHERE ... AND ... 占位符）。

        Returns:
            [user_id, agent_id]
        """
        return [self.user_id, self.agent_id]

    def to_sql_filter_parametrized(self, table: str = "episodic_memories") -> tuple[str, list[str]]:
        """生成参数化 SQL WHERE 子句（防注入）。

        Returns:
            (where_clause, params) 如 ("em.user_id = ? AND em.agent_id = ?", ["default", "xiaoda"])
        """
        where = f"{table}.user_id = ? AND {table}.agent_id = ?"
        return where, [self.user_id, self.agent_id]
