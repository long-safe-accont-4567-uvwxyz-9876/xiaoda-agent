import json
import asyncio
import hashlib
from collections import OrderedDict
from pathlib import Path
from loguru import logger

try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

try:
    from openai import AsyncOpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


class EmbedCache:

    def __init__(self, max_size: int = 128):
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    def get(self, text: str) -> list[float] | None:
        key = hashlib.md5(text[:300].encode()).hexdigest()
        if key in self._cache:
            self._cache.move_to_end(key)
            self._hits += 1
            return self._cache[key]
        self._misses += 1
        return None

    def put(self, text: str, vec: list[float]):
        key = hashlib.md5(text[:300].encode()).hexdigest()
        self._cache[key] = vec
        self._cache.move_to_end(key)
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "size": len(self._cache),
        }


class VectorStore:

    def __init__(self, db_path: str | Path, embed_api_key: str = "",
                 embed_base_url: str = "", embed_model: str = "BAAI/bge-m3"):
        self._db_path = str(db_path)
        self._embed_api_key = embed_api_key
        self._embed_base_url = embed_base_url
        self._embed_model = embed_model
        self._initialized = False
        self._embed_client = None
        self._vec_conn = None
        self._cache = EmbedCache(max_size=128)

        if HAS_OPENAI and embed_api_key:
            self._embed_client = AsyncOpenAI(
                api_key=embed_api_key,
                base_url=embed_base_url or "https://api.siliconflow.cn/v1",
            )

    async def init(self):
        if not HAS_SQLITE_VEC:
            logger.warning("vector_store.sqlite_vec_missing")
            return

        import sqlite3
        self._vec_conn = sqlite3.connect(self._db_path)
        self._vec_conn.enable_load_extension(True)
        sqlite_vec.load(self._vec_conn)
        self._vec_conn.enable_load_extension(False)

        self._vec_conn.execute("PRAGMA journal_mode=WAL")
        self._vec_conn.execute("PRAGMA synchronous=NORMAL")
        self._vec_conn.execute("PRAGMA cache_size=-20000")
        self._vec_conn.execute("PRAGMA mmap_size=67108864")

        self._vec_conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec
            USING vec0(embedding float[1024])
        """)
        self._vec_conn.commit()
        self._initialized = True
        logger.info("vector_store.ready", pragmas="WAL+cache+mmap")

    async def close(self):
        if self._vec_conn:
            self._vec_conn.close()
            self._vec_conn = None

    async def embed(self, text: str) -> list[float]:
        if not self._embed_client:
            return []
        cached = self._cache.get(text)
        if cached:
            return cached
        try:
            response = await self._embed_client.embeddings.create(
                model=self._embed_model,
                input=text,
            )
            vec = response.data[0].embedding
            self._cache.put(text, vec)
            return vec
        except Exception as e:
            logger.warning("vector_store.embed_failed", error=str(e))
            return []

    async def upsert(self, row_id: int, text: str) -> bool:
        if not self._initialized or not self._vec_conn:
            return False
        vec = await self.embed(text)
        if not vec:
            return False
        vec_json = json.dumps(vec)
        try:
            try:
                self._vec_conn.execute("DELETE FROM memories_vec WHERE rowid=?", [row_id])
            except Exception:
                pass
            self._vec_conn.execute(
                "INSERT INTO memories_vec(rowid, embedding) VALUES (?, vec_f32(?))",
                [row_id, vec_json],
            )
            self._vec_conn.commit()
            return True
        except Exception as e:
            logger.warning("vector_store.upsert_failed", error=str(e))
            return False

    async def search(self, query_text: str, top_k: int = 5) -> list[tuple[int, float]]:
        if not self._initialized or not self._vec_conn:
            return []
        vec = await self.embed(query_text)
        if not vec:
            return []
        vec_json = json.dumps(vec)
        try:
            rows = self._vec_conn.execute(
                "SELECT rowid, distance FROM memories_vec "
                "WHERE embedding MATCH vec_f32(?) AND k=? ORDER BY distance",
                [vec_json, top_k],
            ).fetchall()
            return [(row[0], row[1]) for row in rows]
        except Exception as e:
            logger.warning("vector_store.search_failed", error=str(e))
            return []
