"""audit_context_consumption 整批写入失败时也必须提交。

同类缺陷（2026-11-09 修复 database is locked 时顺手收敛）：
原写法把 commit 挂在 inserted 上，executemany 与逐条 execute 全部失败时
inserted=0，写事务留在共享主连接上，其他连接等锁超时。
"""
import sqlite3

from memory.context_governance import ContextGovernance


class _RejectingConn:
    """模拟整批写入失败：executemany 与逐条 execute 都抛 database is locked。"""

    def __init__(self) -> None:
        self.commits = 0
        self.executed: list[str] = []

    async def executemany(self, sql, rows):
        self.executed.append("executemany")
        raise sqlite3.OperationalError("database is locked")

    async def execute(self, sql, params):
        self.executed.append("execute")
        raise sqlite3.OperationalError("database is locked")

    async def commit(self):
        self.commits += 1


async def test_audit_batch_all_failed_still_commits():
    conn = _RejectingConn()
    gov = ContextGovernance(conn)

    inserted = await gov.audit_context_consumption(
        "resp-1", [{"id": 1, "score": 0.5}])

    assert inserted == 0
    assert conn.executed, "应当至少尝试过写入"
    assert conn.commits == 1, "尝试过写入就必须提交，否则写事务留在共享连接上"


async def test_audit_empty_batch_never_commits():
    """没有可写行时不应无谓提交（保持原语义）。"""
    conn = _RejectingConn()
    gov = ContextGovernance(conn)

    inserted = await gov.audit_context_consumption("resp-2", [])

    assert inserted == 0
    assert conn.commits == 0
