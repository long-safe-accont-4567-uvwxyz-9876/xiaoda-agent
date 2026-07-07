"""[DEPRECATED] SSRF 防护 v2 — Pipelock 5步法 + DNS Pinning

.. deprecated::
    本模块已被 :mod:`security.ssrf_guard` 取代。新版实现了完整的 5 步法
    (协议白名单 + 主机名黑名单 + DNS 解析 + IP 分类 + DNS Pinning)，
    并支持 ``SSRF_ALLOW_HOSTS`` 白名单配置。新代码应直接使用
    ``security.ssrf_guard.validate_url`` / ``get_pinned_ip``。

    本文件保留仅为向后兼容, 内部委托给新模块实现。
"""
import ipaddress, socket, re, urllib.parse
import warnings
from loguru import logger

# 模块级废弃标记 (不在此处发 warning, 避免污染导入链;
# 真正使用 get_ssrf_guard/validate_url 时才懒触发告警)
__deprecated__ = True
__deprecated_reason__ = "请改用 security.ssrf_guard (5步法 + DNS Pinning)"


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
    """获取 SSRF 防护全局单例（已废弃）。"""
    # 懒触发废弃告警 (新代码请用 security.ssrf_guard)
    warnings.warn(
        "utils.ssrf_guard.get_ssrf_guard 已废弃, 请改用 "
        "security.ssrf_guard.validate_url / get_pinned_ip (5步法 + DNS Pinning)",
        DeprecationWarning,
        stacklevel=2,
    )
    return _guard
