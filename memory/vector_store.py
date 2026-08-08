from typing import Any
import json
import os
import sys
import asyncio
import hashlib
import threading
from collections import OrderedDict
from pathlib import Path
from loguru import logger


from utils.common import safe_int as _safe_int

try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

try:
    from openai import AsyncOpenAI, APITimeoutError, APIConnectionError, APIStatusError
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    AsyncOpenAI = None  # type: ignore
    APITimeoutError = TimeoutError  # type: ignore
    APIConnectionError = ConnectionError  # type: ignore
    APIStatusError = Exception  # type: ignore

# 根因修复（2026-07-29）：embed client 复用全局共享 httpx client + connect=15s + max_retries=0
# 原 AsyncOpenAI(api_key, base_url) 未传 timeout，SDK 默认 connect=5s 对跨网 SiliconFlow
# embed API 过短，网络抖动期握手失败 → embed 慢 → 触发 memory_manager 外层 3s/3.5s 超时兜底（治标）。
# 与 agnes API / http_pool 共享 client 保持同款根因修复，从源头消除外层超时的必要性。
#
# 治本修复（2026-08-05 用户"治标不治本"反馈）：read 30→5。
# 根因：embed API 正常 0.5-2s，但偶发网络波动时 read=30s 等 30s 才超时。
#   配合 max_retries=2（3次尝试）+ sleep(1) 重试间隔 = 最坏 30+1+30+1+30=92s。
#   外层 asyncio.wait_for(timeout=2.0) cancel 后，httpx 底层连接不立即释放，
#   占用连接池资源 → 后续请求也慢 → 向量检索 1.2-8s 波动（日志铁证）。
# read=5s 治本：embed 正常 0.5-2s，5s 覆盖+3s 余量；偶发慢 5s 快速失败，
#   不依赖外层 cancel，从源头消除连接池污染。
from utils.http_pool import get_shared_client as _get_embed_shared_client
import httpx as _httpx_embed
_EMBED_HTTP_TIMEOUT = _httpx_embed.Timeout(connect=15.0, read=5.0, write=10.0, pool=10.0)


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
        """根据文本查询缓存的嵌入向量，命中时更新 LRU 顺序。

        返回 list 的浅拷贝以避免调用方修改污染缓存（修复 P0 引用泄漏缺陷）。
        """
        key = self._key(text)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._hits += 1
                # 返回浅拷贝防止外部 append/extend 污染缓存
                return list(self._cache[key])
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


def _default_local_model_dir() -> str:
    """本地向量模型目录解析（优先级：env LOCAL_EMBED_MODEL_DIR > 项目内 models/ > 空）。

    项目内路径兼容 PyInstaller onedir 打包（sys._MEIPASS）：
    Windows 安装包内置 bge-small-zh-v1.5（onnx + tokenizer），
    开箱即用、默认 CPU 推理；外部环境仍可用 env 显式指定模型目录。
    """
    d = os.getenv("LOCAL_EMBED_MODEL_DIR", "").strip()
    if d:
        return d
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent.parent
    p = Path(base) / "models" / "bge-small-zh-v1.5"
    return str(p) if p.exists() else ""


class VectorStore:
    """基于 SQLite-vec 的向量存储，支持嵌入、写入、删除和相似度搜索。"""

    def __init__(self, db_path: str | Path, embed_api_key: str = "",
                 embed_base_url: str = "", embed_model: str = "BAAI/bge-m3",
                 dimensions: int = 0, embed_mode: str = "",
                 local_model_dir: str = "", local_query_prefix: str = "") -> None:
        """初始化向量存储。

        embed_mode: "local" 走香橙派本地 onnxruntime 推理（BGE-small-zh-v1.5），
                    默认 "remote" 走远程 API（向后兼容）。
        """
        self._db_path = str(db_path)
        self._embed_api_key = embed_api_key
        self._embed_base_url = embed_base_url
        self._embed_model = embed_model
        self._dimensions = dimensions
        self._embed_mode = embed_mode or os.getenv("EMBED_MODE", "remote")
        self._local_model_dir = local_model_dir or _default_local_model_dir()
        self._local_query_prefix = local_query_prefix or os.getenv("LOCAL_EMBED_QUERY_PREFIX", "")
        self._local_provider = None
        self._initialized = False
        self._closed = False
        self._lock = threading.Lock()
        self._embed_client = None
        self._vec_conn = None
        self._cache = EmbedCache(max_size=512)
        # numpy 内存暴力索引（可选，VECTOR_BRUTE_ENABLED=1 启用）：
        # BLAS 点积精确暴力 KNN（与 sqlite-vec 同 L2 度量，结果 100% 一致），
        # 13761×512 仅 23MB 常驻内存，4.5ms/次（SQLite 暴力 33.5ms）。
        # SQLite 仍是唯一数据源，本索引是加速副本，失败自动回退 SQLite。
        self._brute: Any = None
        self._brute_enabled = os.getenv("VECTOR_BRUTE_ENABLED", "0") == "1"
        self._brute_base_dir = ""
        # 单飞（single-flight）：同一文本并发调用 embed 时只发一次 API 请求，
        # 其余协程共享结果，避免 7 路检索通道并发时重复 embed 放大延迟/限流
        self._inflight: dict[str, asyncio.Future] = {}

        # 并发嵌入限制（避免 API 限流），可通过环境变量配置
        _embed_concurrency = _safe_int(os.getenv("VECTOR_EMBED_CONCURRENCY", "8"), 8)
        self._embed_semaphore = asyncio.Semaphore(_embed_concurrency)

        if self._embed_mode == "local":
            # 本地推理：不依赖远程 API Key / 网络，模型加载为懒加载。
            # 后端由 LOCAL_EMBED_BACKEND 选择：
            #   auto（默认）→ AdaptiveEmbeddingProvider 启动时探测 NPU，
            #     有 VIP9000 走长短自适应（短文本 CPU / 长文本 NPU 常驻子进程），
            #     无 NPU（纯 CPU 机器/Windows 打包版/无 sudo）自动降级全 CPU；
            #   npu → 强制走自适应（探测失败仍降级 CPU）；
            #   cpu → 显式纯 CPU（onnxruntime）。
            try:
                backend = os.getenv("LOCAL_EMBED_BACKEND", "auto")
                if backend in ("npu", "auto"):
                    from memory.npu_embed import AdaptiveEmbeddingProvider
                    self._local_provider = AdaptiveEmbeddingProvider(
                        self._local_model_dir,
                        query_prefix=self._local_query_prefix,
                    )
                else:
                    from memory.local_embed import LocalEmbeddingProvider
                    self._local_provider = LocalEmbeddingProvider(
                        self._local_model_dir,
                        query_prefix=self._local_query_prefix,
                    )
                logger.info("vector_store.local_embed_enabled backend={} model_dir={}",
                            backend, self._local_model_dir)
            except Exception as e:  # noqa: BLE001
                logger.warning("vector_store.local_embed_init_failed error={}", str(e))
                self._local_provider = None
        elif HAS_OPENAI and embed_api_key:
            self._embed_client = AsyncOpenAI(
                api_key=embed_api_key,
                base_url=embed_base_url or "https://api.siliconflow.cn/v1",
                http_client=_get_embed_shared_client(),
                timeout=_EMBED_HTTP_TIMEOUT,
                max_retries=0,  # 禁用 SDK 内部盲重试，连接错误重试无效且放大延迟
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

        def _init_db() -> tuple[Any, bool]:
            """在后台线程中初始化 SQLite 数据库，加载 sqlite_vec 扩展并创建向量虚拟表。"""
            with self._lock:
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                try:
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

                    # 维度策略：
                    # - 显式配置（dimensions > 0）时直接使用
                    # - 本地推理模式：用本地模型输出维度（BGE-small-zh-v1.5 = 512）
                    # - 未配置时查表已有维度；表不存在则用 1024 兜底
                    # 修复 P0：原代码硬编码 1024 且首次 INSERT 时 _dimensions 竞态写入，
                    # 维度不匹配时 INSERT 永久失败。
                    if self._embed_mode == "local" and self._local_provider is not None:
                        # 懒加载时此处同步加载（首次使用），拿到真实维度
                        self._local_provider.load()
                        dims = self._local_provider.dimensions or 512
                        self._dimensions = dims
                    elif self._dimensions > 0:
                        dims = self._dimensions
                    else:
                        try:
                            row = conn.execute(
                                "SELECT embedding FROM memories_vec LIMIT 1"
                            ).fetchone()
                            if row is not None and row[0] is not None:
                                import struct
                                raw = row[0]
                                if isinstance(raw, (bytes, bytearray)):
                                    dims = len(raw) // 4
                                else:
                                    dims = 1024
                            else:
                                dims = 1024
                        except sqlite3.OperationalError:
                            # 表不存在（首次初始化），用 1024 兜底
                            dims = 1024
                        # 固化检测到的维度，避免并发首 INSERT 竞态
                        self._dimensions = dims

                    # local 模式：表已存在但维度与本地模型不一致（如 1024→512）时，
                    # 不能原地改表结构，INSERT 会静默失败。检测到不匹配直接报错，
                    # 由迁移脚本（scripts/rebuild_vec_local.py）重建表并重新向量化。
                    if self._embed_mode == "local" and self._local_provider is not None:
                        try:
                            row = conn.execute(
                                "SELECT embedding FROM memories_vec LIMIT 1"
                            ).fetchone()
                            if row is not None and row[0] is not None:
                                import struct
                                raw = row[0]
                                if isinstance(raw, (bytes, bytearray)):
                                    existing_dims = len(raw) // 4
                                else:
                                    existing_dims = dims
                                if existing_dims != dims:
                                    raise RuntimeError(
                                        f"memories_vec dims={existing_dims} != local embed dims={dims}；"
                                        "请先运行 scripts/rebuild_vec_local.py 重建向量库"
                                    )
                        except sqlite3.OperationalError:
                            pass  # 表不存在（首次初始化），正常创建

                    conn.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec
                        USING vec0(embedding float[{dims}])
                    """)
                    conn.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS memories_child_vec
                        USING vec0(embedding float[{dims}])
                    """)
                    conn.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS kg_entities_vec
                        USING vec0(embedding float[{dims}])
                    """)
                    conn.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS kg_relations_vec
                        USING vec0(embedding float[{dims}])
                    """)
                    conn.commit()
                    return conn, is_fat
                except Exception:
                    # 修复资源泄漏：sqlite_vec.load 失败时必须 close 连接
                    try:
                        conn.close()
                    except Exception:
                        pass
                    raise

        self._vec_conn, is_fat = await asyncio.to_thread(_init_db)

        self._initialized = True
        pragma_desc = "DELETE+cache" if is_fat else "WAL+cache+mmap"
        logger.info("vector_store.ready", pragmas=pragma_desc)

        # numpy 内存暴力索引（VECTOR_BRUTE_ENABLED=1 时启用）：
        # 优先从磁盘恢复（{db_stem}_brute/），失败则从 SQLite 全量重建。
        # 加载是 CPU/IO 密集，走 to_thread；失败置 None 回退 SQLite 暴力 KNN。
        if self._brute_enabled:
            from memory.numpy_index import NumpyBruteIndex
            base_dir = Path(self._db_path).parent / (Path(self._db_path).stem + "_brute")
            self._brute_base_dir = str(base_dir)
            self._brute = NumpyBruteIndex(dim=self._dimensions, base_dir=base_dir)

            def _load_brute() -> None:
                with self._lock:
                    if self._closed:
                        return
                    if not self._brute.load():
                        self._brute.load_from_db(self._vec_conn)

            try:
                await asyncio.to_thread(_load_brute)
                if self._brute.ready:
                    logger.info("vector_store.brute_ready", base_dir=self._brute_base_dir)
                else:
                    logger.warning("vector_store.brute_unavailable error={}",
                                   getattr(self._brute, "_load_error", ""))
                    self._brute = None
            except Exception as e:  # noqa: BLE001
                logger.warning("vector_store.brute_init_failed error={}", str(e))
                self._brute = None

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
        # 本地推理 Provider：释放 ONNX session 与 tokenizer
        if self._local_provider is not None:
            self._local_provider.close()
            self._local_provider = None
        # CodeRabbit 修复：不关闭 _embed_client，因为它复用全局共享 httpx client
        # （_get_embed_shared_client）。关闭 _embed_client 会关闭共享 httpx client，
        # 影响其他 VectorStore 实例。共享 client 生命周期由 close_shared_client() 统一管理。
        # AsyncOpenAI 实例本身无其他需清理的资源（底层 httpx 由共享池管理）。
        # numpy 内存暴力索引：关闭前保存（若启用且已加载）
        if self._brute is not None:
            try:
                self._brute.save()
                self._brute.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("vector_store.brute_close_failed error={}", str(e))
            self._brute = None
        if self._cache.stats["size"] > 0:
            logger.info("vector_store.cache_stats", **self._cache.stats)

    async def embed(self, text: str) -> list[float]:
        """生成文本的嵌入向量，优先使用缓存，失败时自动重试。

        单飞（single-flight）：同一文本的并发调用共享一次 API 请求。
        """
        # CodeRabbit 修复：close() 后 _closed=True，但 _embed_client 复用全局共享 httpx
        # client 不会被 close() 置空。若不检查 _closed，close() 后仍会发起 embed 请求，
        # 违反生命周期契约。与文件内其他方法（_vec_conn 操作）的 _closed 守卫一致。
        # 生命周期契约：close_shared_client() 必须在所有 VectorStore.close() 完成且
        # 无在途 embed 请求后调用（由应用关闭顺序保证）。
        if self._closed:
            return []
        if self._embed_mode != "local" and not self._embed_client:
            return []

        cached = self._cache.get(text)
        if cached:
            return cached

        # 单飞：已有同文本在途请求则直接共享结果，不重复打 API
        inflight = self._inflight.get(text)
        if inflight is not None:
            try:
                return await asyncio.shield(inflight)
            except Exception:
                return []

        future = asyncio.get_running_loop().create_future()
        self._inflight[text] = future
        try:
            vec = await self._do_embed(text)
            if vec:
                self._cache.put(text, vec)
            future.set_result(vec)
            return vec
        except Exception as e:
            # 等待者同样拿到空结果（不传播异常，调用方均有兜底）
            future.set_result([])
            logger.warning("vector_store.embed_singleflight_failed", error=str(e))
            return []
        finally:
            self._inflight.pop(text, None)

    async def _do_embed(self, text: str) -> list[float]:
        """实际生成嵌入向量（本地推理或远程 API，含重试）。"""
        # 本地推理（香橙派 onnxruntime CPU）：CPU 密集，走 to_thread 不阻塞事件循环；
        # 无网络依赖、无重试必要，失败即返回空（调用方均有兜底）。
        if self._embed_mode == "local":
            if self._local_provider is None:
                return []
            vec = await asyncio.to_thread(self._local_provider.embed, text)
            if vec and self._dimensions and len(vec) != self._dimensions:
                logger.warning("vector_store.dimension_mismatch",
                               expected=self._dimensions, actual=len(vec))
            return vec
        # 治本修复（2026-08-05 用户"治标不治本"反馈）：max_retries 2→1。
        # 根因：embed 偶发慢时重试也慢（网络波动不会 1s 内恢复），
        #   read=5s + 重试2次 = 最坏 5+1+5+1+5=17s，远超外层 wait_for(2s) 兜底。
        #   重试叠加延迟与 agnes timeout 重试同理，偶发慢重试无意义。
        # 重试1次：给瞬时抖动一次机会，但不无限叠加。最坏 5+1+5=11s。
        # 配合 read=5s，正常 embed 0.5-2s 不受影响，偶发慢快速失败。
        max_retries = 1
        for attempt in range(max_retries + 1):
            try:
                # CodeRabbit 修复：移除 asyncio.wait_for(timeout=10.0)。
                # 原内层 10s 超时与 _EMBED_HTTP_TIMEOUT(connect=15s) 冲突——
                # connect 15s 期间内层 10s 会先触发，导致 connect 永远用不满 15s，
                # 且 httpx 层超时保护被 wait_for 截断失效。
                # 现由 httpx _EMBED_HTTP_TIMEOUT(connect=15, read=5, write=10, pool=10) 统一保护。
                # embed API 正常 0.5-2s，connect=15s 覆盖网络抖动，无需应用层 wait_for。
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
                return vec
            except (APITimeoutError, APIConnectionError) as e:
                # 瞬时错误：超时/连接错误，重试有效（网络抖动可能恢复）
                if attempt < max_retries:
                    logger.warning("vector_store.embed_transient_retry",
                                   attempt=attempt + 1, error=str(e))
                    await asyncio.sleep(1)
                    continue
                logger.warning("vector_store.embed_transient_final",
                               error=str(e), attempts=max_retries + 1)
                return []
            except APIStatusError as e:
                # HTTP 状态错误：5xx 重试，4xx（认证/验证/限流）立即放弃不重试
                if hasattr(e, 'status_code') and e.status_code >= 500 and attempt < max_retries:
                    logger.warning("vector_store.embed_5xx_retry",
                                   attempt=attempt + 1, status=e.status_code)
                    await asyncio.sleep(1)
                    continue
                logger.warning("vector_store.embed_status_error",
                               status=getattr(e, 'status_code', 0), error=str(e))
                return []
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
                    # 双写 HNSW 加速索引（同锁内保证与 SQLite 顺序一致）
                    if self._brute is not None:
                        self._brute.upsert("memories_vec", row_id, vec)
                    return True
                except Exception as e:
                    try:
                        self._vec_conn.execute("ROLLBACK")
                    except Exception as re:
                        logger.debug("vector_store.upsert_rollback_error", error=str(re))
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
                    # 双写 HNSW 加速索引
                    if self._brute is not None:
                        self._brute.upsert("memories_child_vec", child_id, vec)
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
                try:
                    self._vec_conn.execute("BEGIN TRANSACTION")
                    for cid, vec in valid:
                        vec_json = json.dumps(vec)
                        self._vec_conn.execute(
                            "INSERT OR REPLACE INTO memories_child_vec (rowid, embedding) VALUES (?, vec_f32(?))",
                            (cid, vec_json),
                        )
                        # 双写内存暴力索引（事务内逐条同步，保证一致）
                        if self._brute is not None:
                            self._brute.upsert("memories_child_vec", cid, vec)
                    self._vec_conn.commit()
                except Exception as e:
                    try:
                        self._vec_conn.execute("ROLLBACK")
                    except Exception as re:
                        logger.debug("vector_store.batch_upsert_children_rollback_error", error=str(re))
                    logger.warning("vector_store.batch_upsert_children_failed", error=str(e))

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
                    # 双写 HNSW 加速索引（mark_deleted 排除该节点）
                    if self._brute is not None:
                        self._brute.delete("memories_vec", row_id)
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
                    # 双写 HNSW 加速索引
                    if self._brute is not None:
                        self._brute.delete("memories_child_vec", child_id)
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
        def _do_batch() -> int:
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
                            # 双写 HNSW 加速索引
                            if self._brute is not None:
                                self._brute.upsert("memories_vec", row_id, vec)
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
                    except Exception as re:
                        logger.debug("vector_store.batch_upsert_rollback_error", error=str(re))
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

        def _do_search() -> list[tuple[int, float]]:
            """在后台线程中执行向量相似度搜索。"""
            with self._lock:
                if self._closed:
                    return []
                # HNSW 加速路径：None=索引不可用/失败 → 回退 SQLite；[]=无匹配
                if self._brute is not None:
                    brute_res = self._brute.search(
                        "memories_vec", vec, top_k,
                        candidate_ids=candidate_ids,
                        ef=max(fetch_k, top_k),
                    )
                    if brute_res is not None:
                        return brute_res[:top_k]
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
                # 内存暴力加速路径：None=不可用/失败 → 回退 SQLite
                if self._brute is not None:
                    brute_res = self._brute.search(
                        "memories_child_vec", query_vec, top_k, ef=top_k * 2)
                    if brute_res is not None:
                        return [{"id": r[0], "distance": r[1]} for r in brute_res]
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
            mixed = [(q * (1 - alpha)) + (h * alpha) for q, h in zip(query_vec, hyde_vec)]

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
                    # 内存暴力加速路径：None=不可用/失败 → 回退 SQLite
                    if self._brute is not None:
                        brute_res = self._brute.search(
                            "memories_vec", mixed, k,
                            candidate_ids=cand_int,
                            ef=k * 2,
                        )
                        if brute_res is not None:
                            return [{"rowid": r[0], "distance": r[1]}
                                    for r in brute_res[:k]]
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

    async def upsert_kg_entity(self, row_id: int, text: str) -> bool:
        """写入或更新 KG 实体向量（先删后插）。"""
        if not self._initialized or not self._vec_conn:
            return False
        vec = await self.embed(text)
        if not vec:
            return False
        vec_json = json.dumps(vec)

        def _do_upsert() -> bool:
            with self._lock:
                if self._closed:
                    return False
                try:
                    self._vec_conn.execute("BEGIN TRANSACTION")
                    try:
                        self._vec_conn.execute(
                            "DELETE FROM kg_entities_vec WHERE rowid=?", [row_id]
                        )
                    except Exception as de:
                        logger.debug("vector_store.upsert_kg_entity_delete_old_failed", row_id=row_id, error=str(de))
                    self._vec_conn.execute(
                        "INSERT INTO kg_entities_vec(rowid, embedding) VALUES (?, vec_f32(?))",
                        [row_id, vec_json],
                    )
                    self._vec_conn.commit()
                    # 双写 HNSW 加速索引
                    if self._brute is not None:
                        self._brute.upsert("kg_entities_vec", row_id, vec)
                    return True
                except Exception as e:
                    try:
                        self._vec_conn.execute("ROLLBACK")
                    except Exception as re:
                        logger.debug("vector_store.upsert_kg_entity_rollback_error", error=str(re))
                    logger.warning("vector_store.upsert_kg_entity_failed", row_id=row_id, error=str(e))
                    return False

        return await asyncio.to_thread(_do_upsert)

    async def upsert_kg_relation(self, row_id: int, text: str) -> bool:
        """写入或更新 KG 关系向量（先删后插）。"""
        if not self._initialized or not self._vec_conn:
            return False
        vec = await self.embed(text)
        if not vec:
            return False
        vec_json = json.dumps(vec)

        def _do_upsert() -> bool:
            with self._lock:
                if self._closed:
                    return False
                try:
                    self._vec_conn.execute("BEGIN TRANSACTION")
                    try:
                        self._vec_conn.execute(
                            "DELETE FROM kg_relations_vec WHERE rowid=?", [row_id]
                        )
                    except Exception as de:
                        logger.debug("vector_store.upsert_kg_relation_delete_old_failed", row_id=row_id, error=str(de))
                    self._vec_conn.execute(
                        "INSERT INTO kg_relations_vec(rowid, embedding) VALUES (?, vec_f32(?))",
                        [row_id, vec_json],
                    )
                    self._vec_conn.commit()
                    # 双写 HNSW 加速索引
                    if self._brute is not None:
                        self._brute.upsert("kg_relations_vec", row_id, vec)
                    return True
                except Exception as e:
                    try:
                        self._vec_conn.execute("ROLLBACK")
                    except Exception as re:
                        logger.debug("vector_store.upsert_kg_relation_rollback_error", error=str(re))
                    logger.warning("vector_store.upsert_kg_relation_failed", row_id=row_id, error=str(e))
                    return False

        return await asyncio.to_thread(_do_upsert)

    async def search_kg_entities(self, query_text: str, top_k: int = 5) -> list[tuple[int, float]]:
        """搜索 KG 实体向量，返回 [(rowid, distance), ...]。"""
        if not self._initialized or not self._vec_conn:
            return []
        vec = await self.embed(query_text)
        if not vec:
            return []
        vec_json = json.dumps(vec)
        fetch_k = top_k * 2

        def _do_search() -> list[tuple[int, float]]:
            with self._lock:
                if self._closed:
                    return []
                # 内存暴力加速路径：None=不可用/失败 → 回退 SQLite
                if self._brute is not None:
                    brute_res = self._brute.search(
                        "kg_entities_vec", vec, top_k, ef=top_k * 2)
                    if brute_res is not None:
                        return brute_res
                rows = self._vec_conn.execute(
                    "SELECT rowid, distance FROM kg_entities_vec "
                    "WHERE embedding MATCH vec_f32(?) AND k=? "
                    "ORDER BY distance",
                    [vec_json, fetch_k],
                ).fetchall()
                results = [(row[0], row[1]) for row in rows]
                results.sort(key=lambda r: (r[1], r[0]))
                return results[:top_k]

        try:
            return await asyncio.to_thread(_do_search)
        except Exception as e:
            logger.warning("vector_store.search_kg_entities_failed", error=str(e))
            return []

    async def search_kg_relations(self, query_text: str, top_k: int = 5) -> list[tuple[int, float]]:
        """搜索 KG 关系向量，返回 [(rowid, distance), ...]。"""
        if not self._initialized or not self._vec_conn:
            return []
        vec = await self.embed(query_text)
        if not vec:
            return []
        vec_json = json.dumps(vec)
        fetch_k = top_k * 2

        def _do_search() -> list[tuple[int, float]]:
            with self._lock:
                if self._closed:
                    return []
                # 内存暴力加速路径：None=不可用/失败 → 回退 SQLite
                if self._brute is not None:
                    brute_res = self._brute.search(
                        "kg_relations_vec", vec, top_k, ef=top_k * 2)
                    if brute_res is not None:
                        return brute_res
                rows = self._vec_conn.execute(
                    "SELECT rowid, distance FROM kg_relations_vec "
                    "WHERE embedding MATCH vec_f32(?) AND k=? "
                    "ORDER BY distance",
                    [vec_json, fetch_k],
                ).fetchall()
                results = [(row[0], row[1]) for row in rows]
                results.sort(key=lambda r: (r[1], r[0]))
                return results[:top_k]

        try:
            return await asyncio.to_thread(_do_search)
        except Exception as e:
            logger.warning("vector_store.search_kg_relations_failed", error=str(e))
            return []
