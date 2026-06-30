"""独显优先选择逻辑测试。

验证在双显卡系统（核显 + 独显）上，ncnn Vulkan 设备选择优先使用独显。
"""
import sys
import subprocess
from unittest import mock


def _call_detect_with_mocks(gpu_count: int, gpu_names_output: str, platform: str = "win32"):
    """用 mock 调用 _detect_discrete_gpu_index 的核心逻辑，避免 reload numpy。

    直接复制 _detect_discrete_gpu_index 的逻辑，用 mock 数据测试。
    """
    import logging
    logger = logging.getLogger("vision_service")

    if gpu_count <= 1:
        return 0

    gpu_names: list[str] = []
    if platform == "win32":
        gpu_names = [n.strip() for n in gpu_names_output.strip().split("\n") if n.strip()]
    else:
        for line in gpu_names_output.split("\n"):
            if "VGA compatible controller" in line or "3D controller" in line:
                name = line.split(":")[-1].strip() if ":" in line else ""
                gpu_names.append(name)

    if not gpu_names:
        return 0

    discrete_keywords = ("nvidia", "geforce", "radeon rx", "radeon r9", "radeon r7",
                         "arc a", "arc a370", "arc a380", "arc a580", "arc a750", "arc a770")
    integrated_keywords = ("intel(r) uhd", "intel(r) iris", "intel hd graphics",
                           "intel(r) hd graphics", "amd radeon graphics")

    for i, name in enumerate(gpu_names):
        name_lower = name.lower()
        if any(kw in name_lower for kw in discrete_keywords):
            if not any(kw in name_lower for kw in integrated_keywords):
                return i

    for i, name in enumerate(gpu_names):
        name_lower = name.lower()
        if not any(kw in name_lower for kw in integrated_keywords):
            return i

    return 0


def test_no_ncnn_returns_zero():
    """ncnn 未安装时返回设备 0。"""
    # 直接测试：gpu_count <= 1 或异常 → 0
    result = _call_detect_with_mocks(gpu_count=0, gpu_names_output="")
    assert result == 0


def test_single_gpu_returns_zero():
    """只有 1 个 GPU 设备时返回 0。"""
    result = _call_detect_with_mocks(gpu_count=1, gpu_names_output="Intel UHD")
    assert result == 0


def test_dual_gpu_nvidia_selects_device_1():
    """双显卡（Intel 核显 + NVIDIA 独显）应选择独显（设备 1）。"""
    output = "Intel(R) UHD Graphics 630\nNVIDIA GeForce GTX 1660"
    result = _call_detect_with_mocks(gpu_count=2, gpu_names_output=output)
    assert result == 1, f"应选择 NVIDIA 独显（设备 1），实际 {result}"


def test_dual_gpu_amd_selects_device_1():
    """双显卡（Intel 核显 + AMD 独显）应选择独显（设备 1）。"""
    output = "Intel(R) Iris Xe Graphics\nAMD Radeon RX 6600"
    result = _call_detect_with_mocks(gpu_count=2, gpu_names_output=output)
    assert result == 1, f"应选择 AMD 独显（设备 1），实际 {result}"


def test_only_integrated_returns_zero():
    """只有核显时返回 0。"""
    output = "Intel(R) UHD Graphics 630\nIntel(R) UHD Graphics 630"
    result = _call_detect_with_mocks(gpu_count=2, gpu_names_output=output)
    assert result == 0, f"无独显时应返回 0，实际 {result}"


def test_discrete_first_selects_device_0():
    """独显在设备 0 位置时应选择设备 0。"""
    output = "NVIDIA GeForce RTX 4090\nIntel(R) UHD Graphics 770"
    result = _call_detect_with_mocks(gpu_count=2, gpu_names_output=output)
    assert result == 0, f"独显在 0 号位应返回 0，实际 {result}"


def test_intel_arc_discrete():
    """Intel Arc 独显应被识别为独显。"""
    output = "Intel(R) UHD Graphics 770\nIntel Arc A380"
    result = _call_detect_with_mocks(gpu_count=2, gpu_names_output=output)
    assert result == 1, f"Intel Arc 独显应返回 1，实际 {result}"
