"""SSRF v2 防护 — 5 步法 + DNS Pinning

5 步法:
    1. URL 解析与协议白名单 (只允许 http/https, 拒绝 file://, gopher://, ftp:// 等)
    2. 主机名提取与禁止列表 (拒绝 localhost, 0.0.0.0, metadata.google.internal,
       169.254.169.254 等危险主机名)
    3. DNS 解析获取所有 IP (getaddrinfo, 可能返回多条 A/AAAA 记录)
    4. IP 分类检查 (私有/回环/链路本地/保留段全拒绝, 任一命中即拒绝)
    5. DNS Pinning: 解析后的 IP 锁定, 实际请求时使用该 IP, 防止 TOCTOU
       (DNS 解析后到实际请求前被篡改)

白名单: 环境变量 ``SSRF_ALLOW_HOSTS`` (逗号分隔) 可放行指定的内部受信主机名,
此类主机跳过 IP 检查 (仍必须是 http/https 协议)。
"""
import ipaddress
import os
import re
import socket
import urllib.parse
from typing import Optional

from loguru import logger


# ── Step 1: 协议白名单 ──
_ALLOWED_SCHEMES = {"http", "https"}

# ── Step 2: 危险主机名黑名单 (metadata endpoints, 内部服务名, 回环节点) ──
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "broadcasthost",
    "0.0.0.0",
    # 云元数据端点
    "metadata.google.internal",   # GCP
    "metadata",                   # GCP 别名
    "169.254.169.254",            # AWS / Azure / OpenStack 元数据
    "169.254.170.2",              # ECS 任务元数据 (v2)
    "metadata.azure.com",         # Azure 元数据
    # 容器编排内部服务
    "kubernetes.default.svc",
    "kubernetes.default.svc.cluster.local",
    "kubernetes.default",
    "openshift.default.svc",
    "docker.internal",
    "host.docker.internal",
}

# ── Step 4: 私有/保留 IP 网段 (IPv4 + IPv6) ──
# 使用标准库 ipaddress.ip_network 严格校验
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),        # 当前网络
    ipaddress.ip_network("10.0.0.0/8"),       # 私有 A
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT 运营商级 NAT
    ipaddress.ip_network("127.0.0.0/8"),      # 回环
    ipaddress.ip_network("169.254.0.0/16"),  # 链路本地 (含云元数据)
    ipaddress.ip_network("172.16.0.0/12"),   # 私有 B
    ipaddress.ip_network("192.0.0.0/24"),    # IETF 协议分配
    ipaddress.ip_network("192.0.2.0/24"),    # TEST-NET-1
    ipaddress.ip_network("192.88.99.0/24"),   # 6to4 中继任播
    ipaddress.ip_network("192.168.0.0/16"),  # 私有 C
    ipaddress.ip_network("198.18.0.0/15"),    # 网络基准测试
    ipaddress.ip_network("198.51.100.0/24"), # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),      # 多播
    ipaddress.ip_network("240.0.0.0/4"),      # 保留
    # IPv6
    ipaddress.ip_network("::1/128"),          # 回环
    ipaddress.ip_network("::/128"),           # 未指定
    ipaddress.ip_network("::ffff:0:0/96"),    # IPv4-mapped
    ipaddress.ip_network("64:ff9b::/96"),     # NAT64
    ipaddress.ip_network("fc00::/7"),         # ULA
    ipaddress.ip_network("fe80::/10"),         # 链路本地
    ipaddress.ip_network("ff00::/8"),         # 多播
]

# ── Step 5: DNS Pinning 短期缓存 ──
# hostname(小写) -> (锁定的 IP 字符串, 缓存时间戳)
# 缓存已通过 Step4 校验的 IP, 避免每次请求重新解析, 防止 TOCTOU
_PIN_CACHE: dict[str, tuple[str, float]] = {}
_PIN_CACHE_TTL = 60.0  # 锁定有效期 (秒)


def _load_whitelist() -> set[str]:
    """从环境变量 SSRF_ALLOW_HOSTS 加载白名单 (逗号分隔)"""
    raw = os.getenv("SSRF_ALLOW_HOSTS", "")
    return {h.strip().lower().rstrip(".") for h in raw.split(",") if h.strip()}


def check_ip(ip: str) -> tuple[bool, str]:
    """检查单个 IP 是否为私有/危险地址 (Step 4 的核心)

    Args:
        ip: IP 字符串 (IPv4 或 IPv6)

    Returns:
        (is_safe, reason) — is_safe=True 表示公网 IP, 安全;
        is_safe=False 表示命中危险网段, reason 给出命中的网段。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False, f"无效的 IP 地址: {ip}"

    for net in _BLOCKED_NETWORKS:
        if addr in net:
            return False, f"命中危险网段 {net}"
    return True, ""


def _is_hostname_blocked(hostname: str) -> Optional[str]:
    """Step 2: 检查 hostname 是否在危险主机名黑名单中"""
    if not hostname:
        return "空主机名"
    lower = hostname.lower().rstrip(".")
    if lower in _BLOCKED_HOSTNAMES:
        return f"主机名 {hostname} 在危险黑名单中"
    return None


def _normalize_url(url: str) -> str:
    """Step 1 预处理: 反复 percent-decode, 剥离嵌入凭证, 十六进制 IP 转十进制

    - 多层 percent-encoding 解码直到稳定 (防 %25252e 等绕过)
    - 移除嵌入凭证 user:pass@host
    - 0x 十六进制 IP → 十进制 (e.g. 0x7f000001 → 127.0.0.1)
    """
    prev = None
    while prev != url:
        prev = url
        url = urllib.parse.unquote(url)
    # 移除嵌入凭证 user:pass@host
    url = re.sub(r"(?<=://)[^/@]+@", "", url)
    # 十六进制 IP → 十进制
    url = re.sub(r"0x([0-9a-fA-F]+)", lambda m: str(int(m.group(1), 16)), url)
    return url


def _resolve_all_ips(hostname: str, port: int) -> list[str]:
    """Step 3: DNS 解析返回所有 A/AAAA 记录的 IP 字符串

    使用 getaddrinfo(AF_UNSPEC) 同时获取 IPv4/IPv6 记录,
    去重并保留顺序 (首个 IP 用于 DNS Pinning)。
    """
    infos = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip = sockaddr[0]
        # IPv6 去除 zone_id (e.g. fe80::1%eth0 → fe80::1)
        if "%" in ip:
            ip = ip.split("%", 1)[0]
        if ip not in ips:
            ips.append(ip)
    return ips


def validate_url(url: str) -> tuple[bool, str]:
    """5 步法验证 URL 安全性

    Args:
        url: 待校验的 URL

    Returns:
        (allowed, reason) — allowed=True 表示放行, reason 为空串;
        allowed=False 表示拒绝, reason 给出拒绝原因。
    """
    if not url or not isinstance(url, str):
        return False, "空 URL"

    # Step 1: URL 规范化 + 协议白名单
    try:
        normalized = _normalize_url(url)
        parsed = urllib.parse.urlparse(normalized)
    except Exception as e:
        return False, f"URL 解析失败: {e}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        return False, f"协议 {scheme!r} 不在白名单 (仅允许 http/https)"

    hostname = parsed.hostname
    if not hostname:
        return False, "缺少主机名"

    # 白名单放行: 内部受信服务跳过 IP 检查 (仍要求 http/https)
    whitelist = _load_whitelist()
    if hostname.lower().rstrip(".") in whitelist:
        logger.debug("ssrf.whitelisted host={}", hostname)
        return True, "白名单放行"

    # Step 2: 主机名黑名单检查
    blocked_reason = _is_hostname_blocked(hostname)
    if blocked_reason:
        logger.warning("ssrf.blocked_hostname host={} reason={}", hostname, blocked_reason)
        return False, blocked_reason

    # Step 3: DNS 解析获取所有 IP
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        ips = _resolve_all_ips(hostname, port)
    except socket.gaierror as e:
        return False, f"DNS 解析失败: {hostname} ({e})"

    if not ips:
        return False, f"DNS 无记录: {hostname}"

    # Step 4: IP 分类检查
    # 任一 IP 命中危险网段即拒绝 — 防 DNS rebinding 多记录攻击
    for ip in ips:
        ok, reason = check_ip(ip)
        if not ok:
            logger.warning("ssrf.blocked_ip host={} ip={} reason={}", hostname, ip, reason)
            return False, f"目标 {hostname} 解析到危险 IP {ip}: {reason}"

    # Step 5: DNS Pinning — 锁定首个 IP, 缓存供 get_pinned_ip 使用
    pinned_ip = ips[0]
    import time as _time
    _PIN_CACHE[hostname.lower()] = (pinned_ip, _time.monotonic())
    logger.debug("ssrf.passed host={} pinned_ip={} ips={}", hostname, pinned_ip, ips)
    return True, ""


def get_pinned_ip(url: str) -> Optional[str]:
    """获取 URL 主机名锁定的 IP (DNS Pinning, Step 5)

    若 ``validate_url`` 尚未执行过, 会先执行一次校验。
    用于实际发起请求时绑定该 IP, 防止 DNS 解析后到请求前被篡改 (TOCTOU)。

    Returns:
        锁定的 IP 字符串; 校验失败或无法解析时返回 None。
    """
    if not url:
        return None
    try:
        parsed = urllib.parse.urlparse(_normalize_url(url))
        hostname = (parsed.hostname or "").lower()
    except Exception:
        return None

    if not hostname:
        return None

    import time as _time
    cached = _PIN_CACHE.get(hostname)
    if cached:
        ip, ts = cached
        # 检查 TTL：过期则清除并重新解析
        if _time.monotonic() - ts < _PIN_CACHE_TTL:
            return ip
        else:
            _PIN_CACHE.pop(hostname, None)

    # 未缓存或已过期 → 重新校验并锁定
    ok, _ = validate_url(url)
    if not ok:
        return None
    cached = _PIN_CACHE.get(hostname)
    return cached[0] if cached else None


# ── 便捷封装 ──

def is_safe(url: str) -> bool:
    """安全检查 (不抛异常, 不返回原因)"""
    try:
        ok, _ = validate_url(url)
        return ok
    except Exception:
        return False


def enforce(url: str) -> str:
    """校验并返回规范化后的 URL; 不安全时抛出 ValueError

    便于在工具入口处一行调用: ``url = enforce(url)``
    """
    ok, reason = validate_url(url)
    if not ok:
        raise ValueError(f"SSRF 校验失败: {reason}")
    return _normalize_url(url)
