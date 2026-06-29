"""凭证加密存储 — 机器级密钥 + Fernet(AES-128-CBC)

安全模型:
- 加密密钥 = SHA256(机器指纹 + 盐值) → Fernet兼容格式
- 机器指纹 = CPU ID + 首个MAC地址 (换机器自动失效,防拷贝泄漏)
- 内存中只存密文, __str__ 返回 ***遮蔽
- 解密仅在发起 API 调用时按需进行
"""
import os, base64, hashlib, platform
from loguru import logger


class EncryptedCredential:
    """凭证加密存储包装层"""

    def __init__(self, encrypted_b64: str, salt: str = "xiaoda-agent") -> None:
        """初始化加密凭证。

        参数:
            encrypted_b64: Base64 编码的密文。
            salt: 派生密钥所用的盐值，需与加密时保持一致。
        """
        self._encrypted = encrypted_b64
        self._salt = salt
        self._machine_key = self._derive_machine_key(salt)

    @classmethod
    def from_plaintext(cls, plaintext: str, salt: str = "xiaoda-agent") -> "EncryptedCredential":
        """从明文构造加密凭证对象（工厂方法）。"""
        key = cls._derive_machine_key_static(salt)
        encrypted = cls._encrypt(plaintext, key)
        return cls(base64.urlsafe_b64encode(encrypted).decode(), salt)

    def decrypt(self) -> str:
        """使用机器密钥解密并返回明文。"""
        from cryptography.fernet import Fernet
        f = Fernet(self._machine_key)
        return f.decrypt(base64.urlsafe_b64decode(self._encrypted)).decode()

    def __str__(self) -> str:
        """返回遮蔽密文尾部的字符串表示，避免泄露。"""
        return f"EncryptedCredential(***{self._encrypted[-6:]})"

    def __repr__(self) -> str:
        """返回与 __str__ 一致的可打印表示。"""
        return self.__str__()

    @staticmethod
    def _get_machine_fingerprint() -> str:
        """获取机器指纹（CPU ID + 首个 MAC 地址）。"""
        cpu_id = platform.processor() or "unknown-cpu"
        try:
            import uuid
            mac = uuid.getnode()
            mac_str = ":".join(f"{(mac >> (8*i)) & 0xFF:02x}" for i in range(5, -1, -1))
        except Exception:
            mac_str = "00:00:00:00:00:00"
        return f"{cpu_id}|{mac_str}"

    @staticmethod
    def _derive_machine_key_static(salt: str) -> bytes:
        """由机器指纹和盐值派生 Fernet 兼容的密钥。"""
        fp = EncryptedCredential._get_machine_fingerprint()
        raw = hashlib.sha256(f"{fp}:{salt}".encode()).digest()
        return base64.urlsafe_b64encode(raw)

    def _derive_machine_key(self, salt: str) -> bytes:
        """实例方法版密钥派生，委托给静态方法。"""
        return self._derive_machine_key_static(salt)

    @staticmethod
    def _encrypt(plaintext: str, key: bytes) -> bytes:
        """使用 Fernet 加密明文并返回密文字节。"""
        from cryptography.fernet import Fernet
        f = Fernet(key)
        return f.encrypt(plaintext.encode())

    @staticmethod
    def is_available() -> bool:
        """检查 cryptography 库是否可用"""
        try:
            from cryptography.fernet import Fernet  # noqa: F401
            return True
        except ImportError:
            return False


def protect_credential(plaintext: str, salt: str = "xiaoda-agent") -> EncryptedCredential | str:
    """安全包装凭证: 若 cryptography 可用则加密, 否则返回原文并告警"""
    if not plaintext:
        return plaintext
    if EncryptedCredential.is_available():
        try:
            return EncryptedCredential.from_plaintext(plaintext, salt)
        except Exception as e:
            logger.warning(f"凭证加密失败,回退明文: {e}")
            return plaintext
    logger.warning("cryptography 库未安装, API Key 以明文存储")
    return plaintext


def reveal_credential(cred: EncryptedCredential | str) -> str:
    """解密凭证: 若为 EncryptedCredential 则解密, 若为 str 则直接返回"""
    if isinstance(cred, EncryptedCredential):
        return cred.decrypt()
    return cred or ""
