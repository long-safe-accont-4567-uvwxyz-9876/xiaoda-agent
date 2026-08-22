"""ffmpeg/ffprobe 可执行定位 —— vendor 随项目分发优先，系统 PATH 兜底

路径回退链：
1. <项目根>/vendor/ffmpeg/<platform>/ffmpeg[.exe]  （scripts/fetch_ffmpeg.py 下载的
   静态构建；PyInstaller 打包时进 binaries，位于 sys._MEIPASS/ffmpeg/<platform>/）
2. shutil.which("ffmpeg")                            （系统安装）

返回 None 表示两者皆无，调用方按功能降级处理（如视频壁纸上传拒绝）。
"""
from __future__ import annotations

import platform
import shutil
import sys
from functools import lru_cache
from pathlib import Path


def _platform_dir() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "win32-x64"
    if system == "linux":
        return "linux-arm64" if machine in ("aarch64", "arm64") else "linux-x64"
    return f"{system}-{machine}"


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    # PyInstaller 冻结环境：二进制在 _MEIPASS 下
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass) / "ffmpeg" / _platform_dir())
    # 源码运行：项目根/vendor/ffmpeg/<platform>/
    dirs.append(Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg" / _platform_dir())
    return dirs


def _runnable(path: str) -> bool:
    """二进制可执行性探活：vendor 静态构建可能与本机 CPU/内核不兼容
    （实测 eugeneware arm64 构建在本机 SoC 上 SIGBUS），仅凭可执行位不够。"""
    import subprocess

    try:
        return subprocess.run(
            [path, "-version"], capture_output=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@lru_cache(maxsize=None)
def find_ffmpeg() -> str | None:
    """定位 ffmpeg 可执行文件；vendor 优先，PATH 兜底，均无返回 None。

    vendor 候选需通过 -version 探活（防不兼容静态构建）；PATH 结果信任系统。
    """
    exe = "ffmpeg.exe" if platform.system() == "Windows" else "ffmpeg"
    for d in _candidate_dirs():
        cand = d / exe
        if cand.is_file() and cand.stat().st_mode & 0o111 and _runnable(str(cand)):
            return str(cand)
    return shutil.which("ffmpeg")


@lru_cache(maxsize=None)
def find_ffprobe() -> str | None:
    """定位 ffprobe；与 find_ffmpeg 同回退链。"""
    exe = "ffprobe.exe" if platform.system() == "Windows" else "ffprobe"
    for d in _candidate_dirs():
        cand = d / exe
        if cand.is_file() and cand.stat().st_mode & 0o111:
            return str(cand)
    return shutil.which("ffprobe")
