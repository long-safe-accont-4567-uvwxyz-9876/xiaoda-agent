"""TDD 测试：市场安装 URL 域名白名单 + 强制 SHA256（VULN-22）。"""
import pytest

from market.url_policy import is_allowed_download_url, require_sha256


@pytest.mark.parametrize("url", [
    "http://evil.com/x.zip",
    "https://attacker.net/payload.tar.gz",
    "file:///etc/passwd",
    "gopher://localhost/x",
    "http://10.0.0.1/x.zip",
    "http://localhost/x.zip",
    "",
    "not-a-url",
])
def test_reject_non_allowlisted_urls(url: str):
    assert is_allowed_download_url(url) is False, f"应拒绝: {url!r}"


@pytest.mark.parametrize("url", [
    "https://www.modelscope.cn/x.zip",
    "https://modelscope.cn/x.zip",
    "https://a.modelscope.cn/x.zip",
    "https://www.mcp-cn.com/x.zip",
    "https://github.com/org/repo/archive/refs/heads/main.zip",
    "https://raw.githubusercontent.com/org/repo/main/x.py",
    "https://gitee.com/org/repo/repository/archive/main.zip",
])
def test_allow_trusted_domains(url: str):
    assert is_allowed_download_url(url) is True, f"应放行: {url!r}"


def test_require_sha256_rejects_missing():
    with pytest.raises(ValueError):
        require_sha256("")


def test_require_sha256_rejects_invalid_format():
    with pytest.raises(ValueError):
        require_sha256("abc")
    with pytest.raises(ValueError):
        require_sha256("z" * 64)  # 非十六进制


def test_require_sha256_accepts_valid():
    # 64 位十六进制
    require_sha256("a" * 64)
    require_sha256("ABCDEF0123456789" * 4)  # 大小写均可
