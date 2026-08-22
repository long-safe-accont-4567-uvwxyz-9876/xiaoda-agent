"""NPU Embedding Provider — VIP9000 NPU 推理 BGE-small-zh-v1.5。

以常驻子进程方式驱动 scripts/npu/bge_npu_runner（--serve 模式）：
- 分词：tokenizers.Tokenizer（与 local_embed 同一 tokenizer.json，CPU 侧）
- 推理：NBG 固定 seq=512 输入，逐条 padding 到 512，经 stdin 送入 runner
- 输出：stdout 协议流（magic "BGEVEC01" 后为 N×512×float32），L2 归一化已在 C 侧完成

与 LocalEmbeddingProvider 接口对齐：ready / dimensions / load() / embed() /
encode_batch() / close()，vector_store 可无缝切换。
"""

from __future__ import annotations

import os
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from tokenizers import Tokenizer

    HAS_NPU_EMBED_DEPS = True
except ImportError:  # pragma: no cover
    Tokenizer = None  # type: ignore
    HAS_NPU_EMBED_DEPS = False

from local_ai.devices.vip_probe import probe_vip_backend  # noqa: E402
from memory.local_embed import LocalEmbeddingProvider  # noqa: E402

# runner 流协议常量（与 bge_npu_runner.c --serve 一致）
MAGIC = b"BGEVEC01"
SEQ = 512
# 模型维度和输入数由 NPU 型号决定（bge-small-zh: 512 维 3 输入；
# bge-large-zh w8a16: 1024 维 2 输入）。支持 env 覆盖便于切换模型。
HID = int(os.getenv("NPU_HID", "1024"))
N_IN = int(os.getenv("NPU_N_IN", "2"))
VEC_BYTES = HID * 4          # hidden × float32
INPUT_BYTES = N_IN * SEQ * 4  # input_ids/attention_mask（+token_type_ids 视模型而定）
# 单批最大条数：NPU 流串行（_io_lock），大批（32 条 3.7s）占流会让检索
# embed 排队撞 8s 检索超时线；8 条/批 ≈ 0.9s，检索插队亚秒级
_MAX_NPU_BATCH = 8


def _default_runner() -> str:
    """runner 可执行文件默认路径（相对 ai-agent 根目录）。"""
    root = Path(__file__).resolve().parent.parent
    return str(root / "scripts" / "npu" / "bge_npu_runner")


def _default_nbg() -> str:
    """NBG 默认路径（bge-large-zh w8a16 1024 维固化版；bge-small 512 维旧版
    仍可用 NPU_NBG env 指回 bge_small_zh.nb）。

    路径动态解析（规则：本地模型路径不硬编码挂载点）：
    1. NPU_NBG env 显式指定
    2. KIOXIA_DATA_DIR（外置盘数据目录，如 /mnt/usb2/nahida-data）下
       npu/bge_npu_kit/... 动态拼接
    3. 兜底：项目根相对路径（可能不存在，调用方降级 CPU）
    """
    nbg_env = os.getenv("NPU_NBG", "").strip()
    if nbg_env:
        return nbg_env
    data_dir = os.getenv("KIOXIA_DATA_DIR", "").strip()
    if data_dir:
        return str(
            Path(data_dir) / "npu" / "bge_npu_kit" / "npu_input"
            / "bge_large_zh_sigmoid_pcq.w8a16" / "network_binary.nb"
        )
    root = Path(__file__).resolve().parent.parent
    return str(
        root / "npu" / "bge_npu_kit" / "npu_input"
        / "bge_large_zh_sigmoid_pcq.w8a16" / "network_binary.nb"
    )


def probe_npu(runner_path: str = "", timeout_s: float = 15.0) -> bool:
    """探测本机 VIP 后端是否可用。

    通过 runner 的 --probe 模式验证（vip_init 成功 = NPU 设备/驱动可用）。
    runner 文件不存在（如纯 CPU 机器 / Windows 打包版）直接返回 False。
    成功退出码 0 → True；失败/超时/异常 → False。型号与算力规格不作推断。
    """
    path = Path(runner_path or _default_runner())
    device = probe_vip_backend(str(path), timeout_s=timeout_s)
    ok = device is not None
    logger.info("npu_probe.result ok={} runner={}", ok, str(path))
    return ok


class NpuEmbeddingProvider:
    """基于 VIP9000 NPU 的本地 BGE 小模型 Embedding（VIPLite 常驻子进程）。

    懒启动：首次 embed 才拉起 runner 子进程（初始化约 50ms）。
    线程安全：流式推理串行化（锁），调用方应经 asyncio.to_thread 执行。
    """

    def __init__(self, model_dir: str | Path, *,
                 query_prefix: str = "", max_length: int = 512,
                 runner_path: str = "", nbg_path: str = "",
                 timeout_s: float = 15.0) -> None:
        self._model_dir = Path(model_dir)
        self._query_prefix = query_prefix
        self._max_length = max_length
        self._runner_path = runner_path or _default_runner()
        self._nbg_path = nbg_path or _default_nbg()
        self._timeout_s = timeout_s
        self._tokenizer: Any = None
        self._proc: subprocess.Popen | None = None
        self._stderr_f: Any = None
        self._dimensions: int = HID
        self._loaded = False
        self._load_error = ""
        self._load_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._pending = b""
        self._busy = False
        self._last_ms = 0.0
        self._calls = 0

    # ── 加载 ──────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._loaded

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def busy(self) -> bool:
        """当前是否有推理任务在占用 NPU 流。"""
        return self._busy

    @property
    def last_call_ms(self) -> float:
        """最近一次 NPU 推理耗时（毫秒，0 = 尚无调用）。"""
        return self._last_ms

    @property
    def call_count(self) -> int:
        """NPU 累计推理次数。"""
        return self._calls

    def load(self) -> bool:
        if self._loaded:
            return True
        with self._load_lock:
            if self._loaded:
                return True
            try:
                if not HAS_NPU_EMBED_DEPS:
                    raise RuntimeError("tokenizers not installed")
                tok_path = self._model_dir / "tokenizer.json"
                if not tok_path.exists():
                    raise FileNotFoundError(f"tokenizer.json not found in {self._model_dir}")
                if not Path(self._runner_path).exists():
                    raise FileNotFoundError(f"runner not found: {self._runner_path}")
                if not Path(self._nbg_path).exists():
                    raise FileNotFoundError(f"nbg not found: {self._nbg_path}")
                self._tokenizer = Tokenizer.from_file(str(tok_path))
                self._start_proc()
                self._loaded = True
                logger.info("npu_embed.ready runner={} nbg={} dims={}",
                            self._runner_path, self._nbg_path, self._dimensions)
                return True
            except Exception as e:  # noqa: BLE001
                self._load_error = str(e)
                logger.warning("npu_embed.load_failed error={}", str(e))
                return False

    def _start_proc(self) -> None:
        """拉起 runner 子进程并等待协议 magic（丢弃库横幅）。"""
        # sudo -n：NPU 设备需 root 权限（已配置 NOPASSWD）
        cmd = ["sudo", "-n", self._runner_path, self._nbg_path,
               "--serve", "--seq", str(SEQ), "--hidden", str(self._dimensions),
               "--quiet"]
        log_path = Path(__file__).resolve().parent.parent / "logs" / "npu_embed_runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_f = open(log_path, "ab")
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_f,
            )
        except OSError:
            self._stderr_f.close()
            self._stderr_f = None
            raise
        # 逐块读 stdout 直到 magic，之前内容（VIPLite 版本横幅）全部丢弃；
        # 加超时兜底：runner 初始化挂起（如 NPU 设备忙/驱动异常）时不阻塞服务
        buf = b""
        deadline = time.monotonic() + self._timeout_s
        try:
            while True:
                chunk = self._proc.stdout.read1(4096)  # type: ignore[union-attr]
                if not chunk:
                    raise RuntimeError("runner exited before magic")
                buf += chunk
                idx = buf.find(MAGIC)
                if idx >= 0:
                    self._pending = buf[idx + len(MAGIC):]
                    return
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"runner magic timeout after {self._timeout_s:.0f}s")
        except (TimeoutError, RuntimeError, OSError) as e:
            # 清理泄漏的子进程和管道，避免 magic 超时后僵尸进程/stderr 句柄残留
            try:
                if self._proc and self._proc.poll() is None:
                    self._proc.kill()
                    self._proc.wait(timeout=5)
            except (ImportError, OSError, RuntimeError, ValueError):
                logger.debug("npu_embed.proc_kill_cleanup_failed")
            except Exception:
                logger.exception("npu_embed.proc_kill_unexpected")
            try:
                if self._stderr_f and not self._stderr_f.closed:
                    self._stderr_f.close()
            except (ImportError, OSError, RuntimeError, ValueError):
                logger.debug("npu_embed.stderr_close_cleanup_failed")
            except Exception:
                logger.exception("npu_embed.stderr_close_unexpected")
            raise
        # (超时抛出后由 load() 捕获，Adaptive 层探测已先排除大部分无 NPU 场景)

    # ── 推理 ──────────────────────────────────────────────

    def _apply_prefix(self, text: str) -> str:
        return f"{self._query_prefix}{text}" if self._query_prefix else text

    def _tokenize(self, texts: list[str]) -> bytes:
        """批量分词并 padding 到固定 SEQ，打包为 runner 输入字节流。

        N_IN=2（bge-large）：input_ids + attention_mask；
        N_IN=3（bge-small）：额外 token_type_ids。
        """
        encodings = self._tokenizer.encode_batch(
            [self._apply_prefix(t) for t in texts],
            add_special_tokens=True,
        )
        flat: list[int] = []
        for enc in encodings:
            ids = enc.ids[:SEQ]
            pad = SEQ - len(ids)
            flat += ids + [0] * pad                      # input_ids
            flat += [1] * len(ids) + [0] * pad           # attention_mask
            if N_IN >= 3:
                flat += list(enc.type_ids[:SEQ]) + [0] * pad  # token_type_ids
        return struct.pack(f"<{len(flat)}i", *flat)

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（同步，阻塞等待 NPU，调用方应经 to_thread 执行）。

        内部按 _MAX_NPU_BATCH（默认 8）拆小批：NPU 流（_io_lock）串行，
        大批（32 条实测 3.7s）长时间占流会让检索路径的 embed 排队撞
        8s 检索超时线。拆小批后单批 ≤1s，检索 embed 插队等待降到亚秒级
        （v0.5.62：与 CPU 拆批同策略，全 NPU 模式下后台编码不拖垮检索）。
        """
        if not texts:
            return []
        if not self._loaded and not self.load():
            return []
        out: list[list[float]] = []
        for i in range(0, len(texts), _MAX_NPU_BATCH):
            chunk = texts[i:i + _MAX_NPU_BATCH]
            try:
                payload = self._tokenize(chunk)
            except Exception as e:  # noqa: BLE001
                logger.warning("npu_embed.tokenize_failed error={}", str(e))
                continue
            # 重试一次：子进程异常退出时重启
            for attempt in (0, 1):
                try:
                    vecs = self._infer(payload, len(chunk))
                    out.extend(vecs)
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning("npu_embed.infer_failed attempt={} error={}",
                                   attempt, str(e))
                    self._restart()
            else:
                return []
        return out

    def _infer(self, payload: bytes, n: int) -> list[list[float]]:
        with self._io_lock:
            assert self._proc and self._proc.stdin and self._proc.stdout
            _t0 = time.monotonic()
            self._proc.stdin.write(payload)
            self._proc.stdin.flush()
            need = n * VEC_BYTES
            data = self._pending
            while len(data) < need:
                chunk = self._proc.stdout.read(need - len(data))
                if not chunk:
                    raise RuntimeError("runner stdout closed")
                data += chunk
            self._pending = data[need:]
            # 更新 NPU 实时统计（npu_stats / 算力设备检测页展示）
            self._last_ms = (time.monotonic() - _t0) * 1000
            self._calls += 1
        out: list[list[float]] = []
        for i in range(n):
            out.append(list(struct.unpack(f"<{HID}f", data[i * VEC_BYTES:(i + 1) * VEC_BYTES])))
        return out

    def _restart(self) -> None:
        self._loaded = False
        try:
            if self._proc:
                self._proc.kill()
                self._proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            logger.warning("npu_embed.restart_kill_failed", exc_info=True)
        try:
            if self._stderr_f:
                self._stderr_f.close()
        except Exception:  # noqa: BLE001
            logger.warning("npu_embed.restart_stderr_close_failed", exc_info=True)
        self._proc = None
        self._pending = b""
        self.load()

    def embed(self, text: str) -> list[float]:
        batch = self.encode_batch([text])
        return batch[0] if batch else []

    def close(self) -> None:
        try:
            if self._proc:
                self._proc.terminate()
                self._proc.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                if self._proc:
                    self._proc.kill()
            except Exception:  # noqa: BLE001
                logger.warning("npu_embed.close_kill_failed", exc_info=True)
        try:
            if self._stderr_f:
                self._stderr_f.close()
        except Exception:  # noqa: BLE001
            logger.warning("npu_embed.close_stderr_close_failed", exc_info=True)
        self._proc = None
        self._stderr_f = None
        self._loaded = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def __del__(self):
        if self._proc is not None or self._stderr_f is not None:
            try:
                self.close()
            except Exception as exc:  # 析构路径清理失败仅记录
                logger.debug("npu_embed.del_close_failed: {}", str(exc)[:120])


class AdaptiveEmbeddingProvider:
    """长短文本自适应 Embedding：短文本走 CPU（动态长度快），长文本走 NPU。

    理由：BGE NBG 固定 seq=512，短文本 padding 到 512 在 NPU 上反而比 CPU
    慢（实测短文本 CPU 16.6ms vs NPU 109ms，长文本 CPU 553.9ms vs NPU 99.6ms）。
    以词元数阈值（默认 256）路由；NPU 不可用时自动降级为纯 CPU。

    接口与 LocalEmbeddingProvider / NpuEmbeddingProvider 对齐。
    """

    def __init__(self, model_dir: str | Path, *,
                 query_prefix: str = "", max_length: int = 512,
                 threshold: int = 0, runner_path: str = "", nbg_path: str = "") -> None:
        self._threshold = int(threshold or os.getenv("LOCAL_EMBED_THRESHOLD", "0"))
        self._cpu = LocalEmbeddingProvider(model_dir, query_prefix=query_prefix,
                                           max_length=max_length)
        self._npu = NpuEmbeddingProvider(model_dir, query_prefix=query_prefix,
                                         max_length=max_length,
                                         runner_path=runner_path, nbg_path=nbg_path)
        self._tokenizer: Any = None
        self._dimensions = 0
        self._loaded = False
        self._load_error = ""
        self._load_lock = threading.Lock()

    # ── 加载 ──────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._loaded

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def load(self) -> bool:
        if self._loaded:
            return True
        with self._load_lock:
            if self._loaded:
                return True
            try:
                tok_path = Path(self._cpu._model_dir) / "tokenizer.json"
                if not tok_path.exists():
                    raise FileNotFoundError(f"tokenizer.json not found: {tok_path}")
                self._tokenizer = Tokenizer.from_file(str(tok_path))
                # NPU 优先（本地部署主力，秒级启动）：NPU 可用则 CPU 兜底
                # 懒加载（不常驻 1.3GB bge-large 权重，省内存）；
                # NPU 不可用（无设备/驱动/权限）才加载 CPU 兜底。
                npu_ok = False
                if probe_npu(runner_path=self._npu._runner_path):
                    npu_ok = self._npu.load()
                if npu_ok:
                    self._dimensions = self._npu.dimensions or HID
                    self._loaded = True
                    logger.info("adaptive_embed.ready threshold={} npu_ok=True dims={} cpu=lazy",
                                self._threshold, self._dimensions)
                    return True
                # NPU 不可用 → CPU 兜底（bge-large 1024 维，加载约 5s）
                cpu_ok = self._cpu.load()
                if not cpu_ok:
                    self._load_error = self._cpu._load_error or self._npu._load_error
                    logger.warning("adaptive_embed.load_failed npu_error={} cpu_error={}",
                                   self._npu._load_error, self._cpu._load_error)
                    return False
                self._dimensions = self._cpu.dimensions or HID
                self._loaded = True
                logger.info("adaptive_embed.ready threshold={} npu_ok=False dims={} cpu=active "
                            "error={}", self._threshold, self._dimensions, self._npu._load_error)
                return True
            except Exception as e:  # noqa: BLE001
                self._load_error = str(e)
                logger.warning("adaptive_embed.load_failed error={}", str(e))
                return False

    # ── 推理 ──────────────────────────────────────────────

    def _token_lens(self, texts: list[str]) -> list[int]:
        encs = self._tokenizer.encode_batch(
            [f"{self._cpu._query_prefix}{t}" if self._cpu._query_prefix else t for t in texts],
            add_special_tokens=True,
        )
        return [len(e.ids) for e in encs]

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """按词元长度路由到 CPU（短）或 NPU（长），结果按原顺序返回。"""
        if not texts:
            return []
        if not self._loaded and not self.load():
            return []
        try:
            lens = self._token_lens(texts)
        except Exception as e:  # noqa: BLE001
            logger.warning("adaptive_embed.tokenize_failed error={}", str(e))
            return []
        short_i = [i for i, l in enumerate(lens) if l <= self._threshold]
        long_i = [i for i, l in enumerate(lens) if l > self._threshold]
        result: list[list[float] | None] = [None] * len(texts)
        if short_i:
            # CPU 懒加载：NPU 模式 CPU 未常驻，短文本首次走到 CPU 时再加载
            if not self._cpu.ready:
                self._cpu.load()
            vecs = self._cpu.encode_batch([texts[i] for i in short_i])
            for i, v in zip(short_i, vecs):
                result[i] = v
        if long_i:
            lt = [texts[i] for i in long_i]
            vecs = self._npu.encode_batch(lt) if self._npu.ready else []
            if not vecs:  # NPU 失败降级 CPU（同样懒加载兜底）
                logger.warning("adaptive_embed.npu_degraded_to_cpu n={}", len(lt))
                if not self._cpu.ready:
                    self._cpu.load()
                vecs = self._cpu.encode_batch(lt)
            for i, v in zip(long_i, vecs):
                result[i] = v
        return [v for v in result if v is not None]

    def embed(self, text: str) -> list[float]:
        batch = self.encode_batch([text])
        return batch[0] if batch else []

    def npu_stats(self) -> dict:
        """NPU 实时状态（供算力设备检测页展示占用/性能）。"""
        return {
            "resident": self._npu.ready,
            "busy": self._npu.busy,
            "last_call_ms": self._npu.last_call_ms,
            "calls": self._npu.call_count,
        }

    def close(self) -> None:
        self._npu.close()
        self._cpu.close()
        self._loaded = False