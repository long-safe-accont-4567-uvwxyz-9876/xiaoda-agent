"""沙箱配置 — 借鉴 Claude Agent SDK 的 SandboxSettings 设计

提供框架级的网络域名白名单/黑名单和 SSRF 防护配置。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from loguru import logger


@dataclass
class SandboxNetworkConfig:
    """网络沙箱配置"""
    allowed_domains: list[str] = field(default_factory=list)
    denied_domains: list[str] = field(default_factory=list)
    # 内网 IP 阻止（SSRF 防护）
    block_private_ips: bool = True
    # 允许的端口
    allowed_ports: list[int] = field(default_factory=lambda: [80, 443])


@dataclass
class SandboxSettings:
    """沙箱设置 — 框架级安全配置"""
    network: SandboxNetworkConfig = field(default_factory=SandboxNetworkConfig)
    # 文件路径白名单
    allowed_base_dirs: list[str] = field(default_factory=list)
    # 敏感路径黑名单
    sensitive_paths: list[str] = field(default_factory=list)


# ── 项目根目录（用于默认沙箱配置）──
import sys
import tempfile
_PROJECT_ROOT = str(Path(__file__).parent)
# 跨平台临时目录
_TEMP_DIR = tempfile.gettempdir()
# 用户数据目录（frozen 模式下为 ~/.ai-agent，开发模式下为项目根）
try:
    from config import DATA_DIR, WORKSPACE_DIR, FILE_DIR, MEDIA_DIR
    _USER_DATA_DIRS = [str(DATA_DIR), str(WORKSPACE_DIR), str(FILE_DIR), str(MEDIA_DIR)]
except ImportError:
    _USER_DATA_DIRS = []
# KIOXIA 外置存储（Linux 特定路径，Windows 下不存在）
_KIOXIA_DATA = "/mnt/kioxia" if Path("/mnt/kioxia").exists() else ""

# ── 默认沙箱配置（安全加固）──────────────────────────

DEFAULT_SANDBOX = SandboxSettings(
    network=SandboxNetworkConfig(
        allowed_domains=[],  # 空白名单 = 不额外限制域名（黑名单优先）
        denied_domains=[
            "localhost", "127.0.0.1", "0.0.0.0",
            "169.254.169.254",  # 云元数据端点
            "10.*", "172.16.*", "172.17.*", "172.18.*", "172.19.*",
            "172.20.*", "172.21.*", "172.22.*", "172.23.*",
            "172.24.*", "172.25.*", "172.26.*", "172.27.*",
            "172.28.*", "172.29.*", "172.30.*", "172.31.*",
            "192.168.*",
            "::1", "fe80::*", "fc00::*", "fd00::*",
        ],
        block_private_ips=True,
        allowed_ports=[80, 443],
    ),
    allowed_base_dirs=[
        _PROJECT_ROOT,
        _TEMP_DIR,
    ] + _USER_DATA_DIRS + ([_KIOXIA_DATA] if _KIOXIA_DATA else []),
    sensitive_paths=[
        # Linux 特定
        "/etc/passwd", "/etc/shadow", "/etc/ssh",
        # Windows 特定
        "C:\\Windows\\System32\\config",
        # 跨平台
        "~/.ssh", ".env", "credentials", "credentials.json",
        "config/secrets", ".git",
    ],
)

# legacy 模式（旧行为：全空配置，用于回退）
LEGACY_SANDBOX = SandboxSettings(
    network=SandboxNetworkConfig(
        allowed_domains=[],
        denied_domains=[],
        block_private_ips=False,
        allowed_ports=[],
    ),
    allowed_base_dirs=[],
    sensitive_paths=[],
)


def _get_sandbox_profile() -> str:
    """读取沙箱配置文件：strict（默认）或 legacy"""
    return os.getenv("SANDBOX_PROFILE", "strict").strip().lower()


def get_default_sandbox() -> SandboxSettings:
    """获取当前沙箱配置（根据 SANDBOX_PROFILE 环境变量）"""
    profile = _get_sandbox_profile()
    if profile == "legacy":
        logger.warning("sandbox.legacy_mode", msg="沙箱使用 legacy 配置（不限制），仅建议调试使用")
        return LEGACY_SANDBOX
    return DEFAULT_SANDBOX


def check_domain_allowed(url: str, sandbox: SandboxSettings | None = None) -> tuple[bool, str]:
    """检查 URL 的域名是否被沙箱允许

    Returns:
        (allowed, reason) 元组
    """
    import urllib.parse
    sandbox = sandbox or DEFAULT_SANDBOX
    net_config = sandbox.network

    try:
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname or ""
        port = parsed.port
    except Exception:
        return False, "无效的 URL"

    # 检查端口
    if port and net_config.allowed_ports and port not in net_config.allowed_ports:
        return False, f"端口 {port} 不在允许列表中"

    # 检查黑名单（优先）
    for denied in net_config.denied_domains:
        if _domain_matches(hostname, denied):
            return False, f"域名 {hostname} 在黑名单中"

    # 如果有白名单，检查是否匹配
    if net_config.allowed_domains:
        matched = False
        for allowed in net_config.allowed_domains:
            if _domain_matches(hostname, allowed):
                matched = True
                break
        if not matched:
            return False, f"域名 {hostname} 不在白名单中"

    return True, ""


def _domain_matches(hostname: str, pattern: str) -> bool:
    """检查域名是否匹配模式（支持通配符 *）"""
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return hostname == suffix or hostname.endswith("." + suffix)
    return hostname == pattern
