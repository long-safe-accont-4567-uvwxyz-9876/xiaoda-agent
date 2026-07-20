"""凭证保险库 - API Key 加密存储（机器绑定）

安全模型
--------
1. 加密密钥 = PBKDF2-HMAC-SHA256(机器身份, 固定盐) → 32 字节
2. 机器身份 = "用户名@主机名"（换机器自动失效，防 .env 被复制泄漏）
3. 流密码：密钥流 = HMAC-SHA256(密钥, nonce || counter) 拼接，与明文异或
4. 完整性：tag = HMAC-SHA256(密钥, nonce || 密文)，附在密文末尾
5. 输出格式（Linux/Unix）：enc:v1:<base64url(nonce || 密文 || tag)>
6. 输出格式（Windows + pywin32）：enc:v2:dpapi:<base64url(DPAPI 密文)>
7. 向后兼容：非 enc:v1: / enc:v2:dpapi: 前缀的值视为明文直接返回

Windows 平台优先使用 DPAPI（CryptProtectData / CryptUnprotectData），
凭证与当前 Windows 用户绑定；无 pywin32 时回退到 enc:v1: 方案并 log warning。

仅使用标准库（hashlib / hmac / base64 / secrets），不依赖 cryptography。
pywin32 仅在 Windows 上为可选依赖（见 pyproject.toml 的 [project.optional-dependencies] windows）。
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import os
import re
import secrets
import socket
import sys
from pathlib import Path


class DecryptionError(ValueError):
    """凭证解密失败（机器不匹配 / 标签验证失败 / 数据损坏）。"""

from loguru import logger
import contextlib

# ── Windows DPAPI 可选导入 ─────────────────────────────────────
# 仅在 Windows 上尝试导入 win32crypt；Linux 上 win32crypt = None，HAS_WIN32CRYPT = False。
# 始终定义 win32crypt 模块级变量（None 或实际模块），便于测试时 patch。
if sys.platform == "win32":
    try:
        import win32crypt  # type: ignore[import-not-found]
        HAS_WIN32CRYPT = True
    except ImportError:
        win32crypt = None  # type: ignore[assignment]
        HAS_WIN32CRYPT = False
else:
    win32crypt = None  # type: ignore[assignment]
    HAS_WIN32CRYPT = False

# ── 常量 ──────────────────────────────────────────────────────
# 盐：基础前缀 + 每次部署唯一随机后缀（首次启动时生成并持久化）
_SALT_BASE = b"xiaoda-agent-credential-vault-v2-"


def _resolve_salt_file() -> Path:
    """解析盐文件绝对路径。

    必须使用绝对路径，避免 CWD 变化（systemd/docker/frozen）时找不到盐文件
    导致已加密凭证永久无法解密。

    优先级：
    1. 环境变量 CREDENTIAL_SALT_FILE（显式指定）
    2. frozen 模式：~/.ai-agent/config/credential_salt.bin
    3. 开发模式：项目根目录 /config/credential_salt.bin
    """
    env_path = os.getenv("CREDENTIAL_SALT_FILE")
    if env_path:
        return Path(env_path).expanduser()
    if getattr(sys, "frozen", False):
        return Path.home() / ".ai-agent" / "config" / "credential_salt.bin"
    # 开发模式：项目根目录（credential_vault.py 的上两级）
    return Path(__file__).resolve().parent.parent / "config" / "credential_salt.bin"


_SALT_FILE = _resolve_salt_file()

# 迁移：如果新路径不存在但旧相对路径下有盐文件，复制过来避免凭证失效
try:
    if not _SALT_FILE.exists():
        _legacy_salt = Path("config") / "credential_salt.bin"
        if _legacy_salt.exists():
            _SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
            _SALT_FILE.write_bytes(_legacy_salt.read_bytes())
            with contextlib.suppress(OSError):
                _SALT_FILE.chmod(0o600)
            logger.info("credential_vault.salt_migrated", src=str(_legacy_salt), dst=str(_SALT_FILE))
except Exception as e:
    logger.debug("credential_vault.salt_migrate_failed", error=str(e))
# PBKDF2 迭代次数（增大可减缓暴力破解）
_PBKDF2_ITERATIONS = 200_000
# 随机 nonce 长度（字节）
_NONCE_LEN = 16
# HMAC 认证标签长度（字节，SHA256 输出 32 字节）
_TAG_LEN = 32
# 加密值前缀
_PREFIX = "enc:v1:"
# DPAPI 加密值前缀（Windows + pywin32）
_DPAPI_PREFIX = "enc:v2:dpapi:"
# 密钥派生输出长度
_KEY_LEN = 32

# 敏感环境变量名后缀（匹配则视为需要加密的凭证）
_SENSITIVE_SUFFIXES = ("_API_KEY", "_API_SECRET", "_TOKEN")

# .env 行解析正则：[空白][export ]KEY[空白]=[空白]剩余
_ENV_LINE_PATTERN = re.compile(
    r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*)$"
)

# 合法 base64url 字符集（用于 is_encrypted 校验）
_B64URL_PATTERN = re.compile(r"[A-Za-z0-9_\-]+={0,2}")

# 模块级标记：Windows + 无 pywin32 时仅警告一次，避免日志刷屏
_DPAPI_WARNED = False


# ── 机器身份与密钥派生 ────────────────────────────────────────
def _machine_identity() -> str:
    """获取机器身份字符串：用户名@主机名

    换机器或换用户都会导致身份变化，从而无法解密原机器加密的凭证。
    """
    try:
        user = getpass.getuser()
    except Exception:
        user = os.getenv("USER", "") or os.getenv("USERNAME", "") or "unknown-user"
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown-host"
    return f"{user}@{host}"


def _get_salt() -> bytes:
    """获取盐：首次启动生成 32 字节随机盐并持久化，后续复用。

    同一部署实例内盐稳定（可正常解密），不同实例盐不同（防跨实例碰撞）。
    """
    if _SALT_FILE.exists():
        try:
            data = _SALT_FILE.read_bytes()
            if len(data) == 32:
                return _SALT_BASE + data
        except OSError:
            logger.warning("credential_vault.salt_read_failed", exc_info=True)
    # 首次：生成随机盐并持久化
    random_salt = os.urandom(32)
    try:
        _SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SALT_FILE.write_bytes(random_salt)
        # 设文件权限仅 owner 可读写（Unix）
        with contextlib.suppress(OSError):
            _SALT_FILE.chmod(0o600)
    except OSError as e:
        logger.warning("credential_vault.salt_persist_failed", error=str(e))
    return _SALT_BASE + random_salt


def _derive_key() -> bytes:
    """从机器身份派生 32 字节密钥（PBKDF2-HMAC-SHA256）"""
    identity = _machine_identity()
    return hashlib.pbkdf2_hmac(
        "sha256",
        identity.encode("utf-8"),
        _get_salt(),
        _PBKDF2_ITERATIONS,
        dklen=_KEY_LEN,
    )


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """生成密钥流：HMAC-SHA256(key, nonce || counter_be64) 拼接为指定长度

    类似 CTR 模式：每个 32 字节块由 HMAC(key, nonce || counter) 产生，
    counter 从 0 递增。
    """
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(
            key,
            nonce + counter.to_bytes(8, "big"),
            hashlib.sha256,
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


# ── Windows DPAPI 内部辅助函数 ────────────────────────────────
def _dpapi_encrypt(plaintext: str) -> bytes:
    """使用 DPAPI CryptProtectData 加密。返回加密后的 bytes。

    凭证与当前 Windows 用户绑定，换用户无法解密。
    仅在 sys.platform == 'win32' 且 HAS_WIN32CRYPT = True 时调用。
    """
    # CryptProtectData(dataIn, name, optionalEntropy, reserved, promptStruct, flags)
    # dataIn: 要加密的 bytes；其余参数 None/0 表示无额外熵、无提示、默认标志
    return win32crypt.CryptProtectData(  # type: ignore[union-attr]
        plaintext.encode("utf-8"), None, None, None, None, 0
    )


def _dpapi_decrypt(ciphertext: bytes) -> str:
    """使用 DPAPI CryptUnprotectData 解密。返回原始 plaintext。

    仅在 sys.platform == 'win32' 且 HAS_WIN32CRYPT = True 时调用。
    CryptUnprotectData 返回 (description, data, entropy) 三元组。
    """
    # CryptUnprotectData(dataIn, optionalEntropy, reserved, promptStruct, flags)
    _desc, data, _entropy = win32crypt.CryptUnprotectData(  # type: ignore[union-attr]
        ciphertext, None, None, None, 0
    )
    return data.decode("utf-8")


def _is_dpapi_encrypted(value: str) -> bool:
    """判断值是否使用 DPAPI 加密（enc:v2:dpapi: 前缀 + 合法 base64url 主体）"""
    if not isinstance(value, str) or not value:
        return False
    if not value.startswith(_DPAPI_PREFIX):
        return False
    encoded = value[len(_DPAPI_PREFIX):]
    if not encoded:
        return False
    return _B64URL_PATTERN.fullmatch(encoded) is not None


# ── 公共 API ──────────────────────────────────────────────────
def is_encrypted(value: str) -> bool:
    """判断值是否已加密（enc:v1: 前缀 + 合法 base64url 主体）

    注意：此函数仅检测 enc:v1: 格式。DPAPI 加密的值（enc:v2:dpapi:）
    请使用 _is_dpapi_encrypted 检测，或两者都检查。
    """
    if not isinstance(value, str) or not value:
        return False
    if not value.startswith(_PREFIX):
        return False
    encoded = value[len(_PREFIX):]
    if not encoded:
        return False
    return _B64URL_PATTERN.fullmatch(encoded) is not None


def encrypt(plaintext: str) -> str:
    """加密明文，返回 enc:v1: 或 enc:v2:dpapi: 格式密文

    平台路由：
      - Windows + pywin32 可用：优先走 DPAPI（enc:v2:dpapi: 前缀）
      - Windows + pywin32 不可用：回退到 enc:v1: 并 logger.warning（仅首次）
      - Linux/Unix：走 enc:v1:（行为不变）
      - DPAPI 加密失败：fallback 到 enc:v1:

    幂等：传入已加密的值（v1 或 v2）会原样返回。
    """
    if not plaintext:
        return plaintext
    # 幂等：已加密（v1 或 v2）直接返回
    if is_encrypted(plaintext) or _is_dpapi_encrypted(plaintext):
        return plaintext

    # Windows + DPAPI 可用：优先走 DPAPI 路径
    if sys.platform == "win32" and HAS_WIN32CRYPT:
        try:
            ct_bytes = _dpapi_encrypt(plaintext)
            encoded = base64.urlsafe_b64encode(ct_bytes).decode("ascii")
            return f"{_DPAPI_PREFIX}{encoded}"
        except Exception as e:
            logger.warning(f"DPAPI 加密失败，回退到 enc:v1: 方案：{e}")
            # 落到下面的 enc:v1: 路径

    # Windows + 无 pywin32：首次警告（避免日志刷屏）
    global _DPAPI_WARNED
    if sys.platform == "win32" and not HAS_WIN32CRYPT and not _DPAPI_WARNED:
        logger.warning(
            "pywin32 不可用，Windows 凭证加密回退到 enc:v1: 方案"
            "（建议安装 pywin32 以启用 DPAPI 加密：pip install 'xiaoda-agent[windows]'）"
        )
        _DPAPI_WARNED = True

    # enc:v1: 路径（Linux/Unix 默认；Windows fallback）
    key = _derive_key()
    nonce = secrets.token_bytes(_NONCE_LEN)
    pt_bytes = plaintext.encode("utf-8")
    ks = _keystream(key, nonce, len(pt_bytes))
    ciphertext = bytes(a ^ b for a, b in zip(pt_bytes, ks, strict=False))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()

    payload = nonce + ciphertext + tag
    encoded = base64.urlsafe_b64encode(payload).decode("ascii")
    return f"{_PREFIX}{encoded}"


def _decrypt_v1(ciphertext: str) -> str:
    """enc:v1: 格式解密（内部辅助，逻辑与原 decrypt 一致，未改变）

    调用前需确保 ciphertext 以 enc:v1: 开头且通过 is_encrypted 校验。
    """
    encoded = ciphertext[len(_PREFIX):]
    try:
        payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except Exception as e:
        logger.warning(f"凭证解密失败：base64 解码错误：{e}")
        raise DecryptionError(f"base64 解码错误：{e}") from e

    if len(payload) < _NONCE_LEN + _TAG_LEN:
        logger.warning(
            f"凭证解密失败：密文长度不足（{len(payload)} < {_NONCE_LEN + _TAG_LEN}）"
        )
        raise DecryptionError(f"密文长度不足（{len(payload)} < {_NONCE_LEN + _TAG_LEN}）")

    nonce = payload[:_NONCE_LEN]
    tag = payload[-_TAG_LEN:]
    ciphertext_body = payload[_NONCE_LEN:-_TAG_LEN]

    key = _derive_key()
    expected_tag = hmac.new(key, nonce + ciphertext_body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        # 机器身份不匹配或数据被篡改
        logger.warning("凭证解密失败：HMAC 标签验证失败（机器不匹配或数据损坏）")
        raise DecryptionError("HMAC 标签验证失败（机器不匹配或数据损坏）")

    ks = _keystream(key, nonce, len(ciphertext_body))
    pt_bytes = bytes(a ^ b for a, b in zip(ciphertext_body, ks, strict=False))
    try:
        return pt_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        logger.warning(f"凭证解密失败：UTF-8 解码错误：{e}")
        raise DecryptionError(f"UTF-8 解码错误：{e}") from e


def decrypt(ciphertext: str) -> str:
    """解密 enc:v1: 或 enc:v2:dpapi: 格式密文

    - 非 enc:v1: / enc:v2:dpapi: 前缀的值视为明文直接返回（向后兼容）
    - enc:v2:dpapi: 优先走 DPAPI 解密；失败时 fallback 到 enc:v1: 解密
    - enc:v1: 走原有解密路径（完全不变）
    - 解密失败（机器不匹配 / 标签验证失败 / 数据损坏）抛出 DecryptionError
    """
    if not ciphertext:
        return ""

    # DPAPI 加密的值：先尝试 DPAPI 解密，失败则 fallback 到 v1
    if _is_dpapi_encrypted(ciphertext):
        encoded = ciphertext[len(_DPAPI_PREFIX):]
        ct_bytes: bytes | None = None
        try:
            ct_bytes = base64.urlsafe_b64decode(encoded.encode("ascii"))
        except Exception as e:
            logger.warning(f"DPAPI 凭证 base64 解码失败，尝试 enc:v1: 回退：{e}")

        if ct_bytes is not None and HAS_WIN32CRYPT:
            try:
                return _dpapi_decrypt(ct_bytes)
            except Exception as e:
                logger.warning(f"DPAPI 解密失败，尝试 enc:v1: 回退：{e}")
                # 落到下面的 v1 fallback

        # Fallback：尝试将 payload 当作 v1 密文解密
        # （模拟场景：DPAPI 不可用或失败，但 payload 实际是 v1 格式）
        v1_value = f"{_PREFIX}{encoded}"
        if is_encrypted(v1_value):
            return _decrypt_v1(v1_value)

        raise DecryptionError("DPAPI 解密失败且无法回退到 enc:v1:（payload 非 v1 格式）")

    if not is_encrypted(ciphertext):
        # 明文直接返回（向后兼容）
        return ciphertext

    return _decrypt_v1(ciphertext)


# ── .env 迁移 ─────────────────────────────────────────────────
def _is_sensitive_key(key: str) -> bool:
    """判断环境变量名是否为敏感凭证（*_API_KEY / *_API_SECRET / *_TOKEN）"""
    if not key:
        return False
    upper = key.upper()
    return any(upper.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES)


def _parse_env_value(rest: str) -> tuple[str, str]:
    """解析 .env 值部分，返回 (quote, value)

    支持双引号、单引号、无引号三种格式。
    引号内的值原样保留，引号外的值去除尾部空白。
    """
    rest = rest.rstrip()
    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in ("'", '"'):
        return rest[0], rest[1:-1]
    return "", rest


def migrate_env_file(env_path: str) -> int:
    """扫描 .env 文件，把明文 API Key/Token/Secret 加密为 enc:v1: 或 enc:v2:dpapi: 格式

    幂等：已加密的值（v1 或 v2）不会重复加密。
    非敏感配置（如端口、URL、开关）保持不变。

    Args:
        env_path: .env 文件路径

    Returns:
        迁移的条目数（0 表示无变更或文件不存在）
    """
    path = Path(env_path)
    if not path.exists():
        logger.warning(f"迁移跳过：.env 文件不存在：{env_path}")
        return 0

    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"读取 .env 失败：{env_path}：{e}")
        return 0

    # 保留原始行尾格式
    trailing_newline = content.endswith("\n")
    lines = content.splitlines()
    new_lines: list[str] = []
    migrated = 0

    for line in lines:
        # 跳过空行和注释
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            new_lines.append(line)
            continue

        m = _ENV_LINE_PATTERN.match(line)
        if not m:
            new_lines.append(line)
            continue

        prefix, key, eq, rest = m.groups()
        if not _is_sensitive_key(key):
            new_lines.append(line)
            continue

        quote, value = _parse_env_value(rest)
        if not value or is_encrypted(value) or _is_dpapi_encrypted(value):
            # 空值或已加密（v1 或 v2），跳过
            new_lines.append(line)
            continue

        # 加密明文
        encrypted = encrypt(value)
        new_line = f"{prefix}{key}{eq}{quote}{encrypted}{quote}"
        new_lines.append(new_line)
        migrated += 1
        logger.info(f"已加密 .env 条目：{key}")

    if migrated == 0:
        return 0

    try:
        new_content = "\n".join(new_lines)
        if trailing_newline:
            new_content += "\n"
        path.write_text(new_content, encoding="utf-8")
        logger.info(f"迁移完成：共加密 {migrated} 个明文凭证，文件：{env_path}")
    except Exception as e:
        logger.error(f"写入 .env 失败：{env_path}：{e}")
        return 0

    return migrated
