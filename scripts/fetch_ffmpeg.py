#!/usr/bin/env python3
"""下载 ffmpeg/ffprobe 静态构建到 vendor/ffmpeg/<platform>/（随项目分发）

来源：eugeneware/ffmpeg-static（GitHub Release，含各平台 LICENSE）。
平台映射：当前机器自动识别，或 --platform 显式指定（打包交叉场景）。
已存在且可执行时跳过（幂等）。用法：
    python scripts/fetch_ffmpeg.py [--platform linux-arm64|linux-x64|win32-x64]
"""
from __future__ import annotations

import argparse
import os
import platform
import stat
import sys
import urllib.request
from pathlib import Path

# Linux arm64 用 BtbN 构建——eugeneware 的 arm64 静态构建在本项目目标
# SoC（香橙派 Pi4 Pro, RK3588S 系）实测 SIGBUS 不可执行；BtbN 为通用
# arm64 排布且含 ffprobe。其余平台仍用 eugeneware 单二进制。
BTBN_ASSET = "ffmpeg-n8.1-latest-linuxarm64-gpl-8.1.tar.xz"
BTBN_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
            + BTBN_ASSET)
RELEASE_TAG = "b6.1.1"
BASE = f"https://github.com/eugeneware/ffmpeg-static/releases/download/{RELEASE_TAG}"
# GitHub release CDN 直连不稳定时走 gh-proxy 镜像（2026-08-22 实测可用）
MIRRORS = [
    "https://gh-proxy.com/https://github.com",
    "https://mirror.ghproxy.com/https://github.com",
    "https://github.com",  # 直连兜底
]


def _download(url_path: str, dst: Path) -> None:
    import shutil
    last_err: Exception | None = None
    for mirror in MIRRORS:
        try:
            req = urllib.request.Request(f"{mirror}/{url_path}")
            with urllib.request.urlopen(req, timeout=600) as resp, \
                    open(dst, "wb") as f:
                while chunk := resp.read(1 << 20):
                    f.write(chunk)
            return
        except Exception as e:
            last_err = e
            print(f"  源失败（{mirror}）: {e}", file=sys.stderr)
    if shutil.which("curl"):
        raise SystemExit(f"所有源失败，请手动: curl -L {MIRRORS[0]}/{url_path} -o {dst}")
    raise SystemExit(f"所有下载源均失败: {last_err}")


def detect_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "win32-x64"
    if system == "linux":
        return "linux-arm64" if machine in ("aarch64", "arm64") else "linux-x64"
    raise SystemExit(f"不支持的平台: {system}/{machine}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=["linux-arm64", "linux-x64", "win32-x64"],
                    default=detect_platform())
    ap.add_argument("--token", default=os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN"))
    args = ap.parse_args()

    out_dir = Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg" / args.platform
    out_dir.mkdir(parents=True, exist_ok=True)
    exe_suffix = ".exe" if args.platform.startswith("win") else ""

    if args.platform == "linux-arm64":
        # BtbN tar.xz：解包 bin/{ffmpeg,ffprobe} + LICENSE
        import tarfile

        tgz = out_dir / BTBN_ASSET
        if not tgz.exists():
            _download(f"BtbN/FFmpeg-Builds/releases/download/latest/{BTBN_ASSET}", tgz)
        with tarfile.open(tgz) as tf:
            for member in tf.getmembers():
                name = Path(member.name).name
                if name in ("ffmpeg", "ffprobe") and member.isfile():
                    extracted = tf.extractfile(member)
                    assert extracted is not None
                    dst = out_dir / name
                    dst.write_bytes(extracted.read())
                    dst.chmod(0o755)
                    print(f"完成: {dst}")
            lic = [m for m in tf.getmembers() if "LICENSE" in m.name]
            if lic:
                extracted = tf.extractfile(lic[0])
                assert extracted is not None
                (out_dir / "LICENSE").write_bytes(extracted.read())
        tgz.unlink(missing_ok=True)
        return 0

    for asset, dst_name in [
        (f"ffmpeg-{args.platform}", f"ffmpeg{exe_suffix}"),
        (f"ffprobe-{args.platform}", f"ffprobe{exe_suffix}"),
        (f"{args.platform}.LICENSE", "LICENSE"),
    ]:
        dst = out_dir / dst_name
        if dst.exists() and (dst_name == "LICENSE" or os.access(dst, os.X_OK)):
            print(f"已存在，跳过: {dst}")
            continue
        _download(f"eugeneware/ffmpeg-static/releases/download/{RELEASE_TAG}/{asset}", dst)
        if dst_name != "LICENSE":
            dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"完成: {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
