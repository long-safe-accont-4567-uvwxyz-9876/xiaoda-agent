import os
import time
import aiosqlite
from loguru import logger
from vector_store import VectorStore


class MemoryManager:

    def __init__(self, config: dict):
        self._config = config
        self._db_path = config.get("memory_db_path", "data/memory.db")
        self._db = None
        self._vector_store = VectorStore(
            self._db_path,
            embed_api_key=config.get("embed_api_key", ""),
            embed_base_url=config.get("embed_base_url", ""),
            embed_model=config.get("embed_model", "BAAI/bge-m3"),
        )

    async def init(self):
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._init_tables()
        await self._vector_store.init()
        logger.info("memory_manager.ready")

    async def _init_tables(self):
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                tags TEXT DEFAULT '',
                importance REAL DEFAULT 0.5,
                access_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_input TEXT NOT NULL,
                assistant_reply TEXT NOT NULL,
                user_id TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories(tags);
            CREATE INDEX IF NOT EXISTS idx_conversations_session ON conversations(session_id);
        """)
        await self._db.commit()

    async def close(self):
        await self._vector_store.close()
        if self._db:
            await self._db.close()

    async def store(self, user_input: str, assistant_reply: str, user_id: str = ""):
        await self._db.execute(
            "INSERT INTO conversations (session_id, user_input, assistant_reply, user_id) VALUES (?, ?, ?, ?)",
            ("default", user_input, assistant_reply, user_id)
        )
        await self._db.commit()

    async def remember(self, content: str, tags: str = "", importance: float = 0.5) -> int:
        cursor = await self._db.execute(
            "INSERT INTO memories (content, tags, importance) VALUES (?, ?, ?)",
            (content, tags, importance)
        )
        await self._db.commit()
        row_id = cursor.lastrowid
        await self._vector_store.upsert(row_id, content)
        return row_id

    async def retrieve(self, query: str, top_k: int = 3) -> list[dict]:
        vec_results = await self._vector_store.search(query, top_k=top_k)
        if not vec_results:
            cursor = await self._db.execute(
                "SELECT id, content, tags, importance FROM memories ORDER BY last_accessed DESC LIMIT ?",
                (top_k,)
            )
            rows = await cursor.fetchall()
            return [{"id": r[0], "content": r[1], "tags": r[2], "importance": r[3]} for r in rows]

        results = []
        for row_id, distance in vec_results:
            cursor = await self._db.execute(
                "SELECT id, content, tags, importance FROM memories WHERE id = ?",
                (row_id,)
            )
            row = await cursor.fetchone()
            if row:
                results.append({"id": row[0], "content": row[1], "tags": row[2], "importance": row[3], "distance": distance})
        return results

    async def forget(self, query: str) -> bool:
        results = await self.retrieve(query, top_k=1)
        if results:
            await self._db.execute("DELETE FROM memories WHERE id = ?", (results[0]["id"],))
            await self._db.commit()
            return True
        return False

    async def get_recent(self, limit: int = 10) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT content, tags, importance, created_at FROM memories ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [{"content": r[0], "tags": r[1], "importance": r[2], "created_at": r[3]} for r in rows]
