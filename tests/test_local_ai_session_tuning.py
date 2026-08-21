"""ORT 会话自动调优单元测试（不依赖真实 onnxruntime）。

覆盖：
1. build_session_options：纯 CPU 设 intra_op_num_threads（物理核数）
2. DML：enable_mem_pattern=False + ORT_SEQUENTIAL
3. 非 CPU：session.disable_cpu_ep_fallback=1
4. TRT provider_options：fp16 / engine_cache / max_workspace
5. CUDA provider_options：cudnn_conv_algo_search=HEURISTIC
6. provider_rank 顺序 TRT<CUDA=ROCM=DML<CPU
7. default_engine_cache_dir 遵循 env
8. auto_provider_options 尊重 fp16 开关与 ORT_TRT_FP16 env

通过注入 fake onnxruntime 模块（SessionOptions 记录属性写入）构造，
脱离真实 onnxruntime 依赖。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from local_ai.runtimes import session_tuning as st


class _FakeSessionOptions:
    """记录 onnxruntime.SessionOptions 属性写入的 fake。"""

    def __init__(self) -> None:
        self.enable_mem_pattern = True
        self.execution_mode = None
        self.intra_op_num_threads = None
        self.inter_op_num_threads = None
        self._config_entries: dict[str, str] = {}

    def add_session_config_entry(self, key: str, value: str) -> None:
        self._config_entries[key] = value


class _FakeOrt:
    """最小 onnxruntime 模块替身。"""

    ORT_SEQUENTIAL = "SEQUENTIAL"

    def __init__(self, options: _FakeSessionOptions) -> None:
        self._options = options

    def SessionOptions(self) -> _FakeSessionOptions:  # noqa: N802
        return self._options


@pytest.fixture()
def fake_ort(monkeypatch):
    """注入 fake onnxruntime 到 session_tuning 模块。"""
    opts = _FakeSessionOptions()
    monkeypatch.setattr(st, "ort", _FakeOrt(opts))
    return opts


def _snap(**overrides) -> st.HardwareSnap:
    defaults = dict(
        platform="linux",
        architecture="aarch64",
        cpu_logical=8,
        cpu_physical=4,
        gpu_vendor=None,
        gpu_name=None,
        vram_mb=0,
    )
    defaults.update(overrides)
    return st.HardwareSnap(**defaults)


# ── build_session_options ────────────────────────────────────────────


def test_build_pure_cpu_sets_intra_threads(fake_ort, monkeypatch):
    monkeypatch.setenv("ORT_INTRA_OP_THREADS", "")
    opts = st.build_session_options(["CPUExecutionProvider"], hardware=_snap())
    assert opts.intra_op_num_threads == 4  # 物理核心数


def test_build_cpu_threads_env_overrides_physical(fake_ort, monkeypatch):
    monkeypatch.setenv("ORT_INTRA_OP_THREADS", "2")
    opts = st.build_session_options(["CPUExecutionProvider"], hardware=_snap())
    assert opts.intra_op_num_threads == 2


def test_build_dml_sets_mem_pattern_and_sequential(fake_ort):
    opts = st.build_session_options(
        ["DmlExecutionProvider"], hardware=_snap()
    )
    assert opts.enable_mem_pattern is False
    assert opts.execution_mode == "SEQUENTIAL"


def test_build_gpu_disables_cpu_fallback(fake_ort):
    opts = st.build_session_options(
        ["TensorrtExecutionProvider"], hardware=_snap(gpu_vendor="nvidia", vram_mb=8192)
    )
    assert opts._config_entries.get("session.disable_cpu_ep_fallback") == "1"


def test_build_cpu_no_cpu_fallback_gate(fake_ort):
    opts = st.build_session_options(["CPUExecutionProvider"], hardware=_snap())
    assert "session.disable_cpu_ep_fallback" not in opts._config_entries


# ── auto_provider_options ────────────────────────────────────────────


def test_trt_options_nvidia_fp16_and_cache(monkeypatch):
    monkeypatch.setenv("ORT_TRT_FP16", "1")
    cache = Path("/tmp/ort_cache")
    opts = st.auto_provider_options(
        "TensorrtExecutionProvider",
        hardware=_snap(gpu_vendor="nvidia", vram_mb=8192),
        engine_cache_dir=cache,
    )
    assert opts is not None
    assert opts["trt_fp16_enable"] == "1"
    assert opts["trt_engine_cache_enable"] == "1"
    assert opts["trt_engine_cache_path"] == str(cache)
    assert opts["trt_engine_hw_compatible"] == "1"
    # max_workspace = 显存 40% = 8192*0.4 MB
    assert opts["trt_max_workspace_size"] == str(int(8192 * 0.4) * 1024 * 1024)


def test_trt_fp16_off_via_env(monkeypatch):
    monkeypatch.setenv("ORT_TRT_FP16", "0")
    opts = st.auto_provider_options(
        "TensorrtExecutionProvider",
        hardware=_snap(gpu_vendor="nvidia", vram_mb=4096),
    )
    assert opts["trt_fp16_enable"] == "0"


def test_trt_fp16_non_nvidia_defaults_off(monkeypatch):
    monkeypatch.setenv("ORT_TRT_FP16", "")  # 未设 env
    opts = st.auto_provider_options(
        "TensorrtExecutionProvider",
        hardware=_snap(gpu_vendor="amd", vram_mb=4096),
    )
    assert opts["trt_fp16_enable"] == "0"


def test_trt_fp16_explicit_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv("ORT_TRT_FP16", "0")
    opts = st.auto_provider_options(
        "TensorrtExecutionProvider",
        hardware=_snap(gpu_vendor="nvidia", vram_mb=4096),
        fp16=True,
    )
    assert opts["trt_fp16_enable"] == "1"


def test_cuda_options_heuristic():
    opts = st.auto_provider_options(
        "CUDAExecutionProvider", hardware=_snap(gpu_vendor="nvidia")
    )
    assert opts == {"cudnn_conv_algo_search": "HEURISTIC"}


def test_auto_options_none_for_cpu():
    assert st.auto_provider_options("CPUExecutionProvider", hardware=_snap()) is None


# ── provider_rank ─────────────────────────────────────────────────────


def test_provider_rank_order():
    assert (
        st.provider_rank("TensorrtExecutionProvider")
        < st.provider_rank("CUDAExecutionProvider")
        < st.provider_rank("CPUExecutionProvider")
    )
    assert st.provider_rank("ROCMExecutionProvider") == st.provider_rank(
        "CUDAExecutionProvider"
    )
    assert st.provider_rank("DmlExecutionProvider") == st.provider_rank(
        "CUDAExecutionProvider"
    )
    # 本地 NPU（VIPLite）与 GPU 同等最优先，且必须优于 CPU（否则 NPU 永远选不上）
    assert st.provider_rank("VIPLite") == st.provider_rank(
        "TensorrtExecutionProvider"
    )
    assert st.provider_rank("VIPLite") < st.provider_rank("CPUExecutionProvider")
    # 未知 provider 落在 CPU 之后
    assert st.provider_rank("CPUExecutionProvider") < st.provider_rank("UnknownEP")


# ── default_engine_cache_dir ──────────────────────────────────────────


def test_default_engine_cache_dir_uses_env(monkeypatch):
    monkeypatch.setenv("LOCAL_AI_CACHE_DIR", "/data/custom")
    monkeypatch.delenv("KIOXIA_DATA_DIR", raising=False)
    assert st.default_engine_cache_dir() == Path("/data/custom") / "ort_cache"


def test_default_engine_cache_dir_uses_kioxia(monkeypatch):
    monkeypatch.delenv("LOCAL_AI_CACHE_DIR", raising=False)
    monkeypatch.setenv("KIOXIA_DATA_DIR", "/kioxia/data")
    assert st.default_engine_cache_dir() == Path("/kioxia/data") / "ort_cache"


def test_default_engine_cache_dir_falls_back_home(monkeypatch):
    monkeypatch.delenv("LOCAL_AI_CACHE_DIR", raising=False)
    monkeypatch.delenv("KIOXIA_DATA_DIR", raising=False)
    assert st.default_engine_cache_dir() == Path.home() / ".ai-agent" / "data" / "ort_cache"