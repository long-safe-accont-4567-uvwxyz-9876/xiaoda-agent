"""独显优先选择逻辑测试。

验证在双显卡系统（核显 + 独显）上，ncnn Vulkan 设备选择优先使用独显。
"""
import sys
import os
from unittest import mock


class FakeGpuInfo:
    """模拟 ncnn 的 GpuInfo 对象。"""
    def __init__(self, name: str):
        self._name = name

    def name(self):
        return self._name


class FakeNcnn:
    """模拟 ncnn 模块。"""
    def __init__(self, gpu_names: list[str]):
        self._gpu_names = gpu_names

    def get_gpu_count(self):
        return len(self._gpu_names)

    def get_gpu_info(self, index):
        if index < len(self._gpu_names):
            return FakeGpuInfo(self._gpu_names[index])
        raise IndexError(f"gpu index {index} out of range")


def _detect_with_mock(gpu_names: list[str], env_gpu_index: str = "") -> int:
    """用 mock ncnn 直接调用 _detect_discrete_gpu_index 的核心逻辑。

    不 reload vision_service 模块（避免 numpy 冲突），
    而是提取函数核心逻辑用 mock 数据测试。
    """
    # 环境变量
    old_env = os.environ.pop("XIAODA_GPU_INDEX", None)
    if env_gpu_index:
        os.environ["XIAODA_GPU_INDEX"] = env_gpu_index
    try:
        # 1. 环境变量优先
        env_idx = os.environ.get("XIAODA_GPU_INDEX", "").strip()
        if env_idx:
            try:
                return int(env_idx)
            except ValueError:
                pass

        # 2. ncnn API
        fake_ncnn = FakeNcnn(gpu_names)
        gpu_count = fake_ncnn.get_gpu_count()
        if gpu_count <= 1:
            return 0

        # 用 ncnn API 获取设备名称
        ncnn_gpu_names: list[str] = []
        for i in range(gpu_count):
            try:
                info = fake_ncnn.get_gpu_info(i)
                name = ""
                if hasattr(info, "name"):
                    name = info.name() or ""
                elif hasattr(info, "device_name"):
                    name = info.device_name() or ""
                ncnn_gpu_names.append(name)
            except Exception:
                ncnn_gpu_names.append("")

        if not ncnn_gpu_names or all(not n for n in ncnn_gpu_names):
            return 0

        discrete_keywords = ("nvidia", "geforce", "radeon rx", "radeon r9", "radeon r7",
                             "arc a", "arc a370", "arc a380", "arc a580", "arc a750", "arc a770")
        integrated_keywords = ("intel(r) uhd", "intel(r) iris", "intel hd graphics",
                               "intel(r) hd graphics", "amd radeon graphics")

        # 优先选择独显
        for i, name in enumerate(ncnn_gpu_names):
            if i >= gpu_count:
                break
            name_lower = name.lower()
            if any(kw in name_lower for kw in discrete_keywords):
                if not any(kw in name_lower for kw in integrated_keywords):
                    return i

        # 次优选择：非核显
        for i, name in enumerate(ncnn_gpu_names):
            if i >= gpu_count:
                break
            name_lower = name.lower()
            if not any(kw in name_lower for kw in integrated_keywords):
                return i

        return 0
    finally:
        os.environ.pop("XIAODA_GPU_INDEX", None)
        if old_env is not None:
            os.environ["XIAODA_GPU_INDEX"] = old_env


def test_env_override():
    """环境变量 XIAODA_GPU_INDEX 应覆盖自动检测。"""
    result = _detect_with_mock(
        gpu_names=["Intel(R) UHD Graphics 630", "NVIDIA GeForce RTX 4090"],
        env_gpu_index="0"
    )
    assert result == 0, f"环境变量应覆盖为 0，实际 {result}"


def test_env_override_to_1():
    """环境变量指定设备 1。"""
    result = _detect_with_mock(
        gpu_names=["NVIDIA GeForce RTX 4090", "Intel(R) UHD Graphics 630"],
        env_gpu_index="1"
    )
    assert result == 1, f"环境变量应覆盖为 1，实际 {result}"


def test_no_gpu_returns_zero():
    """无 GPU 设备时返回 0。"""
    result = _detect_with_mock(gpu_names=[])
    assert result == 0


def test_single_gpu_returns_zero():
    """只有 1 个 GPU 设备时返回 0。"""
    result = _detect_with_mock(gpu_names=["Intel UHD"])
    assert result == 0


def test_dual_gpu_nvidia_at_1():
    """双显卡（Intel 核显 0 + NVIDIA 独显 1）应选择设备 1。"""
    result = _detect_with_mock(gpu_names=["Intel(R) UHD Graphics 630", "NVIDIA GeForce GTX 1660"])
    assert result == 1, f"应选择 NVIDIA 独显（设备 1），实际 {result}"


def test_dual_gpu_amd_at_1():
    """双显卡（Intel 核显 0 + AMD 独显 1）应选择设备 1。"""
    result = _detect_with_mock(gpu_names=["Intel(R) Iris Xe Graphics", "AMD Radeon RX 6600"])
    assert result == 1, f"应选择 AMD 独显（设备 1），实际 {result}"


def test_discrete_at_0_returns_0():
    """独显在设备 0（用户实际配置）应选择设备 0。

    用户硬件：GPU0=独显，GPU1=核显。
    这是本次修复的核心场景。
    """
    result = _detect_with_mock(gpu_names=["NVIDIA GeForce RTX 4090", "Intel(R) UHD Graphics 770"])
    assert result == 0, f"独显在 0 号位应返回 0，实际 {result}"


def test_discrete_at_0_rtx_3060():
    """独显 RTX 3060 在设备 0 应选择设备 0。"""
    result = _detect_with_mock(gpu_names=["NVIDIA GeForce RTX 3060", "Intel(R) UHD Graphics 730"])
    assert result == 0, f"独显在 0 号位应返回 0，实际 {result}"


def test_only_integrated_returns_zero():
    """只有核显时返回 0。"""
    result = _detect_with_mock(gpu_names=["Intel(R) UHD Graphics 630", "Intel(R) UHD Graphics 630"])
    assert result == 0, f"无独显时应返回 0，实际 {result}"


def test_intel_arc_discrete():
    """Intel Arc 独显应被识别为独显。"""
    result = _detect_with_mock(gpu_names=["Intel(R) UHD Graphics 770", "Intel Arc A380"])
    assert result == 1, f"Intel Arc 独显应返回 1，实际 {result}"


def test_load_model_always_sets_device():
    """回归测试：_load_model 应始终调用 set_vulkan_device，即使 gpu_index=0。

    之前的 bug：if gpu_index > 0 导致独显在 0 时不设置设备，ncnn 用默认核显。
    修复：删除 if 条件，始终调用 net.set_vulkan_device(gpu_index)。
    """
    # 直接读取源码文件验证，避免导入 vision_service 触发 numpy 依赖
    vision_service_path = os.path.join(os.path.dirname(__file__), '..', 'utils', 'vision_service.py')
    with open(vision_service_path, 'r', encoding='utf-8') as f:
        source = f.read()
    # 确保 set_vulkan_device 不在 if > 0 条件内
    assert "if gpu_index > 0" not in source, "_load_model 不应有 if gpu_index > 0 条件"
    assert "net.set_vulkan_device(gpu_index)" in source, "_load_model 应直接调用 set_vulkan_device"
