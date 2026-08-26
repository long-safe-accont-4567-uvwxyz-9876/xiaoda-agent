import asyncio
import hashlib
import json
import math
import os
import struct
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from utils.atomic_write import atomic_write
except ImportError:  # pragma: no cover
    atomic_write = None  # type: ignore[assignment]
except Exception:  # pragma: no cover
    logger.exception("vector_store.atomic_write_import_unexpected")
    atomic_write = None  # type: ignore[assignment]


from local_ai.integration.embedding import LocalEmbeddingService, LocalEmbeddingUnavailableError
from local_ai.integration.reranker import LocalModelUnavailableError
from local_ai.runtimes.base import RuntimeValidationError

try:
    import sqlite_vec
    # 契约探针：只验可导入不验符号——陈旧 sqlite_vec 缺 load 时会在
    # _init_db_sync 使用点爆 AttributeError（同 rust_core 案例）
    if not hasattr(sqlite_vec, "load"):
        raise AttributeError("sqlite_vec.load missing")
    HAS_SQLITE_VEC = True
except (ImportError, AttributeError):
    HAS_SQLITE_VEC = False

try:
    from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    AsyncOpenAI = None  # type: ignore
    APITimeoutError = TimeoutError  # type: ignore
    APIConnectionError = ConnectionError  # type: ignore
    APIStatusError = Exception  # type: ignore

# numpy 为可选依赖：EmbedCache 磁盘持久化用它（npz 存储），不可用时降级 pickle
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore[assignment]
    _HAS_NUMPY = False

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
import httpx as _httpx_embed

from config_constants import env_flag
from utils.http_pool import get_shared_client as _get_embed_shared_client
from utils.metrics import metrics
from utils.thread_pools import to_thread_heavy, to_thread_hot

_EMBED_HTTP_TIMEOUT = _httpx_embed.Timeout(connect=15.0, read=5.0, write=10.0, pool=10.0)


class EmbedCache:
    """基于 LRU 的文本嵌入向量缓存（可选磁盘持久化）。

    P1-7: 进程重启后复用已缓存的嵌入向量，避免冷启动重复调用 embed API。
    持久化策略：
    - 持久化路径非空时，启动加载磁盘缓存，每次 put 后原子写盘（tmp + os.replace）
    - numpy 可用时用 .npz（float32，~1MB/512 条），否则降级 pickle
    - 加载/保存失败静默降级为空缓存/跳过写盘，不影响主流程
    """

    def __init__(self, max_size: int = 256, persist_path: str = "") -> None:
        """初始化嵌入缓存。

        persist_path: 磁盘缓存文件路径，为空则纯内存缓存（不持久化）。
        """
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._max_size = max_size
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()
        self._persist_path = persist_path
        if persist_path:
            self._load_persisted()

    @staticmethod
    def _key(text: str) -> str:
        """生成缓存键 — 使用完整文本的 SHA256 哈希，避免截断导致碰撞。"""
        return hashlib.sha256(text.encode()).hexdigest()[:32]

    def _load_persisted(self) -> None:
        """从磁盘加载持久化的缓存条目（缺失/损坏时静默降级为空缓存）。"""
        path = Path(self._persist_path)
        if not path.exists():
            return
        try:
            if _HAS_NUMPY:
                data = np.load(self._persist_path, allow_pickle=False)
                keys = data["keys"]
                vecs = data["vecs"]
                # 整矩阵一次 C 层 tolist()：N 行 × 512 维从 N 次行级转换收敛为
                # 1 次（npz 的 vecs 恒为 float32 ndarray，tolist 逐元素提升为
                # Python float，与旧逐行 v.tolist() 值语义逐位一致）
                n = min(len(keys), len(vecs), self._max_size)
                if n > 0:
                    for k, row in zip(keys[:n], vecs[:n].tolist()):
                        self._cache[str(k)] = row
            else:
                import pickle
                with open(self._persist_path, "rb") as f:
                    self._cache.update(pickle.load(f))
                # 超容量时按插入序淘汰（pickle 无 LRU 顺序，保留前 max_size 条）
                while len(self._cache) > self._max_size:
                    self._cache.popitem(last=False)
            logger.info("embed_cache.loaded", path=self._persist_path,
                        size=len(self._cache))
        except Exception as e:
            logger.warning("embed_cache.load_failed", path=self._persist_path,
                           error=str(e))
            self._cache.clear()

    def _save_persisted(self) -> None:
        """将缓存条目原子写入磁盘（失败静默降级，不影响主流程）。"""
        try:
            if _HAS_NUMPY:
                import io
                buf = io.BytesIO()
                np.savez(
                    buf,
                    keys=np.array(list(self._cache.keys())),
                    vecs=np.array(list(self._cache.values()), dtype=np.float32),
                )
                content = buf.getvalue()
            else:
                import pickle
                content = pickle.dumps(dict(self._cache), protocol=pickle.HIGHEST_PROTOCOL)

            if atomic_write is not None:
                atomic_write(self._persist_path, content)
            else:
                # fallback: 固定 tmp 方式（atomic_write 不可用时）
                tmp = self._persist_path + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(content)
                os.replace(tmp, self._persist_path)
        except Exception as e:
            logger.debug("embed_cache.save_failed", path=self._persist_path,
                         error=str(e))

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
            # 持锁原子写盘：缓存写频次低（每次 embed 请求一次），
            # 全量 ~1MB 写盘 <50ms，换取重启后免重复 embed
            if self._persist_path:
                self._save_persisted()

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            if self._persist_path:
                try:
                    Path(self._persist_path).unlink(missing_ok=True)
                except OSError:
                    logger.debug("embed_cache.clear_unlink_failed", path=self._persist_path, exc_info=True)

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
                 local_model_dir: str = "", local_query_prefix: str = "",
                 embedding_service: LocalEmbeddingService | None = None) -> None:
        """初始化向量存储。

        embed_mode: "remote"（默认）走 SiliconFlow 远程 API（配 SILICONFLOW_API_KEY
                    或 EMBED_API_KEY；启动时 key 缺失且本地模型可用则自动降级 local，
                    两者皆无则明确告警"检索不可用"）；
                    "local" 走香橙派本地 onnxruntime 推理（BGE-small-zh-v1.5）。
        """
        self._db_path = str(db_path)
        self._embed_api_key = embed_api_key
        self._embed_base_url = embed_base_url
        self._embed_model = embed_model
        self._dimensions = dimensions
        self._dimensions_explicit = dimensions > 0
        self._embed_mode = embed_mode or os.getenv("EMBED_MODE", "remote")
        self._local_model_dir = local_model_dir or _default_local_model_dir()
        self._local_query_prefix = local_query_prefix or os.getenv("LOCAL_EMBED_QUERY_PREFIX", "")
        self._local_provider = embedding_service
        self._selected_local_service = embedding_service is not None
        self._initialized = False
        self._closed = False
        # 自动重建状态：维度不匹配时由 _init_db_sync 置 True，init() 异步触发重建
        self._needs_rebuild = False
        self._rebuild_from_dims = 0
        self._rebuild_to_dims = 0
        self._rebuild_in_progress = False
        # 裸 create_task 的返回值若无强引用会被 GC 中途回收（emotion_state.save
        # 同款已知问题）——_auto_rebuild 任务挂到实例属性上保活
        self._rebuild_task: asyncio.Task | None = None
        self._lock = threading.Lock()
        self._embed_client = None
        self._vec_conn = None
        self._cache = EmbedCache(max_size=512, persist_path=self._embed_cache_path())
        # numpy 内存暴力索引（可选，VECTOR_BRUTE_ENABLED=1 启用）：
        # BLAS 点积精确暴力 KNN（与 sqlite-vec 同 L2 度量，结果 100% 一致），
        # 13761×512 仅 23MB 常驻内存，4.5ms/次（SQLite 暴力 33.5ms）。
        # SQLite 仍是唯一数据源，本索引是加速副本，失败自动回退 SQLite。
        self._brute: Any = None
        self._brute_enabled = env_flag("VECTOR_BRUTE_ENABLED", False)
        self._brute_base_dir = ""
        # 单飞（single-flight）：同一文本并发调用 embed 时只发一次 API 请求，
        # 其余协程共享结果，避免 7 路检索通道并发时重复 embed 放大延迟/限流
        self._inflight: dict[tuple[Any, str], asyncio.Future] = {}
        self._embedding_selection_key: tuple[str, int] | int | None = None

        if self._embed_mode == "local":
            # 本地推理：不依赖远程 API Key / 网络，模型加载为懒加载。
            # 后端由 LOCAL_EMBED_BACKEND 选择：
            #   auto（默认）→ AdaptiveEmbeddingProvider 启动时探测 NPU，
            #     有 VIP9000 走长短自适应（短文本 CPU / 长文本 NPU 常驻子进程），
            #     无 NPU（纯 CPU 机器/Windows 打包版/无 sudo）自动降级全 CPU；
            #   npu → 强制走自适应（探测失败仍降级 CPU）；
            #   cpu → 显式纯 CPU（onnxruntime）。
            if self._local_provider is None:
                self._local_provider = self._build_local_provider()
            if self._local_provider is not None:
                logger.info("vector_store.local_embed_enabled backend={} model_dir={}",
                            os.getenv("LOCAL_EMBED_BACKEND", "auto"), self._local_model_dir)
            else:
                logger.warning("vector_store.local_embed_init_failed provider=None")
        else:
            # remote（默认，硅基流动）：配 key 走远程 API；key 缺失时若本地模型
            # 可用则降级本地，否则明确告警。绝不静默进入"检索全空"状态。
            self._embed_client = self._build_remote_client()
            if self._embed_client is not None:
                logger.info("vector_store.remote_embed_enabled base_url={} model={}",
                            self._embed_base_url or "https://api.siliconflow.cn/v1",
                            self._embed_model)
            else:
                fallback = self._build_local_provider()
                if fallback is not None:
                    self._local_provider = fallback
                    self._embed_mode = "local"
                    logger.warning(
                        "vector_store.embed_fallback_to_local reason=missing_api_key "
                        "backend={} model_dir={}",
                        os.getenv("LOCAL_EMBED_BACKEND", "auto"), self._local_model_dir)
                else:
                    logger.warning(
                        "vector_store.embed_unavailable reason=missing_api_key_and_no_local "
                        "检索不可用：配置 SILICONFLOW_API_KEY（或 EMBED_API_KEY）"
                        "或部署本地 bge 模型后重启")

    def _embed_cache_path(self) -> str:
        """EmbedCache 磁盘持久化路径（与 db 同目录，按模型区分避免维度冲突）。

        例: data/embed_cache_BAAI_bge-m3.npz。模型升级（维度变化）时文件名不同，
        旧缓存自然失效，不会出现维度不匹配。
        """
        _model_slug = self._embed_model.replace("/", "_").replace(":", "_")
        _base = Path(self._db_path).parent
        try:
            _base.mkdir(parents=True, exist_ok=True)
        except OSError:
            return ""
        return str(_base / f"embed_cache_{_model_slug}.npz")

    # ── embedding 引擎构建 / 热切换（WebUI 本地部署页）──────────

    def _build_local_provider(self) -> Any:
        """按 LOCAL_EMBED_BACKEND 构建本地 embedding provider（幂等）。"""
        try:
            return LocalEmbeddingService.bundled(
                self._local_model_dir,
                query_prefix=self._local_query_prefix,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("vector_store.local_embed_build_failed error={}", str(e))
            return None

    def _build_remote_client(self) -> Any:
        """构建远程 OpenAI 兼容 embedding client（硅基流动默认，幂等）。"""
        if not HAS_OPENAI or not self._embed_api_key:
            return None
        return AsyncOpenAI(
            api_key=self._embed_api_key,
            base_url=self._embed_base_url or "https://api.siliconflow.cn/v1",
            http_client=_get_embed_shared_client(),
            timeout=_EMBED_HTTP_TIMEOUT,
            max_retries=0,  # 禁用 SDK 内部盲重试，连接错误重试无效且放大延迟
        )

    def embed_engine_status(self) -> dict:
        """当前 embedding 引擎状态（WebUI 本地部署页展示）。"""
        provider = self._local_provider
        running = False
        if provider is not None:
            try:
                running = bool(getattr(provider, "ready", False))
            except (AttributeError, RuntimeError):  # noqa: BLE001
                running = False
            except Exception:  # noqa: BLE001
                logger.exception("vector_store.embed_ready_check_unexpected")
                running = False
        return {
            "mode": self._embed_mode,
            "source": getattr(provider, "source", None),
            "engine_running": running,
            "backend": os.getenv("LOCAL_EMBED_BACKEND", "auto"),
            "api_configured": bool(
                self._embed_api_key or os.getenv("SILICONFLOW_API_KEY", "")),
            "model_dir": self._local_model_dir,
            "dimensions": self._dimensions,
        }

    def set_embed_mode(self, mode: str) -> dict:
        """运行时切换 embedding 引擎（local=本地模型 / remote=远程 API）。

        幂等：目标模式与当前一致时直接返回现状。切换时释放旧本地引擎资源
        （onnxruntime session / NPU 常驻进程）；远程 client 由共享 httpx
        连接池管理，仅释放引用。目标引擎不可用时保持显式选中状态。
        """
        mode = (mode or "remote").strip().lower()
        if mode not in ("local", "remote"):
            mode = "remote"
        with self._lock:
            if mode == self._embed_mode:
                return self.embed_engine_status()
            old_provider = self._local_provider
            self._local_provider = None
            self._embed_client = None
            if old_provider is not None:
                try:
                    old_provider.close()
                except Exception as e:  # noqa: BLE001
                    logger.warning("vector_store.embed_mode_old_provider_close_failed error={}", str(e))
            self._embed_mode = mode
            if mode == "local":
                provider = self._build_local_provider()
                if provider is not None:
                    self._local_provider = provider
                    logger.info("vector_store.embed_mode_switched mode=local")
                    # 切换后检测维度变化：local provider 维度可同步获取
                    self._check_dimension_after_switch()
                else:
                    logger.warning("vector_store.embed_mode_switch_failed mode=local")
            else:
                client = self._build_remote_client()
                if client is not None:
                    self._embed_client = client
                    logger.info("vector_store.embed_mode_switched mode=remote")
                    # remote 模式维度需首次 API 调用才知道，这里不检测，
                    # 由后续 embed 时 _validate_dimension / init 自动检测
                else:
                    logger.warning("vector_store.embed_mode_switch_failed mode=remote")
            return self.embed_engine_status()

    def _check_dimension_after_switch(self) -> None:
        """切换 embed 引擎后检测维度变化，不匹配则触发异步自动重建。

        仅 local 模式可同步获取 provider 维度；remote 模式靠 embed 时自动检测。
        """
        if self._rebuild_in_progress or self._needs_rebuild:
            return  # 已在重建或已标记
        if not self._vec_conn:
            return  # 未初始化，无需检测
        # 获取新引擎的目标维度
        target_dims = 0
        if self._embed_mode == "local" and self._local_provider is not None:
            try:
                # 懒加载 provider 以拿到真实维度
                if not getattr(self._local_provider, "ready", False):
                    self._local_provider.load()
                target_dims = getattr(self._local_provider, "dimensions", 0) or 0
            except Exception as exc:  # noqa: BLE001
                logger.debug("vector_store.dim_probe_skipped: {}", str(exc)[:120])
                return
        if target_dims <= 0:
            return  # 无法确定目标维度，跳过
        # 读取现有表维度
        try:
            row = self._vec_conn.execute(
                "SELECT embedding FROM memories_vec LIMIT 1"
            ).fetchone()
            if row is None or row[0] is None:
                return  # 空表，无需重建
            raw = row[0]
            existing_dims = len(raw) // 4 if isinstance(raw, (bytes, bytearray)) else 0
            if existing_dims > 0 and existing_dims != target_dims:
                self._needs_rebuild = True
                self._rebuild_from_dims = existing_dims
                self._rebuild_to_dims = target_dims
                logger.warning(
                    "vector_store.dimension_mismatch_after_switch "
                    "existing={} target={} hint=auto rebuild triggered",
                    existing_dims, target_dims,
                )
                # 尝试异步触发重建（有事件循环时）
                try:
                    self._spawn_auto_rebuild()
                except RuntimeError:
                    # 无事件循环（同步上下文），由下次 init() 触发
                    logger.debug("vector_store.rebuild_deferred_no_loop")
        except Exception as e:  # noqa: BLE001
            logger.debug("vector_store.dimension_check_after_switch_failed error={}", str(e))

    def start_local_engine(self) -> dict:
        """启动本地 embedding 引擎：确保 local 模式 + 预加载模型（含 NPU 探测）。

        WebUI 本地部署页"启动"按钮：使用本地模型前必须先启动。
        """
        with self._lock:
            if self._embed_mode != "local":
                self._embed_mode = "local"
            if self._local_provider is None:
                self._local_provider = self._build_local_provider()
            provider = self._local_provider
            if provider is not None:
                try:
                    ok = provider.load()  # 幂等：已加载直接返回 True
                except Exception as e:  # noqa: BLE001
                    if isinstance(e, LocalModelUnavailableError):
                        raise
                    ok = False
                    logger.warning("vector_store.local_engine_start_failed error={}", str(e))
                if ok:
                    logger.info("vector_store.local_engine_started")
            return self.embed_engine_status()

    def stop_local_engine(self) -> dict:
        """停止本地 embedding 引擎：释放 onnxruntime session / NPU 常驻进程。"""
        with self._lock:
            if self._local_provider is not None:
                try:
                    self._local_provider.close()
                except Exception as e:  # noqa: BLE001
                    logger.warning("vector_store.local_engine_stop_failed error={}", str(e))
            if not self._selected_local_service:
                self._local_provider = None
            logger.info("vector_store.local_engine_stopped")
            return self.embed_engine_status()

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

    def get_memories_vec_rowids(self) -> set[int]:
        """获取 memories_vec 表已存在的 rowid 集合（用于主表↔向量索引对账）。

        返回空集合表示向量库未初始化、表为空或查询失败（由调用方按"无索引"处理）。
        """
        if not self._initialized or not self._vec_conn:
            return set()
        try:
            with self._lock:
                rows = self._vec_conn.execute("SELECT rowid FROM memories_vec").fetchall()
            return {int(r[0]) for r in rows}
        except Exception as e:
            logger.warning("vector_store.get_memories_vec_rowids_failed", error=str(e))
            return set()

    async def init(self) -> None:
        """初始化 SQLite 数据库，加载 sqlite_vec 扩展并创建向量虚拟表。"""
        if not HAS_SQLITE_VEC:
            logger.warning("vector_store.sqlite_vec_missing")
            return

        if (
            not self._dimensions_explicit
            and self._embed_mode == "local"
            and self._local_provider is not None
        ):
            resolver = getattr(self._local_provider, "resolve_dimensions", None)
            if resolver is not None:
                resolved_dimensions = await resolver()
                if resolved_dimensions > 0:
                    self._dimensions = resolved_dimensions

        self._vec_conn, is_fat = await to_thread_hot(self._init_db_sync)

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


            try:
                await to_thread_hot(self._load_brute_sync)
                if self._brute.ready:
                    logger.info("vector_store.brute_ready", base_dir=self._brute_base_dir)
                else:
                    logger.warning("vector_store.brute_unavailable error={}",
                                   getattr(self._brute, "_load_error", ""))
                    self._brute = None
            except Exception as e:  # noqa: BLE001
                logger.warning("vector_store.brute_init_failed error={}", str(e))
                self._brute = None

        # 维度不匹配自动重建：_init_db_sync 检测到表维度≠模型维度时置 _needs_rebuild
        # 异步触发重建（不阻塞 init 返回），重建完成后重新初始化向量库连接。
        if self._needs_rebuild and not self._rebuild_in_progress:
            self._spawn_auto_rebuild()

    def _spawn_auto_rebuild(self) -> None:
        """强引用持有地启动 _auto_rebuild（裸 create_task 会被 GC 中途回收）。

        防重入双保险：本方法的在途任务检查 + _auto_rebuild 入口的
        _rebuild_in_progress 标志。两个触发点（init / _check_dimension_after_switch）
        统一经由此处收口，判断口径一致。
        """
        if self._rebuild_task is not None and not self._rebuild_task.done():
            return
        task = asyncio.create_task(self._auto_rebuild())
        self._rebuild_task = task
        task.add_done_callback(self._on_rebuild_done)

    def _on_rebuild_done(self, task: asyncio.Task) -> None:
        """重建任务收尾：清引用；异常不静默（CancelledError 属正常取消不算）。"""
        if self._rebuild_task is task:
            self._rebuild_task = None
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("vector_store.auto_rebuild_task_failed error={}", str(exc))

    async def _auto_rebuild(self) -> None:
        """维度不匹配时自动重建向量库（异步后台执行）。

        触发时机：
        1. init() 启动时检测到表维度≠当前模型维度
        2. set_embed_mode() 切换模型后维度变化

        实现方式：调用现有重建脚本（scripts/rebuild_vec_local.py 或
        rebuild_vec_remote.py）作为子进程执行。脚本负责：
        - 备份旧库 → 读 agent.db 文本源 → 重新向量化 → 重建 4 张表 → 原子替换
        重建完成后本方法重新初始化向量库连接，使新维度立即生效。

        安全保障：
        - _rebuild_in_progress 标记防止并发重建
        - 重建期间向量检索不可用（INSERT 会失败，search 返回空）
        - 旧库已备份（.bak-<ts>），失败可回滚
        """
        if self._rebuild_in_progress:
            return
        self._rebuild_in_progress = True
        from_dims = self._rebuild_from_dims
        to_dims = self._rebuild_to_dims
        mode = self._embed_mode
        logger.info(
            "vector_store.auto_rebuild_start from={} to={} mode={}",
            from_dims, to_dims, mode,
        )
        try:
            # 选择重建脚本：local 用本地模型，remote 用远程 API
            script_name = "rebuild_vec_local.py" if mode == "local" else "rebuild_vec_remote.py"
            script_path = str(Path(__file__).resolve().parent.parent / "scripts" / script_name)
            if not Path(script_path).exists():
                logger.error("vector_store.auto_rebuild_script_missing path={}", script_path)
                return

            # 关闭当前向量库连接（脚本要替换文件）
            await self._close_vec_conn_for_rebuild()

            # 调用重建脚本（子进程）
            import subprocess
            cmd = [sys.executable, script_path]
            # remote 模式传入 API 配置
            if mode == "remote":
                api_key = self._embed_api_key or os.getenv("SILICONFLOW_API_KEY", "")
                base_url = self._embed_base_url or "https://api.siliconflow.cn/v1"
                model = self._embed_model or "BAAI/bge-m3"
                cmd.extend(["--model", model, "--base-url", base_url, "--api-key", api_key])

            logger.info("vector_store.auto_rebuild_running cmd={}", " ".join(cmd[:2]))
            # 重建是分钟级可等重活：占住 hot 池会挤压检索链路的 worker，
            # 走 heavy 池（期间在线检索服务不受影响）
            result = await to_thread_heavy(
                subprocess.run, cmd,
                capture_output=True, text=True, timeout=3600,
            )
            if result.returncode != 0:
                logger.error(
                    "vector_store.auto_rebuild_failed rc={} stderr={}",
                    result.returncode, (result.stderr or "")[:500],
                )
                return
            logger.info(
                "vector_store.auto_rebuild_done stdout_tail={}",
                (result.stdout or "")[-300:],
            )

            # 重建成功：更新维度并重新初始化
            self._dimensions = to_dims
            self._needs_rebuild = False
            self._rebuild_from_dims = 0
            self._rebuild_to_dims = 0
            # 清理 numpy 暴力索引缓存（维度已变，旧索引无效）
            if self._brute is not None:
                try:
                    self._brute.close()
                except Exception as exc:  # noqa: BLE001 —— 清理失败仅记录，索引下次重建
                    logger.debug("vector_store.brute_close_failed: {}", str(exc)[:120])
                self._brute = None
            brute_dir = Path(self._db_path).parent / (Path(self._db_path).stem + "_brute")
            if brute_dir.exists():
                import shutil
                shutil.rmtree(brute_dir, ignore_errors=True)
            # 重新初始化向量库连接（新维度）
            self._closed = False
            self._vec_conn, is_fat = await to_thread_hot(self._init_db_sync)
            # 重新加载 numpy 暴力索引
            if self._brute_enabled:
                from memory.numpy_index import NumpyBruteIndex
                base_dir = Path(self._db_path).parent / (Path(self._db_path).stem + "_brute")
                self._brute_base_dir = str(base_dir)
                self._brute = NumpyBruteIndex(dim=self._dimensions, base_dir=base_dir)
                try:
                    await to_thread_hot(self._load_brute_sync)
                    if self._brute.ready:
                        logger.info("vector_store.brute_ready_after_rebuild")
                    else:
                        self._brute = None
                except Exception as e:  # noqa: BLE001
                    logger.warning("vector_store.brute_init_after_rebuild_failed error={}", str(e))
                    self._brute = None
            logger.info(
                "vector_store.auto_rebuild_complete new_dims={} mode={}",
                self._dimensions, mode,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("vector_store.auto_rebuild_error error={}", str(e))
        finally:
            self._rebuild_in_progress = False

    async def _close_vec_conn_for_rebuild(self) -> None:
        """重建前关闭向量库连接（释放文件锁，让脚本能替换文件）。"""
        with self._lock:
            if self._vec_conn:
                try:
                    self._vec_conn.close()
                except Exception:  # noqa: BLE001
                    logger.debug("vector_store.rebuild_close_conn_failed", exc_info=True)
                self._vec_conn = None
            self._initialized = False

    def _init_db_sync(self) -> tuple[Any, bool]:
        """在后台线程中初始化 SQLite 数据库，加载 sqlite_vec 扩展并创建向量虚拟表。"""
        import sqlite3

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
                    # WAL checkpoint 阈值统一 2000 页（8MB）：U 盘上大阈值
                    # 造成周期性几十 MB 回写尖峰，撞上用户请求即整批卡顿。
                    # （2026-08-27 复盘：agent.db WAL 因 10000 阈值 + 批量剪枝
                    # 一度积到 2.3GB 且被常驻读连接挡住无法 TRUNCATE。）
                    conn.execute("PRAGMA wal_autocheckpoint=2000")

                # 维度策略：
                # - 显式配置（dimensions > 0）时直接使用
                # - 本地推理模式：用本地模型输出维度（BGE-small-zh-v1.5 = 512）
                # - 未配置时查表已有维度；表不存在则用 1024 兜底
                # 修复 P0：原代码硬编码 1024 且首次 INSERT 时 _dimensions 竞态写入，
                # 维度不匹配时 INSERT 永久失败。
                if self._dimensions > 0:
                    dims = self._dimensions
                elif self._embed_mode == "local" and self._local_provider is not None:
                    # 懒加载时此处同步加载（首次使用），拿到真实维度
                    self._local_provider.load()
                    dims = self._local_provider.dimensions or 512
                    self._dimensions = dims
                else:
                    try:
                        row = conn.execute(
                            "SELECT embedding FROM memories_vec LIMIT 1"
                        ).fetchone()
                        if row is not None and row[0] is not None:
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

                # 维度不匹配检测（local/remote 均适用）：
                # vec0 虚拟表结构无法原地改维度，INSERT 会静默失败。检测到不匹配时
                # 不再直接 raise（旧实现要求手动跑脚本），而是记录重建标记，
                # 由 init() 异步触发自动重建（调用 scripts/rebuild_vec_*.py）。
                if not self._dimensions_explicit:
                    try:
                        row = conn.execute(
                            "SELECT embedding FROM memories_vec LIMIT 1"
                        ).fetchone()
                        if row is not None and row[0] is not None:
                            raw = row[0]
                            if isinstance(raw, (bytes, bytearray)):
                                existing_dims = len(raw) // 4
                            else:
                                existing_dims = dims
                            if existing_dims != dims and existing_dims > 0:
                                # 记录重建标记：init() 检测到后异步触发自动重建
                                self._needs_rebuild = True
                                self._rebuild_from_dims = existing_dims
                                self._rebuild_to_dims = dims
                                logger.warning(
                                    "vector_store.dimension_mismatch_needs_rebuild "
                                    "existing={} target={} mode={} "
                                    "hint=auto rebuild will be triggered",
                                    existing_dims, dims, self._embed_mode,
                                )
                    except sqlite3.OperationalError:
                        logger.debug("vector_store.memories_vec_table_missing_create", exc_info=True)  # 表不存在（首次初始化），正常创建

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
            except (ImportError, OSError, RuntimeError):
                # 修复资源泄漏：sqlite_vec.load 失败时必须 close 连接
                try:
                    conn.close()
                except (OSError, RuntimeError):
                    logger.warning("vector_store.conn_close_failed_during_load", exc_info=True)
                except Exception:
                    logger.exception("vector_store.conn_close_unexpected_during_load")
                raise
            except Exception:
                logger.exception("vector_store.vec_init_unexpected")
                try:
                    conn.close()
                except (OSError, RuntimeError):
                    logger.warning("vector_store.conn_close_failed_during_load", exc_info=True)
                except Exception:
                    logger.exception("vector_store.conn_close_unexpected_during_load")
                raise



    def _load_brute_sync(self) -> None:
            with self._lock:
                if self._closed:
                    return
                self._brute.load_from_db(self._vec_conn)


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

        await to_thread_hot(_do_close)
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

    async def embed(self, texts: list[str] | str) -> list[list[float]] | list[float]:
        legacy_single = isinstance(texts, str)
        batch = [texts] if legacy_single else texts
        if not batch:
            return []
        if len(batch) == 1:
            vector = await self._embed_one(batch[0])
            return vector if legacy_single else [vector]
        if self._embed_mode == "local":
            vectors = await self._embed_batch_cached(batch)
        else:
            vectors = await asyncio.gather(*(self._embed_one(text) for text in batch))
        for vector in vectors:
            self._validate_dimension(vector)
        return vectors

    async def _embed_batch_cached(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入（local 模式）：先逐条查 EmbedCache，仅 miss 子集送本地推理。

        原实现整批直通 _do_embed_batch 绕过缓存——重建索引/多查询改写场景下
        重复文本也全量打 NPU/CPU 推理（单条秒级，批量线性放大）。改为与
        _embed_one 同款的"缓存优先 + provider 只算 miss"，selection_key 漂移时
        先清缓存再取值，保证维度切换后不会命中旧维度向量。
        """
        if not texts:
            return []
        selection_key = await self._current_selection_key()
        if (
            self._embedding_selection_key is not None
            and selection_key != self._embedding_selection_key
        ):
            self._cache.clear()
        if selection_key is not None:
            self._embedding_selection_key = selection_key

        results: list[list[float] | None] = [None] * len(texts)
        miss_idx: list[int] = []
        for i, text in enumerate(texts):
            cached = self._cache.get(text)
            if cached:
                results[i] = cached
            else:
                miss_idx.append(i)

        if miss_idx:
            miss_texts = [texts[i] for i in miss_idx]
            fresh = await self._do_embed_batch(miss_texts)
            for i, vec in zip(miss_idx, fresh):
                results[i] = vec
                if vec and await self._current_selection_key() == selection_key:
                    self._cache.put(texts[i], vec)
        # _do_embed_batch 已校验返回条数；此处兜底避免 None 下漏
        return [v or [] for v in results]

    async def _do_embed_batch(self, texts: list[str]) -> list[list[float]]:
        if self._local_provider is None:
            raise LocalEmbeddingUnavailableError("no running local embedding instance")
        _t0 = time.perf_counter()
        vectors = await self._local_provider.embed(texts)
        # 直方图打点（timer.embed_provider）：区分 NPU/CPU 推理退化、批量均摊观测
        metrics.observe(
            "embed_provider",
            (time.perf_counter() - _t0) / max(len(texts), 1),
        )
        if len(vectors) != len(texts):
            raise RuntimeError(
                f"local embedding returned {len(vectors)} vectors for {len(texts)} texts"
            )
        return vectors

    async def _embed_one(self, text: str) -> list[float]:
        """生成单条文本的嵌入向量，优先使用缓存，失败时自动重试。

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

        selection_key = None
        if self._embed_mode == "local" and self._local_provider is not None:
            selector = getattr(self._local_provider, "selection_key", None)
            if selector is not None:
                selection_key = await selector()
                if (
                    self._embedding_selection_key is not None
                    and selection_key != self._embedding_selection_key
                ):
                    self._cache.clear()
                self._embedding_selection_key = selection_key
        cached = self._cache.get(text)
        if cached:
            return cached

        # 单飞：已有同文本在途请求则直接共享结果，不重复打 API
        key = (selection_key, text)
        inflight = self._inflight.get(key)
        if inflight is not None:
            try:
                return await asyncio.shield(inflight)
            except LocalEmbeddingUnavailableError:
                if self._selected_local_service:
                    raise
                return []
            except RuntimeValidationError:
                raise
            except (OSError, RuntimeError, ValueError):
                return []
            except Exception:
                logger.exception("vector_store.embed_inflight_unexpected")
                return []

        future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            vec = await self._do_embed(text)
            if vec:
                if await self._current_selection_key() == selection_key:
                    self._cache.put(text, vec)
            future.set_result(vec)
            return vec
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise
        except Exception as e:
            if isinstance(e, RuntimeValidationError):
                if not future.done():
                    future.set_exception(e)
                    future.exception()
                raise
            if isinstance(e, LocalEmbeddingUnavailableError) and self._selected_local_service:
                if not future.done():
                    future.set_exception(e)
                    future.exception()
                raise
            if not future.done():
                future.set_result([])
            logger.warning("vector_store.embed_singleflight_failed", error=str(e))
            return []
        finally:
            if self._inflight.get(key) is future:
                self._inflight.pop(key, None)

    async def _current_selection_key(self) -> Any | None:
        if self._embed_mode == "local" and self._local_provider is not None:
            selector = getattr(self._local_provider, "selection_key", None)
            if selector is not None:
                return await selector()
        return None

    async def _do_embed(self, text: str) -> list[float]:
        """实际生成嵌入向量（本地推理或远程 API，含重试）。"""
        # 本地推理（香橙派 onnxruntime CPU）：CPU 密集，走 to_thread 不阻塞事件循环；
        # 无网络依赖、无重试必要，失败即返回空（调用方均有兜底）。
        if self._embed_mode == "local":
            if self._local_provider is None:
                return []
            vectors = await self._local_provider.embed([text])
            vec = vectors[0] if vectors else []
            self._validate_dimension(vec)
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

    def _validate_dimension(self, vec: list[float]) -> None:
        if vec and self._dimensions and len(vec) != self._dimensions:
            raise RuntimeValidationError(
                f"embedding dimension {len(vec)} does not match expected {self._dimensions}"
            )

    async def warm_cache(self, texts: list[str]) -> None:
        """预热嵌入缓存：对未缓存文本调用 embed 填充缓存，单条失败不影响整体。"""
        if not self._embed_client or not texts:
            return
        for text in texts:
            if not text or text in self._cache:
                continue
            try:
                await self.embed([text])
            except Exception as e:
                logger.warning("vector_store.warm_cache_item_failed", error=str(e))

    async def upsert(self, row_id: int, text: str) -> bool:
        """写入或更新指定 rowid 的向量记录（先删后插）。"""
        if not self._initialized or not self._vec_conn:
            return False

        vectors = await self.embed([text])
        vec = vectors[0] if vectors else []
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
                        logger.debug("vector_store upsert 删除旧记录失败(rowid={}): {}", row_id, e)
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

        return await to_thread_hot(_do_upsert)

    async def batch_upsert_children(self, items: list[tuple[int, str]]) -> bool:
        """批量子chunk向量写入。items = [(child_id, text), ...]"""
        if not self._initialized or not self._vec_conn or not items:
            return False
        try:
            vectors = await self.embed([text for _, text in items])
            if len(vectors) != len(items):
                raise RuntimeError(
                    f"embedding returned {len(vectors)} vectors for {len(items)} child chunks"
                )
            valid = []
            for (child_id, _), vector in zip(items, vectors, strict=True):
                if not isinstance(vector, list) or not vector:
                    raise RuntimeError(f"embedding for child {child_id} is empty")
                self._validate_dimension(vector)
                valid.append((child_id, vector))
        except Exception as error:
            logger.warning("vector.batch_embed_children_failed", error=str(error)[:200])
            return False

        def _do_batch() -> bool:
            """在后台线程中批量子chunk向量写入。"""
            with self._lock:
                if self._closed:
                    return False
                try:
                    self._vec_conn.execute("BEGIN TRANSACTION")
                    for cid, vec in valid:
                        vec_json = json.dumps(vec)
                        self._vec_conn.execute(
                            "INSERT OR REPLACE INTO memories_child_vec (rowid, embedding) VALUES (?, vec_f32(?))",
                            (cid, vec_json),
                        )
                    self._vec_conn.commit()
                    if self._brute is not None:
                        brute_ok = all(
                            self._brute.upsert("memories_child_vec", cid, vec)
                            for cid, vec in valid
                        )
                        if not brute_ok:
                            self._brute.load_from_db(self._vec_conn)
                    return True
                except Exception as e:
                    try:
                        self._vec_conn.execute("ROLLBACK")
                    except Exception as re:
                        logger.debug("vector_store.batch_upsert_children_rollback_error", error=str(re))
                    logger.warning("vector_store.batch_upsert_children_failed", error=str(e))
                    return False

        return await to_thread_hot(_do_batch)

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
            return await to_thread_hot(_do_delete)
        except Exception as e:
            logger.warning("vector_store.delete_failed", row_id=row_id, error=str(e))
            return False

    def _search_candidates_exact(
        self,
        table: str,
        query_vec: list[float],
        candidate_ids: list[int],
        top_k: int,
    ) -> list[tuple[int, float]]:
        """Exact bounded search over candidate rowids when vec0 cannot filter KNN.

        距离计算分两条路径：
        - numpy 可用：批量 frombuffer + 广播减法，C/BLAS 层完成（行数多时
          相比逐行 struct.unpack + 生成器 sum 快约一个数量级）；
        - numpy 不可用：逐行 struct.unpack 纯 Python 兜底。
        两者结果逐位一致（同一 L2 公式与求和顺序无关紧要，浮点差异
        在 tie-breaking 排序前不可观测）。
        """
        if not candidate_ids or top_k <= 0:
            return []
        candidate_list = sorted({int(value) for value in candidate_ids})
        row_ids: list[int] = []
        raw_blobs: list[bytes] = []
        for offset in range(0, len(candidate_list), 900):
            batch = candidate_list[offset:offset + 900]
            placeholders = ",".join("?" for _ in batch)
            rows = self._vec_conn.execute(
                f"SELECT rowid, embedding FROM {table} "
                f"WHERE rowid IN ({placeholders})",
                batch,
            ).fetchall()
            for row_id, raw in rows:
                if not isinstance(raw, (bytes, bytearray, memoryview)):
                    continue
                row_ids.append(int(row_id))
                raw_blobs.append(bytes(raw))

        if _HAS_NUMPY and raw_blobs:
            dim = len(query_vec)
            matrix = np.frombuffer(b"".join(raw_blobs), dtype="<f4")
            if matrix.size == len(raw_blobs) * dim:
                # 所有行维度一致（正常情况）：批量广播减法 + 行内点积
                diff = matrix.reshape(len(raw_blobs), dim).astype(np.float32) \
                    - np.asarray(query_vec, dtype=np.float32)
                dists = np.sqrt(np.einsum("ij,ij->i", diff, diff))
                ranked = list(zip(row_ids, dists.tolist(), strict=True))
            else:
                # 维度不一致行（理论不出现）：逐行兜底剔除
                ranked = self._exact_fallback(row_ids, raw_blobs, query_vec)
        else:
            ranked = self._exact_fallback(row_ids, raw_blobs, query_vec)
        ranked.sort(key=lambda item: (item[1], item[0]))
        return ranked[:top_k]

    @staticmethod
    def _exact_fallback(
        row_ids: list[int], raw_blobs: list[bytes], query_vec: list[float]
    ) -> list[tuple[int, float]]:
        """纯 Python L2 兜底（numpy 不可用/维度异常时）。"""
        ranked: list[tuple[int, float]] = []
        for row_id, raw in zip(row_ids, raw_blobs, strict=True):
            values = struct.unpack(f"<{len(raw) // 4}f", raw)
            if len(values) != len(query_vec):
                continue
            distance = math.sqrt(sum(
                (float(value) - float(query_value)) ** 2
                for value, query_value in zip(values, query_vec, strict=True)
            ))
            ranked.append((row_id, distance))
        return ranked

    async def search(self, query_text: str, top_k: int = 5,
                     candidate_ids: list[int] | None = None,
                     deterministic: bool = True,
                     query_vec: list[float] | None = None) -> list[tuple[int, float]]:
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
            query_vec: 预计算查询向量（P1-4：多查询场景由调用方批量 embed 后传入，
                避免每个子查询各自调用一次 embed API）；None 时内部 embed
        """
        if not self._initialized or not self._vec_conn:
            return []

        if query_vec is not None:
            vec = query_vec
        else:
            vectors = await self.embed([query_text])
            vec = vectors[0] if vectors else []
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
                    return self._search_candidates_exact(
                        "memories_vec", vec, candidate_ids, top_k
                    )
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
            return await to_thread_hot(_do_search)
        except Exception as e:
            logger.warning("vector_store.search_failed", error=str(e))
            return []

    async def search_child(
        self,
        query_vec: list[float],
        top_k: int = 20,
        candidate_ids: list[int] | None = None,
    ) -> list[dict]:
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
                        "memories_child_vec", query_vec, top_k,
                        candidate_ids=candidate_ids,
                        ef=max(top_k * 2, len(candidate_ids or [])),
                    )
                    if brute_res is not None:
                        return [{"id": r[0], "distance": r[1]} for r in brute_res]
                if candidate_ids is not None:
                    exact = self._search_candidates_exact(
                        "memories_child_vec", query_vec, candidate_ids, top_k
                    )
                    return [{"id": row_id, "distance": distance}
                            for row_id, distance in exact]
                rows = self._vec_conn.execute(
                    "SELECT rowid, distance FROM memories_child_vec "
                    "WHERE embedding MATCH vec_f32(?) AND k=? "
                    "ORDER BY distance",
                    (vec_json, top_k),
                ).fetchall()
                return [{"id": r[0], "distance": r[1]} for r in rows]

        try:
            return await to_thread_hot(_do_search)
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
            query_vectors = await self.embed([query])
            query_vec = query_vectors[0] if query_vectors else []
            if not query_vec:
                tuples = await self.search(query, top_k=k, candidate_ids=cand_int)
                return [{"rowid": r, "distance": d} for r, d in tuples]

            # 2. 获取 HyDE 文档向量
            hyde_vectors = await self.embed([hyde_doc])
            hyde_vec = hyde_vectors[0] if hyde_vectors else []
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
                        results = self._search_candidates_exact(
                            "memories_vec", mixed, cand_int, k
                        )
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

            return await to_thread_hot(_do_hyde_search)
        except Exception as e:
            logger.warning("vector_store.search_with_hyde_failed", error=str(e))
            tuples = await self.search(query, top_k=k, candidate_ids=cand_int)
            return [{"rowid": r, "distance": d} for r, d in tuples]

    async def upsert_kg_entity(self, row_id: int, text: str) -> bool:
        """写入或更新 KG 实体向量（先删后插）。"""
        if not self._initialized or not self._vec_conn:
            return False
        vectors = await self.embed([text])
        vec = vectors[0] if vectors else []
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

        return await to_thread_hot(_do_upsert)

    async def upsert_kg_relation(self, row_id: int, text: str) -> bool:
        """写入或更新 KG 关系向量（先删后插）。"""
        if not self._initialized or not self._vec_conn:
            return False
        vectors = await self.embed([text])
        vec = vectors[0] if vectors else []
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

        return await to_thread_hot(_do_upsert)

    async def search_kg_entities(
        self,
        query_text: str,
        top_k: int = 5,
        candidate_ids: list[int] | None = None,
    ) -> list[tuple[int, float]]:
        """搜索 KG 实体向量，返回 [(rowid, distance), ...]。"""
        if not self._initialized or not self._vec_conn:
            return []
        vectors = await self.embed([query_text])
        vec = vectors[0] if vectors else []
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
                        "kg_entities_vec", vec, top_k,
                        candidate_ids=candidate_ids,
                        ef=max(top_k * 2, len(candidate_ids or [])),
                    )
                    if brute_res is not None:
                        return brute_res
                if candidate_ids is not None:
                    return self._search_candidates_exact(
                        "kg_entities_vec", vec, candidate_ids, top_k
                    )
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
            return await to_thread_hot(_do_search)
        except Exception as e:
            logger.warning("vector_store.search_kg_entities_failed", error=str(e))
            return []

    async def search_kg_relations(
        self,
        query_text: str,
        top_k: int = 5,
        candidate_ids: list[int] | None = None,
    ) -> list[tuple[int, float]]:
        """搜索 KG 关系向量，可限定允许的 relation rowid。"""
        if not self._initialized or not self._vec_conn:
            return []
        vectors = await self.embed([query_text])
        vec = vectors[0] if vectors else []
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
                        "kg_relations_vec", vec, top_k,
                        candidate_ids=candidate_ids,
                        ef=max(top_k * 2, len(candidate_ids or [])),
                    )
                    if brute_res is not None:
                        return brute_res
                if candidate_ids is not None:
                    if not candidate_ids:
                        return []
                    candidate_set = set(candidate_ids)
                    count_row = self._vec_conn.execute(
                        "SELECT COUNT(*) FROM kg_relations_vec"
                    ).fetchone()
                    fetch_count = max(int(count_row[0] if count_row else 0), top_k)
                else:
                    candidate_set = None
                    fetch_count = fetch_k
                rows = self._vec_conn.execute(
                    "SELECT rowid, distance FROM kg_relations_vec "
                    "WHERE embedding MATCH vec_f32(?) AND k=? "
                    "ORDER BY distance",
                    [vec_json, fetch_count],
                ).fetchall()
                results = [
                    (row[0], row[1]) for row in rows
                    if candidate_set is None or row[0] in candidate_set
                ]
                results.sort(key=lambda r: (r[1], r[0]))
                return results[:top_k]

        try:
            return await to_thread_hot(_do_search)
        except Exception as e:
            logger.warning("vector_store.search_kg_relations_failed", error=str(e))
            return []
