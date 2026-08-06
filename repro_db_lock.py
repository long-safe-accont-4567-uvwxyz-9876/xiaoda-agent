"""验证 aiosqlite 单连接共享事务：长写事务期间，同连接 SELECT 是否排队阻塞。用临时 DB。"""
import asyncio
import time
import aiosqlite
import tempfile
import os


async def main() -> None:
    tmp = tempfile.mktemp(suffix=".db")
    conn = await aiosqlite.connect(tmp)
    await conn.execute("CREATE TABLE IF NOT EXISTS t_error(id INTEGER PRIMARY KEY, tool_name TEXT, pattern TEXT, rule_text TEXT, hit_count INTEGER, created_at TEXT)")
    await conn.execute("CREATE TABLE IF NOT EXISTS t_bulk(id INTEGER PRIMARY KEY, content TEXT)")

    # 场景A：长写事务（大量 INSERT 未提交）进行中，同连接 SELECT 是否排队
    async def long_write():
        try:
            await conn.execute("BEGIN")
            for i in range(30000):
                await conn.execute("INSERT INTO t_bulk(content) VALUES(?)", (f"row {i}",))
            print("[WRITE] 30000 inserts done, awaiting signal...", flush=True)
            await asyncio.sleep(8)
            await conn.commit()
            print("[WRITE] committed", flush=True)
        except Exception as e:
            print(f"[WRITE] err: {e}", flush=True)
            try:
                await conn.rollback()
            except Exception:
                pass

    async def select_during_write():
        await asyncio.sleep(2)  # 等写事务铺开
        t0 = time.time()
        try:
            cur = await conn.execute("SELECT id, tool_name FROM t_error WHERE tool_name='list_stickers' LIMIT 10")
            await cur.fetchall()
            print(f"[SELECT] DURING write on same conn: {time.time()-t0:.2f}s", flush=True)
        except Exception as e:
            print(f"[SELECT] err DURING write: {time.time()-t0:.2f}s {e}", flush=True)

    await asyncio.gather(long_write(), select_during_write())

    # 场景B：无写事务时 SELECT
    t0 = time.time()
    cur = await conn.execute("SELECT COUNT(*) AS c FROM t_error")
    await cur.fetchall()
    print(f"[SELECT] idle: {time.time()-t0:.2f}s", flush=True)

    await conn.close()
    os.path.exists(tmp) and os.remove(tmp)


if __name__ == "__main__":
    asyncio.run(main())