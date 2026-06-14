import asyncio
import ipaddress
import os
import shutil
import socket
import tempfile
import time
import urllib.request
from pathlib import Path
from loguru import logger


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, validator):
        self._validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._validator(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _SSRFCheckHTTPHandler(urllib.request.HTTPHandler):
    """自定义 HTTP Handler：在连接建立后检查实际连接的远程 IP，防止 DNS Rebinding"""

    def http_open(self, req):
        return self._open_with_ip_check(req)

    def _open_with_ip_check(self, req):
        # 先执行正常请求
        response = super().http_open(req)
        # 检查实际连接的远程地址
        if response and hasattr(response, 'fp') and hasattr(response.fp, 'raw'):
            sock = getattr(response.fp.raw, '_sock', None)
            if sock:
                peer = sock.getpeername()
                if peer:
                    self._check_peer_ip(peer[0])
        return response

    @staticmethod
    def _check_peer_ip(ip_str: str):
        ip = ipaddress.ip_address(ip_str)
        if isinstance(ip, ipaddress.IPv4Address):
            for net in FileReceiver.PRIVATE_NETWORKS:
                if ip in net:
                    raise ValueError(f"SSRF 防护：实际连接到内网 IP {ip_str}")
        elif isinstance(ip, ipaddress.IPv6Address):
            for net in FileReceiver.PRIVATE_NETWORKS_V6:
                if ip in net:
                    raise ValueError(f"SSRF 防护：实际连接到内网 IP {ip_str}")


class _SSRFCheckHTTPSHandler(urllib.request.HTTPSHandler):
    """自定义 HTTPS Handler：在连接建立后检查实际连接的远程 IP，防止 DNS Rebinding"""

    def https_open(self, req):
        return self._open_with_ip_check(req)

    def _open_with_ip_check(self, req):
        response = super().https_open(req)
        if response and hasattr(response, 'fp') and hasattr(response.fp, 'raw'):
            sock = getattr(response.fp.raw, '_sock', None)
            if sock:
                peer = sock.getpeername()
                if peer:
                    _SSRFCheckHTTPHandler._check_peer_ip(peer[0])
        return response


class FileReceiver:
    MAX_FILE_SIZE = 20 * 1024 * 1024

    TYPE_MAP = {
        "images": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"],
        "documents": [".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx"],
        "other": [".txt", ".md", ".csv", ".json", ".log", ".zip", ".rar"],
    }

    TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log", ".py", ".js", ".html", ".css", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh", ".bat"}

    ALLOWED_DOMAINS = {
        "qq.com", "qq.com.cn", "qpic.cn", "myqcloud.com",
        "gtimg.cn", "qlogo.cn",
    }

    PRIVATE_NETWORKS = [
        ipaddress.ip_network("127.0.0.0/8"),
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
        ipaddress.ip_network("169.254.0.0/16"),
        ipaddress.ip_network("0.0.0.0/8"),
    ]

    PRIVATE_NETWORKS_V6 = [
        ipaddress.ip_network("::1/128"),
        ipaddress.ip_network("fc00::/7"),
        ipaddress.ip_network("fe80::/10"),
    ]

    def __init__(self, base_dir: Path):
        self._base = base_dir
        self._base.mkdir(parents=True, exist_ok=True)
        for sub in ("images", "documents", "other"):
            (self._base / sub).mkdir(exist_ok=True)

    async def _validate_url(self, url: str) -> bool:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        is_allowed_domain = False
        for domain in self.ALLOWED_DOMAINS:
            if hostname == domain or hostname.endswith("." + domain):
                is_allowed_domain = True
                break
        if not is_allowed_domain:
            logger.warning("file_receiver.blocked_domain", hostname=hostname)
            return False
        try:
            # 异步解析 DNS，避免阻塞事件循环
            resolved = await asyncio.to_thread(
                socket.getaddrinfo, hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
            for family, type_, proto, canonname, sockaddr in resolved:
                ip = ipaddress.ip_address(sockaddr[0])
                if isinstance(ip, ipaddress.IPv4Address):
                    for net in self.PRIVATE_NETWORKS:
                        if ip in net:
                            logger.warning("file_receiver.blocked_private_ip", hostname=hostname, ip=str(ip))
                            return False
                elif isinstance(ip, ipaddress.IPv6Address):
                    for net in self.PRIVATE_NETWORKS_V6:
                        if ip in net:
                            logger.warning("file_receiver.blocked_private_ip", hostname=hostname, ip=str(ip))
                            return False
        except socket.gaierror:
            return False
        return True

    def _validate_url_sync(self, url: str) -> bool:
        """同步版本的 URL 校验，供 _SafeRedirectHandler 在同步上下文中使用"""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        is_allowed_domain = False
        for domain in self.ALLOWED_DOMAINS:
            if hostname == domain or hostname.endswith("." + domain):
                is_allowed_domain = True
                break
        if not is_allowed_domain:
            return False
        try:
            resolved = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for family, type_, proto, canonname, sockaddr in resolved:
                ip = ipaddress.ip_address(sockaddr[0])
                if isinstance(ip, ipaddress.IPv4Address):
                    for net in self.PRIVATE_NETWORKS:
                        if ip in net:
                            return False
                elif isinstance(ip, ipaddress.IPv6Address):
                    for net in self.PRIVATE_NETWORKS_V6:
                        if ip in net:
                            return False
        except socket.gaierror:
            return False
        return True

    async def receive(self, attachment) -> dict:
        url = getattr(attachment, 'url', '')
        filename = getattr(attachment, 'filename', 'unknown')
        content_type = getattr(attachment, 'content_type', '')
        size = getattr(attachment, 'size', 0)

        if not url:
            return {"status": "no_url", "filename": filename}

        if size and size > self.MAX_FILE_SIZE:
            return {"status": "too_large", "filename": filename, "size": size}

        if not await self._validate_url(url):
            return {"status": "blocked_url", "filename": filename, "error": "URL未通过安全校验"}

        sub_dir = self._classify(filename, content_type)
        safe_name = self._safe_filename(filename)
        ts = int(time.time())
        save_name = f"{ts}_{safe_name}" if safe_name != "unknown" else f"file_{ts}"
        save_path = self._base / sub_dir / save_name

        try:
            def _download():
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                })
                # 使用带 SSRF 检查的 opener，在 HTTP 连接层面验证实际连接 IP，防止 DNS Rebinding
                opener = urllib.request.build_opener(
                    _SSRFCheckHTTPHandler,
                    _SSRFCheckHTTPSHandler,
                    _SafeRedirectHandler(self._validate_url_sync),
                )
                with opener.open(req, timeout=30) as resp:
                    tmp_fd, tmp_path = tempfile.mkstemp()
                    total_size = 0
                    try:
                        while True:
                            chunk = resp.read(65536)  # 64KB 分块读取
                            if not chunk:
                                break
                            total_size += len(chunk)
                            if total_size > self.MAX_FILE_SIZE:
                                os.close(tmp_fd)
                                os.unlink(tmp_path)
                                return None, total_size
                            os.write(tmp_fd, chunk)
                        os.close(tmp_fd)
                    except Exception:
                        os.close(tmp_fd)
                        os.unlink(tmp_path)
                        raise
                return tmp_path, total_size

            tmp_path, total_size = await asyncio.to_thread(_download)

            if tmp_path is None:
                return {"status": "too_large", "filename": filename, "size": total_size}

            shutil.move(tmp_path, save_path)

            text_preview = self._try_read_text(save_path)
            file_size = save_path.stat().st_size

            logger.info("file_receiver.saved", filename=filename, path=str(save_path), size=file_size)

            return {
                "status": "ok",
                "filename": filename,
                "save_path": str(save_path),
                "size": file_size,
                "content_type": content_type,
                "text_preview": text_preview,
            }
        except Exception as e:
            logger.warning("file_receiver.download_failed", error=str(e), filename=filename)
            return {"status": "error", "filename": filename, "error": str(e)[:100]}

    def _classify(self, filename: str, content_type: str) -> str:
        ext = Path(filename).suffix.lower() if filename else ""
        for category, exts in self.TYPE_MAP.items():
            if ext in exts:
                return category
        if content_type.startswith("image/"):
            return "images"
        if content_type.startswith("video/"):
            return "other"
        return "other"

    def _safe_filename(self, filename: str) -> str:
        if not filename or filename == "unknown":
            return "unknown"
        name = Path(filename).name
        name = "".join(c for c in name if c.isalnum() or c in "._-")
        if not name or name == "." or name == "..":
            return "unknown"
        if name.startswith("."):
            name = "_" + name[1:]
        if len(name) > 128:
            stem = name[:120]
            ext = Path(name).suffix
            name = stem + ext
        return name

    def _try_read_text(self, path: Path) -> str:
        if path.suffix.lower() in self.TEXT_EXTENSIONS:
            try:
                return path.read_text(encoding='utf-8', errors='ignore')[:2000]
            except Exception as e:
                logger.warning(f"读取文本文件预览失败({path}): {e}")
        return ""
