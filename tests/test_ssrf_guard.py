"""tests/test_ssrf_guard.py — SSRF v2 防护 5 步法 + DNS Pinning 测试

覆盖验收标准:
- 私有 IP 全拒绝 (10/172.16/192.168/127/169.254)
- file://, gopher:// 等非 HTTP 协议拒绝
- DNS Pinning 防止 TOCTOU
- metadata endpoint 拒绝
- 白名单可配置
"""
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import ipaddress

from security.ssrf_guard import (
    _PIN_CACHE,
    check_ip,
    get_pinned_ip,
    validate_url,
)


def _make_getaddrinfo(ip_list):
    """构造 getaddrinfo 的 mock: 忽略入参, 返回指定 IP 列表的 addrinfo"""

    def _mock(host, port, *args, **kwargs):
        infos = []
        for ip in ip_list:
            addr = ipaddress.ip_address(ip)
            family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
            sockaddr = (ip, port) if family == socket.AF_INET else (ip, port, 0, 0)
            infos.append((family, socket.SOCK_STREAM, 0, "", sockaddr))
        return infos

    return _mock


class TestSSRFGuard(unittest.TestCase):
    """SSRF 5 步法防护测试"""

    def setUp(self):
        _PIN_CACHE.clear()

    # ── 公网放行 ──

    def test_allow_https_public(self):
        """公网 https + 公网 IP 通过"""
        with patch("security.ssrf_guard.socket.getaddrinfo",
                   _make_getaddrinfo(["93.184.216.34"])):
            ok, reason = validate_url("https://example.com/")
        self.assertTrue(ok, f"应放行: {reason}")
        self.assertEqual(reason, "")

    def test_allow_http_public(self):
        """公网 http 也通过"""
        with patch("security.ssrf_guard.socket.getaddrinfo",
                   _make_getaddrinfo(["1.1.1.1"])):
            ok, _ = validate_url("http://example.com/")
        self.assertTrue(ok)

    # ── localhost 拒绝 ──

    def test_reject_localhost(self):
        """localhost 直接拒绝 (危险主机名黑名单)"""
        ok, reason = validate_url("http://localhost/admin")
        self.assertFalse(ok)
        self.assertIn("黑名单", reason)

    def test_reject_localhost_variants(self):
        """localhost 变体均拒绝"""
        for host in ["localhost", "localhost.localdomain", "0.0.0.0"]:
            with self.subTest(host=host):
                ok, _ = validate_url(f"http://{host}/x")
                self.assertFalse(ok, f"{host} 应被拒绝")

    # ── 私有 IP 拒绝 ──

    def test_reject_private_ip(self):
        """私有 IP 网段全拒绝 (10/172.16/192.168/127)"""
        for ip in ["10.0.0.1", "10.255.255.255",
                   "172.16.0.1", "172.31.255.255",
                   "192.168.1.1", "192.168.0.0",
                   "127.0.0.1", "127.1.2.3"]:
            with self.subTest(ip=ip):
                ok, reason = validate_url(f"http://{ip}/x")
                self.assertFalse(ok, f"{ip} 应被拒绝, 得到: {reason}")

    def test_reject_private_ip_direct_check(self):
        """check_ip 直接校验私有网段"""
        for ip in ["10.0.0.1", "172.16.0.1", "192.168.1.1", "127.0.0.1"]:
            with self.subTest(ip=ip):
                ok, _ = check_ip(ip)
                self.assertFalse(ok, f"{ip} 应判定为危险")

    def test_check_ip_public_safe(self):
        """公网 IP check_ip 返回安全"""
        ok, _ = check_ip("8.8.8.8")
        self.assertTrue(ok)
        ok, _ = check_ip("1.1.1.1")
        self.assertTrue(ok)

    def test_check_ip_invalid(self):
        """无效 IP 字符串判定为危险"""
        ok, _ = check_ip("not-an-ip")
        self.assertFalse(ok)

    # ── metadata endpoint 拒绝 ──

    def test_reject_metadata_endpoint(self):
        """169.254.169.254 云元数据端点拒绝"""
        ok, reason = validate_url("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(ok)
        # 黑名单命中 或 169.254 网段命中
        self.assertTrue("黑名单" in reason or "169.254" in reason,
                        f"原因应涉及 metadata/链路本地: {reason}")

    def test_reject_metadata_hostnames(self):
        """云元数据主机名均拒绝"""
        for host in ["metadata.google.internal", "metadata", "metadata.azure.com"]:
            with self.subTest(host=host):
                ok, _ = validate_url(f"http://{host}/computeMetadata/v1/")
                self.assertFalse(ok, f"{host} 应被拒绝")

    # ── 非 HTTP 协议拒绝 ──

    def test_reject_non_http_protocol(self):
        """非 http/https 协议拒绝 (file/gopher/ftp/dict/ldap)"""
        for proto, url in [
            ("file", "file:///etc/passwd"),
            ("gopher", "gopher://localhost/x"),
            ("ftp", "ftp://127.0.0.1/file"),
            ("dict", "dict://localhost:11211/stat"),
            ("ldap", "ldap://localhost/dc=x"),
        ]:
            with self.subTest(proto=proto):
                ok, reason = validate_url(url)
                self.assertFalse(ok, f"{proto} 应被拒绝")
                self.assertIn("协议", reason)

    # ── DNS Pinning ──

    def test_dns_pinning(self):
        """DNS Pinning: 解析后 IP 锁定, get_pinned_ip 返回相同 IP"""
        with patch("security.ssrf_guard.socket.getaddrinfo",
                   _make_getaddrinfo(["93.184.216.34"])):
            ok, _ = validate_url("https://example.com/")
            self.assertTrue(ok)
            pinned = get_pinned_ip("https://example.com/")
        self.assertEqual(pinned, "93.184.216.34")

    def test_dns_pinning_uses_first_ip(self):
        """多 A 记录时锁定首个 IP"""
        with patch("security.ssrf_guard.socket.getaddrinfo",
                   _make_getaddrinfo(["93.184.216.34", "93.184.216.35"])):
            validate_url("https://example.com/")
            pinned = get_pinned_ip("https://example.com/")
        self.assertEqual(pinned, "93.184.216.34")

    def test_dns_pinning_rejects_unsafe(self):
        """校验失败时 get_pinned_ip 返回 None"""
        pinned = get_pinned_ip("http://10.0.0.1/x")
        self.assertIsNone(pinned)

    def test_dns_pinning_prevents_toctou(self):
        """DNS Pinning 防 TOCTOU: 第二次解析被篡改, 仍返回已锁定 IP"""
        url = "https://example.com/"
        with patch("security.ssrf_guard.socket.getaddrinfo",
                   _make_getaddrinfo(["93.184.216.34"])):
            validate_url(url)
        # 模拟攻击: 同主机名 DNS 被篡改为内网 IP
        with patch("security.ssrf_guard.socket.getaddrinfo",
                   _make_getaddrinfo(["10.0.0.1"])):
            pinned = get_pinned_ip(url)
        # 应返回锁定的公网 IP, 而非被篡改的内网 IP
        self.assertEqual(pinned, "93.184.216.34")

    # ── 白名单 ──

    def test_whitelist(self):
        """白名单主机放行 (即使无法 DNS 解析也放行)"""
        with patch.dict(os.environ, {"SSRF_ALLOW_HOSTS": "internal.svc,trusted.local"}):
            ok, reason = validate_url("http://internal.svc/health")
            self.assertTrue(ok, f"白名单主机应放行: {reason}")
            self.assertIn("白名单", reason)

    def test_whitelist_case_insensitive(self):
        """白名单匹配大小写不敏感"""
        with patch.dict(os.environ, {"SSRF_ALLOW_HOSTS": "Trusted.Local"}):
            ok, _ = validate_url("http://TRUSTED.LOCAL/x")
            self.assertTrue(ok)

    def test_whitelist_still_rejects_non_http(self):
        """白名单主机仍要求 http/https 协议"""
        with patch.dict(os.environ, {"SSRF_ALLOW_HOSTS": "internal.svc"}):
            ok, reason = validate_url("file://internal.svc/etc/passwd")
            self.assertFalse(ok)
            self.assertIn("协议", reason)

    # ── IPv6 私有地址拒绝 ──

    def test_reject_ipv6_private(self):
        """IPv6 私有/回环/链路本地地址拒绝 (::1, fe80::, fc00::)"""
        for ip in ["::1", "fe80::1", "fc00::1", "fd00::1", "fe80::abcd"]:
            with self.subTest(ip=ip):
                ok, _ = check_ip(ip)
                self.assertFalse(ok, f"{ip} 应判定为危险")
                # URL 形式也校验 ([ipv6])
                ok2, _ = validate_url(f"http://[{ip}]/")
                self.assertFalse(ok2, f"http://[{ip}]/ 应被拒绝")

    def test_reject_ipv6_multicast(self):
        """IPv6 多播地址拒绝"""
        ok, _ = check_ip("ff02::1")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
