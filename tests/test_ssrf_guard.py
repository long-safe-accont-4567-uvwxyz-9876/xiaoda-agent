"""tests/test_ssrf_guard.py — SSRF v2 防护 5 步法 + DNS Pinning 测试

覆盖验收标准:
- 私有 IP 全拒绝 (10/172.16/192.168/127/169.254)
- file://, gopher:// 等非 HTTP 协议拒绝
- DNS Pinning 防止 TOCTOU
- metadata endpoint 拒绝
- 白名单可配置
"""
import asyncio
import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import ipaddress

import httpx

from security.ssrf_guard import (
    _PIN_CACHE,
    _PIN_RESULT_CACHE,
    SecureAsyncTransport,
    check_ip,
    get_pinned_ip,
    resolve_and_pin,
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

    # ── 请求期解析绑定 (resolve_and_pin) ──

    def test_resolve_and_pin_pins_to_safe_ip(self):
        """请求期解析: 返回 (锁定 IP 的连接 URL, 原始 Host 头)"""
        with patch("security.ssrf_guard.socket.getaddrinfo",
                   _make_getaddrinfo(["93.184.216.34"])):
            connect_url, host = resolve_and_pin("https://example.com/v1")
        self.assertEqual(connect_url, "https://93.184.216.34/v1")
        self.assertEqual(host, "example.com")

    def test_resolve_and_pin_preserves_port(self):
        """请求期解析: 非默认端口保留在连接 URL 与 Host 头中"""
        with patch("security.ssrf_guard.socket.getaddrinfo",
                   _make_getaddrinfo(["93.184.216.34"])):
            connect_url, host = resolve_and_pin("http://example.com:8080/v1")
        self.assertEqual(connect_url, "http://93.184.216.34:8080/v1")
        self.assertEqual(host, "example.com:8080")

    def test_resolve_and_pin_rejects_request_time_dangerous(self):
        """请求期 DNS rebinding 到危险 IP 应被拦截"""
        with patch("security.ssrf_guard.socket.getaddrinfo",
                   _make_getaddrinfo(["10.0.0.1"])):
            with self.assertRaises(ValueError):
                resolve_and_pin("https://example.com/api")

    def test_resolve_and_pin_rejects_localhost(self):
        """请求期解析 localhost 直接拒绝"""
        with self.assertRaises(ValueError):
            resolve_and_pin("http://localhost:8000/api")

    def test_resolve_and_pin_whitelist_passthrough(self):
        """白名单主机请求期原样返回, 不替换主机名"""
        with patch.dict(os.environ, {"SSRF_ALLOW_HOSTS": "internal.svc"}):
            connect_url, host = resolve_and_pin("http://internal.svc/health")
        self.assertEqual(connect_url, "http://internal.svc/health")
        self.assertEqual(host, "")

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


class _StubTransport:
    """记录 handle_async_request 收到的 request，返回固定 Response。"""

    def __init__(self) -> None:
        self.requests: list = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, text="ok")


class TestSecureAsyncTransport(unittest.TestCase):
    """SecureAsyncTransport：TTL 缓存 + 线程池 DNS，不阻塞事件循环。"""

    def setUp(self):
        _PIN_RESULT_CACHE.clear()

    def _make_request(self, base_url: str) -> httpx.Request:
        """构造一个发往 base_url 路径的 httpx.Request。"""
        return httpx.Request("GET", f"{base_url}/chat")

    def test_secure_transport_pins_and_caches(self):
        """首次请求解析 DNS 并缓存，第二次命中缓存不重复解析。"""
        stub = _StubTransport()
        transport = SecureAsyncTransport("https://example.com", http_transport=stub)
        mock_dns = Mock(side_effect=_make_getaddrinfo(["93.184.216.34"]))
        with patch("security.ssrf_guard.socket.getaddrinfo", mock_dns):
            asyncio.run(transport.handle_async_request(self._make_request("https://example.com")))
            self.assertEqual(mock_dns.call_count, 1)
            # 第二次应命中缓存，不再次 DNS
            asyncio.run(transport.handle_async_request(self._make_request("https://example.com")))
            self.assertEqual(mock_dns.call_count, 1)
        # 两次请求都改写 netloc 为 pinned IP，并注入 Host 头
        self.assertEqual(len(stub.requests), 2)
        for req in stub.requests:
            self.assertEqual(req.url.host, "93.184.216.34")
            self.assertEqual(req.headers["host"], "example.com")

    def test_secure_transport_cache_expiry_re_resolves(self):
        """缓存过期后重新解析 DNS。"""
        stub = _StubTransport()
        transport = SecureAsyncTransport("https://example.com", http_transport=stub)
        mock_dns = Mock(side_effect=_make_getaddrinfo(["93.184.216.34"]))
        with patch("security.ssrf_guard.socket.getaddrinfo", mock_dns):
            asyncio.run(transport.handle_async_request(self._make_request("https://example.com")))
            self.assertEqual(mock_dns.call_count, 1)
        # 手动让缓存过期（倒拨 expires_at 时间戳）
        from security.ssrf_guard import _pin_cache_key
        key = _pin_cache_key("https://example.com")
        _PIN_RESULT_CACHE[key] = (
            "https://93.184.216.34", "example.com", -1.0,
        )
        mock_dns2 = Mock(side_effect=_make_getaddrinfo(["1.2.3.4"]))
        with patch("security.ssrf_guard.socket.getaddrinfo", mock_dns2):
            asyncio.run(transport.handle_async_request(self._make_request("https://example.com")))
            self.assertEqual(mock_dns2.call_count, 1)
        # 过期后用了新 IP
        self.assertEqual(stub.requests[-1].url.host, "1.2.3.4")

    def test_secure_transport_propagates_value_error(self):
        """DNS 解析到危险 IP 时 ValueError 正常传播。"""
        stub = _StubTransport()
        transport = SecureAsyncTransport("https://example.com", http_transport=stub)
        with patch("security.ssrf_guard.socket.getaddrinfo",
                   _make_getaddrinfo(["10.0.0.1"])):
            with self.assertRaises(ValueError):
                asyncio.run(transport.handle_async_request(
                    self._make_request("https://example.com")))
        # 校验失败不缓存
        self.assertEqual(len(_PIN_RESULT_CACHE), 0)
        self.assertEqual(len(stub.requests), 0)

    def test_secure_transport_whitelist_host_no_pin(self):
        """白名单主机 host 为空，请求原样透传不改写 netloc。"""
        stub = _StubTransport()
        transport = SecureAsyncTransport("http://internal.svc", http_transport=stub)
        with patch.dict(os.environ, {"SSRF_ALLOW_HOSTS": "internal.svc"}):
            asyncio.run(transport.handle_async_request(
                self._make_request("http://internal.svc")))
        self.assertEqual(len(stub.requests), 1)
        # netloc 未改写，仍指向原主机名（白名单 host=空串，不改写）
        self.assertEqual(stub.requests[0].url.host, "internal.svc")


if __name__ == "__main__":
    unittest.main()
