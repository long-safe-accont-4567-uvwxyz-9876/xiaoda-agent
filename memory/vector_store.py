from typing import Any
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
    """基于 LRU 的文本嵌入向量缓存。"""

    def __init__(self, max_size: int = 256) -> None:
        """初始化嵌入缓存。"""
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    @staticmethod
    def _key(text: str) -> str:
        """生成缓存键 — 使用完整文本的 SHA256 哈希，避免截断导致碰撞。"""
        return hashlib.sha256(text.encode()).hexdigest()[:32]

    def __contains__(self, text: str) -> bool:
        """检查文本是否已存在于缓存中。"""
        key = self._key(text)
        with self._lock:
            return key in self._cache

    def get(self, text: str) -> list[float] | None:
        """根据文本查询缓存的嵌入向量，命中时更新 LRU 顺序。"""
        key = self._key(text)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._hits += 1
                return self._cache[key]
            self._misses += 1
            return None

    def put(self, text: str, vec: list[float]) -> None:
        """将文本和对应嵌入向量存入缓存，超出容量时淘汰最久未使用的条目。"""
        key = self._key(text)
        with self._lock:
            self._cache[key] = vec
            self._cache.move_to_end(key)
            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    @property
    def stats(self) -> dict:
        """返回缓存统计信息（命中数、未命中数、命中率、当前大小）。"""
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / total, 3) if total > 0 else 0.0,
            "size": len(self._cache),
        }


class VectorStore:
    """基于 SQLite-vec 的向量存储，支持嵌入、写入、删除和相似度搜索。"""

    def __init__(self, db_path: str | Path, embed_api_key: str = "",
                 embed_base_url: str = "", embed_model: str = "BAAI/bge-m3",
                 dimensions: int = 0) -> None:
        """初始化向量存储。"""
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
        self._cache = EmbedCache(max_size=512)

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
        """返回存储是否已初始化且未关闭。"""
        return self._initialized and not self._closed

    @property
    def enabled(self) -> bool:
        """返回存储是否已初始化。"""
        return self._initialized

    @property
    def dimensions(self) -> int:
        """返回嵌入向量的维度。"""
        return self._dimensions

    async def init(self) -> None:
        """初始化 SQLite 数据库，加载 sqlite_vec 扩展并创建向量虚拟表。"""
        if not HAS_SQLITE_VEC:
            logger.warning("vector_store.sqlite_vec_missing")
            return

        import sqlite3

        def _init_db() -> tuple:
            """在后台线程中初始化 SQLite 数据库，加载 sqlite_vec 扩展并创建向量虚拟表。"""
            with self._lock:
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)

                # 检测文件系统类型，vfat/exfat 不支持 WAL
                from pathlib import Path
                from db.database import _detect_fs_type
                fs_type = _detect_fs_type(Path(self._db_path))
                is_fat = fs_type in ("vfat", "fat", "msdos", "exfat", "fat32")
                if is_fat:
                    conn.execute("PRAGMA journal_mode=DELETE")
                else:
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA cache_size=-20000")
                if not is_fat:
                    conn.execute("PRAGMA mmap_size=67108864")

                # Use configured dimensions, or default 1024 until auto-detected
                dims = self._dimensions if self._dimensions > 0 else 1024
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec
                    USING vec0(embedding float[{dims}])
                """)
                conn.execute(f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_child_vec
                    USING vec0(embedding float[{dims}])
                """)
                conn.commit()
                return conn, is_fat

        self._vec_conn, is_fat = await asyncio.to_thread(_init_db)

        self._initialized = True
        pragma_desc = "DELETE+cache" if is_fat else "WAL+cache+mmap"
        logger.info("vector_store.ready", pragmas=pragma_desc)

    async def close(self) -> None:
        def _do_close() -> None:
            """在后台线程中关闭 SQLite 连接。"""
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
        """生成文本的嵌入向量，优先使用缓存，失败时自动重试。"""
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
        return None

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
        """写入或更新指定 rowid 的向量记录（先删后插）。"""
        if not self._initialized or not self._vec_conn:
            return False

        vec = await self.embed(text)
        if not vec:
            return False

        vec_json = json.dumps(vec)

        def _do_upsert() -> bool:
            """在后台线程中执行向量的先删后插（upsert）操作。"""
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
                        logger.debug("vector_store.upsert_rollback_error: {}", exc_info=True)
                    logger.warning("vector_store.upsert_failed", row_id=row_id, error=str(e))
                    return False

        return await asyncio.to_thread(_do_upsert)

    async def upsert_child(self, child_id: int, text: str) -> None:
        """子chunk向量写入（使用独立表 memories_child_vec）。"""
        if not self._initialized or not self._vec_conn:
            return
        vec = await self.embed(text)
        if not vec:
            return
        vec_json = json.dumps(vec)

        def _do_upsert() -> None:
            """在后台线程中执行子chunk向量的写入（upsert）操作。"""
            with self._lock:
                if self._closed:
                    return
                try:
                    self._vec_conn.execute(
                        "INSERT OR REPLACE INTO memories_child_vec (rowid, embedding) VALUES (?, vec_f32(?))",
                        (child_id, vec_json),
                    )
                    self._vec_conn.commit()
                except Exception as e:
                    logger.warning("vector_store.upsert_child_failed", row_id=child_id, error=str(e))

        await asyncio.to_thread(_do_upsert)

    async def batch_upsert_children(self, items: list[tuple[int, str]]) -> None:
        """批量子chunk向量写入。items = [(child_id, text), ...]"""
        if not self._initialized or not self._vec_conn or not items:
            return
        # 并发嵌入，受 semaphore 限制
        async def _embed_one(cid: int, text: str):
            """对单条文本执行嵌入，受并发信号量限制。"""
            async with self._embed_semaphore:
                vec = await self.embed(text)
                return (cid, vec)

        tasks = [_embed_one(cid, text) for cid, text in items]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        valid: list[tuple[int, list[float]]] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("vector.batch_embed_child_failed", error=str(result)[:200])
                continue
            cid, vec = result
            if isinstance(vec, list) and vec:
                valid.append((cid, vec))
        if not valid:
            return

        def _do_batch() -> None:
            """在后台线程中批量子chunk向量写入。"""
            with self._lock:
                if self._closed:
                    return
                for cid, vec in valid:
                    vec_json = json.dumps(vec)
                    self._vec_conn.execute(
                        "INSERT OR REPLACE INTO memories_child_vec (rowid, embedding) VALUES (?, vec_f32(?))",
                        (cid, vec_json),
                    )
                self._vec_conn.commit()

        await asyncio.to_thread(_do_batch)

    async def delete(self, row_id: int) -> bool:
        """删除指定 rowid 的向量记录"""
        if not self._initialized or not self._vec_conn:
            return False

        def _do_delete() -> bool:
            """在后台线程中删除指定 rowid 的向量记录。"""
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

    async def delete_child(self, child_id: int) -> None:
        """删除子chunk向量。"""
        if not self._initialized or not self._vec_conn:
            return

        def _do_delete() -> None:
            """在后台线程中删除指定 child_id 的子chunk向量记录。"""
            with self._lock:
                if self._closed:
                    return
                try:
                    self._vec_conn.execute(
                        "DELETE FROM memories_child_vec WHERE rowid=?", (child_id,)
                    )
                    self._vec_conn.commit()
                except Exception as e:
                    logger.warning("vector_store.delete_child_failed", row_id=child_id, error=str(e))

        try:
            await asyncio.to_thread(_do_delete)
        except Exception as e:
            logger.warning("vector_store.delete_child_failed", row_id=child_id, error=str(e))

    async def batch_upsert(self, items: list[tuple[int, str]]) -> int:
        """批量写入向量（并发嵌入 + 单事务写入）"""
        if not self._initialized or not self._vec_conn:
            return 0

        if not items:
            return 0

        # 并发嵌入（受 Semaphore 限制，避免 API 限流）
        async def _embed_one(row_id: int, text: str) -> tuple[int, str, list[float]]:
            """对单条文本执行嵌入，受并发信号量限制。"""
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
        def _do_batch() -> Any:
            """在后台线程中以单事务批量写入向量记录。"""
            with self._lock:
                if self._closed:
                    return 0
                conn = self._vec_conn
                success = 0
                try:
                    conn.execute("BEGIN TRANSACTION")
                    for row_id, _text, vec in valid_items:
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
                        logger.debug("vector_store.batch_upsert_rollback_error: {}", exc_info=True)
                    logger.error("vector.batch_upsert_failed", error=str(e)[:200])
                    return 0

        return await asyncio.to_thread(_do_batch)

    async def search(self, query_text: str, top_k: int = 5,
                     candidate_ids: list[int] | None = None,
                     deterministic: bool = True) -> list[tuple[int, float]]:
        """基于查询文本进行向量相似度搜索，返回最相似的 top_k 条记录。

        ContextNest 论文实证: dense+HNSW 在 80% 查询上非确定 (mean Jaccard 0.611)。
        本方法通过两项措施提升确定性:
        1. tie-breaking: ``ORDER BY distance, rowid`` 消除距离并列时的乱序
        2. oversample+trim: 取 top_k*2 候选再做稳定排序, 避免边界处 k 截断引入的非确定

        Args:
            query_text: 查询文本
            top_k: 返回条数
            candidate_ids: 确定性预过滤的候选 rowid 集合 (ContextNest selector 层)
                提供时只在该集合内做向量排序, 候选集本身是确定的 (Jaccard 1.0)
            deterministic: 启用 tie-breaking + oversample
        """
        if not self._initialized or not self._vec_conn:
            return []

        vec = await self.embed(query_text)
        if not vec:
            return []

        vec_json = json.dumps(vec)
        # oversample 2x 给 tie-breaking 留余量, 再稳定 trim 到 top_k
        fetch_k = top_k * 2 if deterministic else top_k

        def _do_search() -> Any:
            """在后台线程中执行向量相似度搜索。"""
            with self._lock:
                if self._closed:
                    return []
                # sqlite-vec 的 vec0 KNN 只允许 ORDER BY distance (不允许 , rowid)
                # 所以 tie-breaking 在 Python 层做: 按 (distance, rowid) 稳定排序
                if candidate_ids is not None:
                    cand_set = set(candidate_ids)
                    oversample = min(top_k * 6, len(candidate_ids) + top_k * 2)
                    rows = self._vec_conn.execute(
                        "SELECT rowid, distance FROM memories_vec "
                        "WHERE embedding MATCH vec_f32(?) AND k=? "
                        "ORDER BY distance",
                        [vec_json, oversample],
                    ).fetchall()
                    results = [(row[0], row[1]) for row in rows if row[0] in cand_set]
                else:
                    rows = self._vec_conn.execute(
                        "SELECT rowid, distance FROM memories_vec "
                        "WHERE embedding MATCH vec_f32(?) AND k=? "
                        "ORDER BY distance",
                        [vec_json, fetch_k],
                    ).fetchall()
                    results = [(row[0], row[1]) for row in rows]
                # deterministic tie-breaking: distance 相同时按 rowid 稳定排序
                if deterministic:
                    results.sort(key=lambda r: (r[1], r[0]))
                return results[:top_k]

        try:
            return await asyncio.to_thread(_do_search)
        except Exception as e:
            logger.warning("vector_store.search_failed", error=str(e))
            return []

    async def search_child(self, query_vec: list[float], top_k: int = 20) -> list[dict]:
        """子chunk向量相似度检索。返回 [{id, distance}, ...]"""
        if not self._initialized or not self._vec_conn:
            return []
        if not query_vec:
            return []
        vec_json = json.dumps(query_vec)

        def _do_search() -> list[dict]:
            """在后台线程中执行子chunk向量相似度搜索。"""
            with self._lock:
                if self._closed:
                    return []
                rows = self._vec_conn.execute(
                    "SELECT rowid, distance FROM memories_child_vec "
                    "WHERE embedding MATCH vec_f32(?) AND k=? "
                    "ORDER BY distance",
                    (vec_json, top_k),
                ).fetchall()
                return [{"id": r[0], "distance": r[1]} for r in rows]

        try:
            return await asyncio.to_thread(_do_search)
        except Exception as e:
            logger.warning("vector_store.search_child_failed", error=str(e))
            return []

    async def search_with_hyde(self, query: str, hyde_doc: str | None = None,
                               alpha: float = 0.4, k: int = 50,
                               candidate_ids: list[str] | None = None) -> list[dict]:
        """HyDE 向量混合搜索

        原查询向量 * (1-alpha) + HyDE 向量 * alpha

        Args:
            query: 原始查询
            hyde_doc: HyDE 假设文档（None 则降级为普通搜索）
            alpha: HyDE 向量权重（默认 0.4）
            k: 返回结果数
            candidate_ids: 候选 ID 限制
        """
        # 候选 ID 转换为 int（search 需要 list[int]）
        cand_int = [int(c) for c in candidate_ids] if candidate_ids else None

        # 无 HyDE 文档或未初始化，降级到普通搜索
        if not hyde_doc or not self._initialized or not self._vec_conn:
            tuples = await self.search(query, top_k=k, candidate_ids=cand_int)
            return [{"rowid": r, "distance": d} for r, d in tuples]

        try:
            # 1. 获取原查询向量
            query_vec = await self.embed(query)
            if not query_vec:
                tuples = await self.search(query, top_k=k, candidate_ids=cand_int)
                return [{"rowid": r, "distance": d} for r, d in tuples]

            # 2. 获取 HyDE 文档向量
            hyde_vec = await self.embed(hyde_doc)
            if not hyde_vec:
                tuples = await self.search(query, top_k=k, candidate_ids=cand_int)
                return [{"rowid": r, "distance": d} for r, d in tuples]

            # 3. 混合：mixed = query_vec * (1-alpha) + hyde_vec * alpha
            mixed = [(q * (1 - alpha)) + (h * alpha) for q, h in zip(query_vec, hyde_vec, strict=False)]

            # 4. 归一化（除以 L2 范数）
            norm = sum(v * v for v in mixed) ** 0.5
            if norm > 0:
                mixed = [v / norm for v in mixed]

            # 5. 用混合向量搜索
            vec_json = json.dumps(mixed)
            fetch_k = k * 2  # oversample for tie-breaking

            def _do_hyde_search() -> list[dict]:
                with self._lock:
                    if self._closed:
                        return []
                    if cand_int is not None:
                        cand_set = set(cand_int)
                        oversample = min(k * 6, len(cand_set) + k * 2)
                        rows = self._vec_conn.execute(
                            "SELECT rowid, distance FROM memories_vec "
                            "WHERE embedding MATCH vec_f32(?) AND k=? "
                            "ORDER BY distance",
                            [vec_json, oversample],
                        ).fetchall()
                        results = [(row[0], row[1]) for row in rows if row[0] in cand_set]
                    else:
                        rows = self._vec_conn.execute(
                            "SELECT rowid, distance FROM memories_vec "
                            "WHERE embedding MATCH vec_f32(?) AND k=? "
                            "ORDER BY distance",
                            [vec_json, fetch_k],
                        ).fetchall()
                        results = [(row[0], row[1]) for row in rows]
                    # tie-breaking: distance 相同时按 rowid 稳定排序
                    results.sort(key=lambda r: (r[1], r[0]))
                    return [{"rowid": r, "distance": d} for r, d in results[:k]]

            return await asyncio.to_thread(_do_hyde_search)
        except Exception as e:
            logger.warning("vector_store.search_with_hyde_failed", error=str(e))
            tuples = await self.search(query, top_k=k, candidate_ids=cand_int)
            return [{"rowid": r, "distance": d} for r, d in tuples]
