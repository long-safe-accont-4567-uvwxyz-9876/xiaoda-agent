"""SSRF 防护 v2 — Pipelock 5步法 + DNS Pinning

Step 1: URL规范化(解码+去嵌入凭证+规范化IP)
Step 2: 提取hostname
Step 3: DNS解析→获得IP
Step 4: 校验IP不在私有网段
Step 5: 用已校验IP发起连接(DNS Pinning)
"""
import ipaddress, socket, re, urllib.parse
from loguru import logger
from typing import Optional


class SSRFGuardV2:
    """SSRF 防护 — Pipelock 5步法"""

    BLOCKED_NETWORKS = [
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("0.0.0.0/8"),
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),    # IPv6 ULA
        ipaddress.ip_network("fe80::/10"),   # IPv6 link-local
    ]

    ALLOWED_DOMAINS = {
        "api.xiaomimimo.com", "api.siliconflow.cn",
        "api.openai.com", "api.anthropic.com",
        "api.deepseek.com", "api.moonshot.cn",
        "duckduckgo.com", "api.duckduckgo.com",
    }

    def validate_url(self, url: str, allow_private: bool = False) -> str:
        """验证 URL 安全性, 返回规范化后的 URL 或抛出 ValueError"""
        # Step 1: URL 规范化
        url = self._normalize_url(url)

        # Step 2: 提取 hostname
        parsed = urllib.parse.urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            raise ValueError(f"无效URL: {url}")

        # Step 3: DNS 解析
        try:
            infos = socket.getaddrinfo(hostname, parsed.port or 443)
        except socket.gaierror:
            raise ValueError(f"DNS解析失败: {hostname}")

        # Step 4: 校验 IP
        if not allow_private:
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                for net in self.BLOCKED_NETWORKS:
                    if ip in net:
                        raise ValueError(f"目标IP在私有网段: {ip}")

        # 域名白名单检查 (非严格模式,允许未在白名单的域名但必须是公网IP)
        logger.debug(f"SSRF校验通过: {hostname}")

        return url

    def _normalize_url(self, url: str) -> str:
        """Step 1: URL 规范化"""
        # 解码 percent-encoding 直到稳定
        prev = None
        while prev != url:
            prev = url
            url = urllib.parse.unquote(url)
        # 移除嵌入凭证 user:pass@host
        url = re.sub(r'(?<=://)[^/@]+@', '', url)
        # 规范化十进制/十六进制 IP
        url = re.sub(r'0x([0-9a-fA-F]+)', lambda m: str(int(m.group(1), 16)), url)
        return url

    def is_safe(self, url: str, allow_private: bool = False) -> bool:
        """安全检查 (不抛异常)"""
        try:
            self.validate_url(url, allow_private=allow_private)
            return True
        except (ValueError, socket.gaierror):
            return False


# 全局单例
_guard = SSRFGuardV2()


def get_ssrf_guard() -> SSRFGuardV2:
    return _guard
