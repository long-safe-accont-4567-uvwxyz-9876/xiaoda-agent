"""凭证保险库 - API Key 加密存储（机器绑定）

安全模型
--------
1. 加密密钥 = PBKDF2-HMAC-SHA256(机器身份, 固定盐) → 32 字节
2. 机器身份 = "用户名@主机名"（换机器自动失效，防 .env 被复制泄漏）
3. 流密码：密钥流 = HMAC-SHA256(密钥, nonce || counter) 拼接，与明文异或
4. 完整性：tag = HMAC-SHA256(密钥, nonce || 密文)，附在密文末尾
5. 输出格式：enc:v1:<base64url(nonce || 密文 || tag)>
6. 向后兼容：非 enc:v1: 前缀的值视为明文直接返回

仅使用标准库（hashlib / hmac / base64 / secrets），不依赖 cryptography。
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
from pathlib import Path

from loguru import logger

# ── 常量 ──────────────────────────────────────────────────────
# 盐：基础前缀 + 每次部署唯一随机后缀（首次启动时生成并持久化）
_SALT_BASE = b"xiaoda-agent-credential-vault-v2-"
_SALT_FILE = Path(os.getenv("CREDENTIAL_SALT_FILE", "config/credential_salt.bin"))
# PBKDF2 迭代次数（增大可减缓暴力破解）
_PBKDF2_ITERATIONS = 200_000
# 随机 nonce 长度（字节）
_NONCE_LEN = 16
# HMAC 认证标签长度（字节，SHA256 输出 32 字节）
_TAG_LEN = 32
# 加密值前缀
_PREFIX = "enc:v1:"
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
            pass
    # 首次：生成随机盐并持久化
    random_salt = os.urandom(32)
    try:
        _SALT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SALT_FILE.write_bytes(random_salt)
        # 设文件权限仅 owner 可读写（Unix）
        try:
            _SALT_FILE.chmod(0o600)
        except OSError:
            pass
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


# ── 公共 API ──────────────────────────────────────────────────
def is_encrypted(value: str) -> bool:
    """判断值是否已加密（enc:v1: 前缀 + 合法 base64url 主体）"""
    if not isinstance(value, str) or not value:
        return False
    if not value.startswith(_PREFIX):
        return False
    encoded = value[len(_PREFIX):]
    if not encoded:
        return False
    return _B64URL_PATTERN.fullmatch(encoded) is not None


def encrypt(plaintext: str) -> str:
    """加密明文，返回 enc:v1:<base64> 格式密文

    步骤：
      1. 生成 16 字节随机 nonce
      2. 派生密钥（PBKDF2）
      3. 生成密钥流并与明文异或得到密文
      4. 计算 HMAC-SHA256(密钥, nonce || 密文) 作为认证标签
      5. 输出 enc:v1: + base64url(nonce || 密文 || 标签)

    幂等：传入已加密的值会原样返回。
    """
    if not plaintext:
        return plaintext
    if is_encrypted(plaintext):
        # 已加密，幂等返回
        return plaintext

    key = _derive_key()
    nonce = secrets.token_bytes(_NONCE_LEN)
    pt_bytes = plaintext.encode("utf-8")
    ks = _keystream(key, nonce, len(pt_bytes))
    ciphertext = bytes(a ^ b for a, b in zip(pt_bytes, ks))
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()

    payload = nonce + ciphertext + tag
    encoded = base64.urlsafe_b64encode(payload).decode("ascii")
    return f"{_PREFIX}{encoded}"


def decrypt(ciphertext: str) -> str:
    """解密 enc:v1: 格式密文

    - 非 enc:v1: 前缀的值视为明文直接返回（向后兼容）
    - 解密失败（机器不匹配 / 标签验证失败 / 数据损坏）返回空字符串
    """
    if not ciphertext:
        return ciphertext or ""
    if not is_encrypted(ciphertext):
        # 明文直接返回（向后兼容）
        return ciphertext

    encoded = ciphertext[len(_PREFIX):]
    try:
        payload = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except Exception as e:
        logger.warning(f"凭证解密失败：base64 解码错误：{e}")
        return ""

    if len(payload) < _NONCE_LEN + _TAG_LEN:
        logger.warning(
            f"凭证解密失败：密文长度不足（{len(payload)} < {_NONCE_LEN + _TAG_LEN}）"
        )
        return ""

    nonce = payload[:_NONCE_LEN]
    tag = payload[-_TAG_LEN:]
    ciphertext_body = payload[_NONCE_LEN:-_TAG_LEN]

    key = _derive_key()
    expected_tag = hmac.new(key, nonce + ciphertext_body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        # 机器身份不匹配或数据被篡改
        logger.warning("凭证解密失败：HMAC 标签验证失败（机器不匹配或数据损坏）")
        return ""

    ks = _keystream(key, nonce, len(ciphertext_body))
    pt_bytes = bytes(a ^ b for a, b in zip(ciphertext_body, ks))
    try:
        return pt_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        logger.warning(f"凭证解密失败：UTF-8 解码错误：{e}")
        return ""


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
    """扫描 .env 文件，把明文 API Key/Token/Secret 加密为 enc:v1: 格式

    幂等：已加密的值不会重复加密。
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
        if not value or is_encrypted(value):
            # 空值或已加密，跳过
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
