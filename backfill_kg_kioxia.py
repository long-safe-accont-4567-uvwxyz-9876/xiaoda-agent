"""回填知识图谱：对 KIOXIA 数据库中的 episodic_memories 重新提取实体和关系。"""
import asyncio
import os
import time

from dotenv import load_dotenv

load_dotenv()

import aiosqlite

from db.db_knowledge import KnowledgeDB
from memory.knowledge_graph import KnowledgeGraph

DB_PATH = "/media/orangepi/KIOXIA/xiaoda-data/db/agent.db"
SF_KEY = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")


async def main() -> None:
    if not SF_KEY:
        print("ERROR: 未配置 SILICONFLOW_API_KEY / EMBED_API_KEY")
        return
    print(f"API Key: {SF_KEY[:8]}***")
    print(f"DB: {DB_PATH}")

    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    kdb = KnowledgeDB(conn)
    kg = KnowledgeGraph(knowledge_db=kdb)
    kg.set_free_model_client(
        api_key=SF_KEY,
        base_url="https://api.siliconflow.cn/v1",
        model="THUDM/GLM-4-9B-0414",
    )

    cur = await conn.execute("SELECT id, summary FROM episodic_memories ORDER BY id")
    rows = await cur.fetchall()
    print(f"共 {len(rows)} 条记忆待回填\n")

    ok = 0
    fail = 0
    for row in rows:
        summary = row["summary"]
        if not summary:
            continue
        t0 = time.time()
        try:
            await kg.auto_extract_and_merge(summary)
            ok += 1
            print(f"[{row['id']}] OK ({time.time()-t0:.1f}s) {summary[:50]}")
        except Exception as e:
            fail += 1
            print(f"[{row['id']}] FAIL: {e} | {summary[:50]}")

    await conn.commit()
    cur = await conn.execute("SELECT COUNT(*) FROM knowledge_entities")
    ent_count = (await cur.fetchone())[0]
    cur = await conn.execute("SELECT COUNT(*) FROM knowledge_relations")
    rel_count = (await cur.fetchone())[0]
    await conn.close()
    print("\n=== 回填完成 ===")
    print(f"成功: {ok}, 失败: {fail}")
    print(f"数据库实体总数: {ent_count}, 关系总数: {rel_count}")


if __name__ == "__main__":
    asyncio.run(main())
