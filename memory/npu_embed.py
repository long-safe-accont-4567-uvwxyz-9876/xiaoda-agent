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
import sys
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

from memory.local_embed import LocalEmbeddingProvider  # noqa: E402

# runner 流协议常量（与 bge_npu_runner.c --serve 一致）
MAGIC = b"BGEVEC01"
SEQ = 512
HID = 512
VEC_BYTES = HID * 4          # 512 float32
INPUT_BYTES = 3 * SEQ * 4    # 6144：input_ids/attention_mask/token_type_ids 各 512×int32


def _default_runner() -> str:
    """runner 可执行文件默认路径（相对 ai-agent 根目录）。"""
    root = Path(__file__).resolve().parent.parent
    return str(root / "scripts" / "npu" / "bge_npu_runner")


def _default_nbg() -> str:
    """NBG 默认路径（INT16 固化正式版）。"""
    return os.getenv(
        "NPU_NBG",
        "/media/orangepi/KIOXIA/nahida-data/npu/bge_npu_kit/npu_input/bge_small_zh.nb",
    )


def probe_npu(runner_path: str = "", timeout_s: float = 15.0) -> bool:
    """探测本机 NPU（VIP9000）是否可用。

    通过 runner 的 --probe 模式验证（vip_init 成功 = NPU 设备/驱动可用）。
    runner 文件不存在（如纯 CPU 机器 / Windows 打包版）直接返回 False。
    成功退出码 0 → True；失败/超时/异常 → False。调用方据此自动降级纯 CPU。
    """
    path = Path(runner_path or _default_runner())
    # 非 Linux 平台（Windows/macOS）无 VIP9000：直接判定不可用，不 spawn runner。
    # 兼容 Windows 打包版：默认 CPU 推理，绝不尝试执行 aarch64 runner。
    if not sys.platform.startswith("linux"):
        logger.info("npu_probe.skipped platform={}", sys.platform)
        return False
    if not path.exists():
        logger.info("npu_probe.skipped runner_missing={}", str(path))
        return False
    try:
        proc = subprocess.run(
            ["sudo", "-n", str(path), "--probe", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_s,
            check=False,
        )
        ok = proc.returncode == 0
        logger.info("npu_probe.result ok={} rc={}", ok, proc.returncode)
        return ok
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("npu_probe.failed error={}", str(e))
        return False


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
               "--serve", "--seq", str(SEQ), "--quiet"]
        log_path = Path(__file__).resolve().parent.parent / "logs" / "npu_embed_runner.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._stderr_f = open(log_path, "ab")
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr_f,
        )
        # 逐块读 stdout 直到 magic，之前内容（VIPLite 版本横幅）全部丢弃；
        # 加超时兜底：runner 初始化挂起（如 NPU 设备忙/驱动异常）时不阻塞服务
        buf = b""
        deadline = time.monotonic() + self._timeout_s
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
        # (超时抛出后由 load() 捕获，Adaptive 层探测已先排除大部分无 NPU 场景)

    # ── 推理 ──────────────────────────────────────────────

    def _apply_prefix(self, text: str) -> str:
        return f"{self._query_prefix}{text}" if self._query_prefix else text

    def _tokenize(self, texts: list[str]) -> bytes:
        """批量分词并 padding 到固定 SEQ，打包为 runner 输入字节流。"""
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
            flat += list(enc.type_ids[:SEQ]) + [0] * pad  # token_type_ids
        return struct.pack(f"<{len(flat)}i", *flat)

    def encode_batch(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（同步，阻塞等待 NPU，调用方应经 to_thread 执行）。"""
        if not texts:
            return []
        if not self._loaded and not self.load():
            return []
        try:
            payload = self._tokenize(texts)
        except Exception as e:  # noqa: BLE001
            logger.warning("npu_embed.tokenize_failed error={}", str(e))
            return []
        # 重试一次：子进程异常退出时重启
        for attempt in (0, 1):
            try:
                return self._infer(payload, len(texts))
            except Exception as e:  # noqa: BLE001
                logger.warning("npu_embed.infer_failed attempt={} error={}",
                               attempt, str(e))
                self._restart()
        return []

    def _infer(self, payload: bytes, n: int) -> list[list[float]]:
        with self._io_lock:
            assert self._proc and self._proc.stdin and self._proc.stdout
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
            pass
        try:
            if self._stderr_f:
                self._stderr_f.close()
        except Exception:  # noqa: BLE001
            pass
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
                pass
        try:
            if self._stderr_f:
                self._stderr_f.close()
        except Exception:  # noqa: BLE001
            pass
        self._proc = None
        self._stderr_f = None
        self._loaded = False


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
        self._threshold = int(threshold or os.getenv("LOCAL_EMBED_THRESHOLD", "256"))
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
                cpu_ok = self._cpu.load()
                # NPU 探测短路：无 NPU / runner 缺失 / 权限不足（sudo 不可用）→
                # 不 spawn 常驻进程，自动降级纯 CPU（CPU 为兜底必须可用）
                if cpu_ok and probe_npu(runner_path=self._npu._runner_path):
                    npu_ok = self._npu.load()
                else:
                    npu_ok = False
                    self._npu._load_error = (
                        "npu_probe_failed" if cpu_ok else "cpu_unavailable")
                self._dimensions = self._cpu.dimensions or self._npu.dimensions or HID
                self._loaded = cpu_ok  # CPU 为兜底，必须可用
                if not cpu_ok:
                    self._load_error = self._cpu._load_error
                    logger.warning("adaptive_embed.load_failed cpu_error={}", self._load_error)
                else:
                    logger.info("adaptive_embed.ready threshold={} npu_ok={} dims={}",
                                self._threshold, npu_ok, self._dimensions)
                    if not npu_ok:
                        logger.warning(
                            "adaptive_embed.no_npu_fallback cpu_only=True "
                            "error={}", self._npu._load_error)
                return self._loaded
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
            vecs = self._cpu.encode_batch([texts[i] for i in short_i])
            for i, v in zip(short_i, vecs):
                result[i] = v
        if long_i:
            lt = [texts[i] for i in long_i]
            vecs = self._npu.encode_batch(lt) if self._npu.ready else []
            if not vecs:  # NPU 失败降级 CPU
                logger.warning("adaptive_embed.npu_degraded_to_cpu n={}", len(lt))
                vecs = self._cpu.encode_batch(lt)
            for i, v in zip(long_i, vecs):
                result[i] = v
        return [v for v in result if v is not None]

    def embed(self, text: str) -> list[float]:
        batch = self.encode_batch([text])
        return batch[0] if batch else []

    def close(self) -> None:
        self._npu.close()
        self._cpu.close()
        self._loaded = False
