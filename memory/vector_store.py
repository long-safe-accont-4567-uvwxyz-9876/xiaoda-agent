import json
import os
import asyncio
import hashlib
import threading
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

    def __init__(self, max_size: int = 256):
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    def __contains__(self, text: str) -> bool:
        key = hashlib.md5(text[:300].encode()).hexdigest()
        return key in self._cache

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
                 embed_base_url: str = "", embed_model: str = "BAAI/bge-m3",
                 dimensions: int = 0):
        self._db_path = str(db_path)
        self._embed_api_key = embed_api_key
        self._embed_base_url = embed_base_url
        self._embed_model = embed_model
        self._dimensions = dimensions
        self._initialized = False
        self._closed = False
        self._lock = threading.Lock()
        self._embed_client = None
        self._vec_conn = None
        self._cache = EmbedCache(max_size=256)

        # 并发嵌入限制（避免 API 限流），可通过环境变量配置
        _embed_concurrency = int(os.getenv("VECTOR_EMBED_CONCURRENCY", "8"))
        self._embed_semaphore = asyncio.Semaphore(_embed_concurrency)

        if HAS_OPENAI and embed_api_key:
            self._embed_client = AsyncOpenAI(
                api_key=embed_api_key,
                base_url=embed_base_url or "https://api.siliconflow.cn/v1",
            )

    @property
    def ready(self) -> bool:
        return self._initialized and not self._closed

    @property
    def enabled(self) -> bool:
        return self._initialized

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def init(self):
        if not HAS_SQLITE_VEC:
            logger.warning("vector_store.sqlite_vec_missing")
            return

        import sqlite3

        def _init_db():
            with self._lock:
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)

                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA cache_size=-20000")
                conn.execute("PRAGMA mmap_size=67108864")

                # Use configured dimensions, or default 1024 until auto-detected
                dims = self._dimensions if self._dimensions > 0 else 1024
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec
                    USING vec0(embedding float[{dims}])
                """)
                conn.commit()
                return conn

        self._vec_conn = await asyncio.to_thread(_init_db)

        self._initialized = True
        logger.info("vector_store.ready", pragmas="WAL+cache+mmap")

    async def close(self):
        def _do_close():
            with self._lock:
                if self._closed:
                    return
                self._closed = True
                if self._vec_conn:
                    self._vec_conn.close()
                    self._vec_conn = None

        await asyncio.to_thread(_do_close)
        if self._cache.stats["size"] > 0:
            logger.info("vector_store.cache_stats", **self._cache.stats)

    async def embed(self, text: str) -> list[float]:
        if not self._embed_client:
            return []

        cached = self._cache.get(text)
        if cached:
            return cached

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                response = await self._embed_client.embeddings.create(
                    model=self._embed_model,
                    input=text,
                )
                vec = response.data[0].embedding
                # Auto-detect dimensions from first API response
                if self._dimensions == 0 and vec:
                    self._dimensions = len(vec)
                    logger.info("vector_store.dimensions_detected", dimensions=self._dimensions)
                elif vec and len(vec) != self._dimensions:
                    logger.warning("vector_store.dimension_mismatch", expected=self._dimensions, actual=len(vec))
                self._cache.put(text, vec)
                return vec
            except Exception as e:
                if attempt < max_retries:
                    await asyncio.sleep(1)
                    continue
                logger.warning("vector_store.embed_failed", error=str(e), attempts=max_retries + 1)
                return []

    async def warm_cache(self, texts: list[str]) -> None:
        """预热嵌入缓存：对未缓存文本调用 embed 填充缓存，单条失败不影响整体。"""
        if not self._embed_client or not texts:
            return
        for text in texts:
            if not text or text in self._cache:
                continue
            try:
                await self.embed(text)
            except Exception as e:
                logger.warning("vector_store.warm_cache_item_failed", error=str(e))

    async def upsert(self, row_id: int, text: str) -> bool:
        if not self._initialized or not self._vec_conn:
            return False

        vec = await self.embed(text)
        if not vec:
            return False

        vec_json = json.dumps(vec)

        def _do_upsert():
            with self._lock:
                if self._closed:
                    return False
                try:
                    self._vec_conn.execute("BEGIN TRANSACTION")
                    try:
                        self._vec_conn.execute("DELETE FROM memories_vec WHERE rowid=?", [row_id])
                    except Exception as e:
                        logger.debug(f"vector_store upsert 删除旧记录失败(rowid={row_id}): {e}")
                    self._vec_conn.execute(
                        "INSERT INTO memories_vec(rowid, embedding) VALUES (?, vec_f32(?))",
                        [row_id, vec_json],
                    )
                    self._vec_conn.commit()
                    return True
                except Exception as e:
                    try:
                        self._vec_conn.execute("ROLLBACK")
                    except Exception:
                        pass
                    logger.warning("vector_store.upsert_failed", row_id=row_id, error=str(e))
                    return False

        return await asyncio.to_thread(_do_upsert)

    async def delete(self, row_id: int) -> bool:
        """删除指定 rowid 的向量记录"""
        if not self._initialized or not self._vec_conn:
            return False

        def _do_delete():
            with self._lock:
                if self._closed:
                    return False
                try:
                    self._vec_conn.execute("DELETE FROM memories_vec WHERE rowid=?", [row_id])
                    self._vec_conn.commit()
                    return True
                except Exception as e:
                    logger.warning("vector_store.delete_failed", row_id=row_id, error=str(e))
                    return False

        try:
            return await asyncio.to_thread(_do_delete)
        except Exception as e:
            logger.warning("vector_store.delete_failed", row_id=row_id, error=str(e))
            return False

    async def batch_upsert(self, items: list[tuple[int, str]]) -> int:
        """批量写入向量（并发嵌入 + 单事务写入）"""
        if not self._initialized or not self._vec_conn:
            return 0

        if not items:
            return 0

        # 并发嵌入（受 Semaphore 限制，避免 API 限流）
        async def _embed_one(row_id: int, text: str) -> tuple[int, str, list[float]]:
            async with self._embed_semaphore:
                vec = await self.embed(text)
                return (row_id, text, vec)

        embed_results = await asyncio.gather(
            *[_embed_one(row_id, text) for row_id, text in items],
            return_exceptions=True,
        )

        # 过滤成功的嵌入结果（日志不记录文本内容，可能含 PII）
        valid_items: list[tuple[int, str, list[float]]] = []
        for result in embed_results:
            if isinstance(result, Exception):
                logger.warning("vector.batch_embed_failed", error=str(result)[:200])
                continue
            row_id, text, vec = result
            if vec:
                valid_items.append((row_id, text, vec))

        if not valid_items:
            return 0

        # 单事务批量写入（保持原有逻辑）
        def _do_batch():
            with self._lock:
                if self._closed:
                    return 0
                conn = self._vec_conn
                success = 0
                try:
                    conn.execute("BEGIN TRANSACTION")
                    for row_id, text, vec in valid_items:
                        vec_json = json.dumps(vec)
                        try:
                            conn.execute("DELETE FROM memories_vec WHERE rowid=?", [row_id])
                        except Exception as e:
                            logger.debug(f"vector_store batch_upsert 删除旧记录失败(rowid={row_id}): {e}")
                        try:
                            conn.execute(
                                "INSERT INTO memories_vec(rowid, embedding) VALUES (?, vec_f32(?))",
                                [row_id, vec_json],
                            )
                            success += 1
                        except Exception as e:
                            logger.warning("vector_store.batch_upsert_item_failed", row_id=row_id, error=str(e))
                    if success > 0:
                        conn.commit()
                    else:
                        conn.rollback()
                    return success
                except Exception as e:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    logger.error("vector.batch_upsert_failed", error=str(e)[:200])
                    return 0

        return await asyncio.to_thread(_do_batch)

    async def search(self, query_text: str, top_k: int = 5) -> list[tuple[int, float]]:
        if not self._initialized or not self._vec_conn:
            return []

        vec = await self.embed(query_text)
        if not vec:
            return []

        vec_json = json.dumps(vec)

        def _do_search():
            with self._lock:
                if self._closed:
                    return []
                rows = self._vec_conn.execute(
                    "SELECT rowid, distance FROM memories_vec "
                    "WHERE embedding MATCH vec_f32(?) AND k=? ORDER BY distance",
                    [vec_json, top_k],
                ).fetchall()
                return [(row[0], row[1]) for row in rows]

        try:
            return await asyncio.to_thread(_do_search)
        except Exception as e:
            logger.warning("vector_store.search_failed", error=str(e))
            return []
