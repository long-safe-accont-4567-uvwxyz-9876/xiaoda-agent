"""市场安装 URL 安全策略 — 域名白名单 + 强制 SHA256。

VULN-22 修复：市场安装允许任意 URL 下载且 SHA256 可选，导致认证后任意代码执行 + SSRF。
本模块提供：
- 域名白名单校验（仅允许官方/受信下载源）
- 强制 SHA256 校验和（缺失即拒绝）
"""
from __future__ import annotations

from urllib.parse import urlparse

from loguru import logger

# 受信下载源域名白名单（子域名匹配，如 a.modelscope.cn 命中 modelscope.cn）
ALLOWED_DOWNLOAD_DOMAINS: frozenset[str] = frozenset({
    "modelscope.cn",
    "www.modelscope.cn",
    "mcp-cn.com",
    "www.mcp-cn.com",
    "github.com",
    "raw.githubusercontent.com",
    "gitee.com",
    "gitcode.com",
})


def is_allowed_download_url(url: str) -> bool:
    """校验下载 URL 是否在受信域名白名单内。

    Returns:
        True 放行；False 拒绝（协议非 http/https、无主机名、或域名不在白名单）。
    """
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except (ImportError, OSError, RuntimeError, ValueError):
        return False
    except Exception:
        logger.exception(".market.url_policy.is_allowed_download_url_unexpected")
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        return False
    for domain in ALLOWED_DOWNLOAD_DOMAINS:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    logger.warning("market.url_policy.blocked_domain", hostname=hostname)
    return False


def require_sha256(sha256: str) -> None:
    """强制要求提供 SHA256 校验和；缺失或格式非法时抛 ValueError。

    VULN-22：缺省仅告警放行 → 改为强制，缺失即拒绝安装。
    """
    if not sha256 or not isinstance(sha256, str):
        raise ValueError("必须提供 SHA256 校验和以验证文件完整性")
    sha = sha256.strip().lower()
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("SHA256 校验和格式非法（应为 64 位十六进制）")
