"""运行时能力探测 —— 替代硬编码硬件信息。

基于 AgentBoot 思路，适配 xiaoda-agent 的本地场景：
- platform.system() / platform.machine() 获取基础平台信息
- psutil 检测 CPU/内存
- nvidia-smi / rocm-smi 检测 GPU
- /sys/class/gpio, /dev/i2c-0, /dev/video0 检测 SBC 硬件接口
- shutil.which() 检测可用命令行工具

设计原则：能力-上下文分离（ArXiv:2603.14332），Agent 的身份和能力在运行时动态确定。
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class CapabilityProfile:
    """Agent 运行时能力画像。"""
    schema_version: int = 1  # 画像版本，供下游判断字段可用性
    platform_os: str = ""
    platform_arch: str = ""
    hostname: str = ""
    os_release: str = ""
    processor: str = ""
    cpu_cores: int = 0
    total_ram_gb: float = 0.0
    has_gpu: bool = False
    gpu_name: str = ""
    gpu_memory_mb: int = 0
    has_cuda: bool = False
    cuda_version: str = ""
    has_gpio: bool = False
    has_i2c: bool = False
    has_camera: bool = False
    is_sbc: bool = False  # Single Board Computer (香橙派/树莓派)
    available_tools: list[str] = field(default_factory=list)
    npu_enabled: bool = False

    def to_prompt_segment(self, data_dir: str = "") -> str:
        """生成注入 system prompt 的能力描述段。"""
        lines = ["[本机硬件信息]"]
        lines.append(
            f"主机名: {self.hostname} | 架构: {self.platform_arch} | 处理器: {self.processor or '未知'}"
        )
        lines.append(
            f"系统: {self.platform_os} {self.os_release} ({self.platform_arch})"
        )

        # 硬件接口
        hw_features = []
        if self.has_gpio:
            hw_features.append("GPIO (40pin排针)")
        if self.has_i2c:
            hw_features.append("I2C")
        if self.has_camera:
            hw_features.append("摄像头")
        if hw_features:
            lines.append(f"可用接口: {' / '.join(hw_features)} / SPI / UART / PWM")
        else:
            lines.append("可用接口: 无特殊硬件接口")

        # 可用工具（根据硬件能力动态列出）
        tools = []
        if self.has_gpio:
            tools.append("gpio_control(引脚控制)")
        if self.has_i2c:
            tools.append("i2c_comm(I2C通信)")
        tools.append("hardware_status(硬件监控)")
        tools.append("service_manage(服务管理)")
        tools.append("network_diag(网络诊断)")
        tools.append("dev_assist(开发辅助)")
        if self.has_camera:
            tools.append("camera_capture(拍照)")
            tools.append("vision_analyze(视觉分析)")
        if tools:
            lines.append(f"可用工具: {' / '.join(tools)}")

        if data_dir:
            lines.append(f"数据存储: {data_dir}")

        # 摄像头与视觉模型
        if self.has_camera:
            npu_status = "NPU视觉识别已启用" if self.npu_enabled else "视觉识别（ncnn后端）"
            lines.append(
                f"摄像头: 已连接 (/dev/video0) | 视觉模型: YOLOv10-nano (ncnn CPU) | {npu_status}"
            )

        # GPU 信息（如有）
        if self.has_gpu:
            gpu_line = f"GPU: {self.gpu_name}"
            if self.gpu_memory_mb:
                gpu_line += f" ({self.gpu_memory_mb}MB)"
            if self.has_cuda:
                gpu_line += f" | CUDA: {self.cuda_version}"
            lines.append(gpu_line)

        return "\n".join(lines)


# 模块级缓存：启动时探测一次，后续直接返回
_profile_cache: CapabilityProfile | None = None


def detect_capabilities() -> CapabilityProfile:
    """运行时探测 Agent 能力，结果缓存。"""
    global _profile_cache
    if _profile_cache is not None:
        return _profile_cache

    profile = CapabilityProfile()

    # 基础平台信息
    _uname = platform.uname()
    profile.platform_os = _uname.system
    profile.platform_arch = _uname.machine
    profile.os_release = _uname.release
    profile.processor = _uname.processor or ""
    try:
        profile.hostname = socket.gethostname()
    except Exception:
        profile.hostname = "unknown"

    # NPU 状态（从环境变量读取）
    profile.npu_enabled = os.getenv("ENABLE_NPU", "").lower() in ("1", "true", "yes")

    # CPU 核心
    profile.cpu_cores = _detect_cpu_cores()

    # 内存
    profile.total_ram_gb = _detect_ram()

    # GPU 检测
    gpu_info = _detect_gpu()
    profile.has_gpu = gpu_info.get("has_gpu", False)
    profile.gpu_name = gpu_info.get("name", "")
    profile.gpu_memory_mb = gpu_info.get("memory_mb", 0)
    profile.has_cuda = gpu_info.get("has_cuda", False)
    profile.cuda_version = gpu_info.get("cuda_version", "")

    # SBC 硬件检测（仅在 Linux 上检测）
    if profile.platform_os == "Linux":
        profile.has_gpio = _path_exists("/sys/class/gpio") or _path_exists("/dev/gpiochip0")
        profile.has_i2c = _path_exists("/dev/i2c-0")
        profile.has_camera = _path_exists("/dev/video0")
        profile.is_sbc = _detect_sbc()

    # 可用工具检测
    profile.available_tools = _detect_available_tools()

    _profile_cache = profile
    logger.info(
        f"capability.detected os={profile.platform_os} arch={profile.platform_arch} "
        f"sbc={profile.is_sbc} gpio={profile.has_gpio} i2c={profile.has_i2c} "
        f"camera={profile.has_camera} gpu={profile.has_gpu} cores={profile.cpu_cores} "
        f"ram={profile.total_ram_gb:.1f}GB"
    )
    return profile


def _detect_cpu_cores() -> int:
    try:
        import psutil
        return psutil.cpu_count(logical=True) or 1
    except ImportError:
        return os.cpu_count() or 1


def _detect_ram() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except ImportError:
        return 0.0


def _detect_gpu() -> dict:
    result: dict = {"has_gpu": False}

    # nvidia-smi 检测
    if shutil.which("nvidia-smi"):
        try:
            output = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                timeout=5, text=True,
            ).strip()
            if output:
                parts = output.split(", ")
                result["has_gpu"] = True
                result["name"] = parts[0]
                result["memory_mb"] = int(float(parts[1])) if len(parts) > 1 else 0
        except Exception:
            logger.debug("capability_detector.gpu_detect_failed", exc_info=True)

        # CUDA 版本
        try:
            output = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                timeout=5, text=True,
            ).strip()
            if output and output != "N/A":
                result["has_cuda"] = True
                result["cuda_version"] = output
        except Exception:
            logger.debug("capability.cuda_detect_failed", exc_info=True)

    # ROCm 检测（AMD GPU）
    if not result["has_gpu"] and shutil.which("rocm-smi"):
        try:
            output = subprocess.check_output(
                ["rocm-smi", "--showproductname"], timeout=5, text=True,
            )
            if "GPU" in output:
                result["has_gpu"] = True
                result["name"] = "AMD GPU"
                result["has_cuda"] = False
        except Exception:
            logger.debug("capability_detector.rocm_detect_failed", exc_info=True)

    return result


def _path_exists(path: str) -> bool:
    return os.path.exists(path)


def _detect_sbc() -> bool:
    """检测是否为单板计算机（香橙派/树莓派等）。"""
    sbc_indicators = ["/proc/device-tree/model", "/proc/device-tree/compatible"]
    for path in sbc_indicators:
        try:
            with open(path) as f:
                content = f.read().lower()
                if any(kw in content for kw in ["raspberry", "orange pi", "orangepi",
                                                  "nanopi", "rockpi", "xunlong"]):
                    return True
        except Exception:
            continue
    return False


def _detect_available_tools() -> list[str]:
    """检测系统可用命令行工具。"""
    tools = []
    for tool in ["git", "python3", "pip", "docker", "ffmpeg", "node", "npm"]:
        if shutil.which(tool):
            tools.append(tool)
    return tools