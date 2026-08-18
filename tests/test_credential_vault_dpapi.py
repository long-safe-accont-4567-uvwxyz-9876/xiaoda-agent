"""测试 security/credential_vault.py 的 Windows DPAPI 加密支持

覆盖场景：
- Windows + pywin32 可用：encrypt/decrypt 走 DPAPI 路径
- Windows + pywin32 不可用：fallback 到 enc:v1: 并 logger.warning（仅首次）
- Linux 行为完全不变（走 enc:v1: 路径）
- DPAPI 解密失败时 fallback 到 enc:v1: 路径

由于项目运行在 Linux，所有 Windows 行为均通过 unittest.mock.patch 模拟，
不会真正导入 win32crypt。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 将项目根目录加入 sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import security.credential_vault as cv
from security.credential_vault import (
    DecryptionError,
    decrypt,
    encrypt,
    is_encrypted,
)


# ── 辅助函数 ──────────────────────────────────────────────────
def _reset_dpapi_warning_flag():
    """重置模块级 _DPAPI_WARNED 标记，确保每个测试都能触发 warning"""
    cv._DPAPI_WARNED = False


# ── 测试用例 ──────────────────────────────────────────────────
def test_dpapi_available_uses_dpapi():
    """Windows + pywin32 可用时，encrypt() 走 DPAPI 路径

    验证：
    - encrypt() 返回 enc:v2:dpapi: 前缀的值
    - win32crypt.CryptProtectData 被调用
    - decrypt() 也走 DPAPI 路径
    - win32crypt.CryptUnprotectData 被调用
    """
    plaintext = "sk-test-dpapi-secret-12345"

    with patch.object(cv.sys, "platform", "win32"), \
         patch.object(cv, "HAS_WIN32CRYPT", True), \
         patch.object(cv, "win32crypt") as mock_win32crypt:
        # 模拟 CryptProtectData 返回加密后的 bytes
        mock_win32crypt.CryptProtectData.return_value = b"dpapi-encrypted-blob"
        # 模拟 CryptUnprotectData 返回 (description, data, entropy)
        mock_win32crypt.CryptUnprotectData.return_value = (
            None,
            plaintext.encode("utf-8"),
            None,
        )

        # 加密：应走 DPAPI 路径
        encrypted = encrypt(plaintext)
        assert encrypted.startswith("enc:v2:dpapi:"), \
            f"DPAPI 加密的值应以 enc:v2:dpapi: 开头，实际: {encrypted[:30]}"
        assert not encrypted.startswith("enc:v1:")
        mock_win32crypt.CryptProtectData.assert_called_once()

        # 验证 CryptProtectData 被调用时传入了明文 bytes
        call_args = mock_win32crypt.CryptProtectData.call_args
        # 第一个位置参数应是明文的 bytes
        first_arg = call_args[0][0] if call_args[0] else call_args[1].get("dataIn")
        assert first_arg == plaintext.encode("utf-8"), \
            "CryptProtectData 第一个参数应为明文 bytes"

        # 解密：也应走 DPAPI 路径
        decrypted = decrypt(encrypted)
        assert decrypted == plaintext
        mock_win32crypt.CryptUnprotectData.assert_called_once()


def test_dpapi_unavailable_falls_back():
    """Windows + pywin32 不可用时，fallback 到 enc:v1: 并 logger.warning

    验证：
    - encrypt() 返回 enc:v1: 前缀的值（fallback 到 v1 路径）
    - logger.warning 被调用（首次警告）
    - 不调用 win32crypt（因为不可用）
    """
    _reset_dpapi_warning_flag()
    plaintext = "sk-test-no-pywin32-fallback"

    with patch.object(cv.sys, "platform", "win32"), \
         patch.object(cv, "HAS_WIN32CRYPT", False), \
         patch.object(cv, "win32crypt", None), \
         patch.object(cv, "logger") as mock_logger:
        # 加密：应 fallback 到 enc:v1:
        encrypted = encrypt(plaintext)
        assert encrypted.startswith("enc:v1:"), \
            f"无 pywin32 时应 fallback 到 enc:v1:，实际: {encrypted[:30]}"
        assert not encrypted.startswith("enc:v2:dpapi:")

        # 应该有 warning 日志
        assert mock_logger.warning.called, \
            "无 pywin32 时应输出 logger.warning"

        # 验证 warning 内容包含 pywin32 相关信息
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        pywin32_warning_found = any("pywin32" in w for w in warning_calls)
        assert pywin32_warning_found, \
            f"warning 应包含 pywin32 相关信息，实际 calls: {warning_calls}"

        # 验证加密结果可正常解密（v1 路径）
        decrypted = decrypt(encrypted)
        assert decrypted == plaintext


def test_dpapi_unavailable_warning_only_once():
    """Windows + pywin32 不可用时，logger.warning 只触发一次（避免日志刷屏）"""
    _reset_dpapi_warning_flag()
    plaintext1 = "sk-test-warning-once-1"
    plaintext2 = "sk-test-warning-once-2"

    with patch.object(cv.sys, "platform", "win32"), \
         patch.object(cv, "HAS_WIN32CRYPT", False), \
         patch.object(cv, "win32crypt", None), \
         patch.object(cv, "logger") as mock_logger:
        # 第一次加密：应触发 warning
        encrypt(plaintext1)
        first_call_count = mock_logger.warning.call_count
        assert first_call_count >= 1, "首次加密应触发 warning"

        # 第二次加密：不应再触发 pywin32 相关 warning
        encrypt(plaintext2)
        second_call_count = mock_logger.warning.call_count
        # warning 调用次数不应增加（_DPAPI_WARNED 标记生效）
        assert second_call_count == first_call_count, \
            f"warning 应只触发一次，第一次: {first_call_count}，第二次: {second_call_count}"


def test_linux_unchanged():
    """Linux 上 encrypt/decrypt 行为完全不变，走 enc:v1: 路径

    验证：
    - sys.platform 不是 'win32'（真实 Linux 环境）
    - encrypt() 返回 enc:v1: 前缀的值
    - decrypt() 能正确解密
    - is_encrypted() 正确识别
    - 不调用 win32crypt
    """
    # 确认当前是 Linux 环境
    assert sys.platform != "win32", "此测试必须在非 Windows 环境运行"

    plaintext = "sk-linux-unchanged-secret"

    # 不 patch 任何东西，使用真实 Linux 行为
    encrypted = encrypt(plaintext)
    assert encrypted.startswith("enc:v1:"), \
        f"Linux 上应走 enc:v1: 路径，实际: {encrypted[:30]}"
    assert not encrypted.startswith("enc:v2:dpapi:")

    # is_encrypted 正确识别
    assert is_encrypted(encrypted) is True

    # decrypt 正确解密
    decrypted = decrypt(encrypted)
    assert decrypted == plaintext

    # 幂等性：已加密的值不再重复加密
    re_encrypted = encrypt(encrypted)
    assert re_encrypted == encrypted, "已加密的值应幂等返回"


def test_dpapi_decrypt_failure_falls_back():
    """DPAPI 解密失败时 fallback 到 enc:v1: 路径

    场景：一个值带有 enc:v2:dpapi: 前缀，但其 payload 实际是 v1 格式的密文。
    当 DPAPI 解密失败时，应回退到 v1 解密路径并成功返回明文。

    这模拟了迁移场景：DPAPI 加密失败后 fallback 到 v1，但值被打上了 v2 前缀。
    """
    plaintext = "sk-test-dpapi-decrypt-fallback"

    # 1. 使用真实 Linux 环境加密得到 v1 密文
    v1_encrypted = encrypt(plaintext)
    assert v1_encrypted.startswith("enc:v1:")
    v1_payload = v1_encrypted[len("enc:v1:"):]

    # 2. 构造一个带 v2 前缀、payload 是 v1 密文的值
    v2_value = f"enc:v2:dpapi:{v1_payload}"

    # 3. 模拟 Windows + DPAPI 可用，但 CryptUnprotectData 抛异常
    with patch.object(cv.sys, "platform", "win32"), \
         patch.object(cv, "HAS_WIN32CRYPT", True), \
         patch.object(cv, "win32crypt") as mock_win32crypt:
        mock_win32crypt.CryptUnprotectData.side_effect = Exception("DPAPI decryption failed")

        # 4. decrypt 应先尝试 DPAPI（失败），再 fallback 到 v1（成功）
        result = decrypt(v2_value)
        assert result == plaintext, \
            f"DPAPI 失败后应 fallback 到 v1 解密，期望: {plaintext}，实际: {result}"

        # 验证 DPAPI 确实被尝试过
        mock_win32crypt.CryptUnprotectData.assert_called_once()


def test_dpapi_encrypt_idempotent():
    """已 DPAPI 加密的值不应重复加密（幂等性）"""
    plaintext = "sk-test-dpapi-idempotent"

    with patch.object(cv.sys, "platform", "win32"), \
         patch.object(cv, "HAS_WIN32CRYPT", True), \
         patch.object(cv, "win32crypt") as mock_win32crypt:
        mock_win32crypt.CryptProtectData.return_value = b"dpapi-blob"
        mock_win32crypt.CryptUnprotectData.return_value = (
            None,
            plaintext.encode("utf-8"),
            None,
        )

        # 第一次加密
        encrypted1 = encrypt(plaintext)
        assert encrypted1.startswith("enc:v2:dpapi:")
        assert mock_win32crypt.CryptProtectData.call_count == 1

        # 第二次加密（传入已加密的值）：应幂等返回，不再调用 CryptProtectData
        encrypted2 = encrypt(encrypted1)
        assert encrypted2 == encrypted1, "已 DPAPI 加密的值应幂等返回"
        assert mock_win32crypt.CryptProtectData.call_count == 1, \
            "幂等加密不应再次调用 CryptProtectData"


def test_dpapi_encrypt_failure_falls_back_to_v1():
    """DPAPI 加密失败时 fallback 到 enc:v1: 路径"""
    plaintext = "sk-test-dpapi-encrypt-failure"

    with patch.object(cv.sys, "platform", "win32"), \
         patch.object(cv, "HAS_WIN32CRYPT", True), \
         patch.object(cv, "win32crypt") as mock_win32crypt, \
         patch.object(cv, "logger") as mock_logger:
        # 模拟 CryptProtectData 抛异常
        mock_win32crypt.CryptProtectData.side_effect = Exception("DPAPI encrypt failed")

        # 加密：DPAPI 失败后应 fallback 到 v1
        encrypted = encrypt(plaintext)
        assert encrypted.startswith("enc:v1:"), \
            f"DPAPI 加密失败应 fallback 到 enc:v1:，实际: {encrypted[:30]}"
        assert not encrypted.startswith("enc:v2:dpapi:")

        # 应有 warning 日志
        assert mock_logger.warning.called, "DPAPI 加密失败应输出 warning"

        # 验证可正常解密
        decrypted = decrypt(encrypted)
        assert decrypted == plaintext


def test_v1_value_still_decrypts_on_windows():
    """Windows 上 enc:v1: 格式的值仍能通过 v1 路径解密（向后兼容）

    这确保已存在的 enc:v1: 凭证在迁移到 Windows + DPAPI 后仍可用。
    """
    plaintext = "sk-test-v1-backward-compat"

    # 1. 在 Linux 环境下加密为 v1
    v1_encrypted = encrypt(plaintext)
    assert v1_encrypted.startswith("enc:v1:")

    # 2. 切换到 Windows + DPAPI 可用环境
    with patch.object(cv.sys, "platform", "win32"), \
         patch.object(cv, "HAS_WIN32CRYPT", True), \
         patch.object(cv, "win32crypt") as mock_win32crypt:
        # v1 值不应走 DPAPI 解密，应直接走 v1 路径
        decrypted = decrypt(v1_encrypted)
        assert decrypted == plaintext

        # CryptUnprotectData 不应被调用（因为不是 v2 前缀）
        mock_win32crypt.CryptUnprotectData.assert_not_called()


def test_plaintext_passthrough_on_windows():
    """Windows 上明文仍直接返回（向后兼容）"""
    with patch.object(cv.sys, "platform", "win32"), \
         patch.object(cv, "HAS_WIN32CRYPT", True), \
         patch.object(cv, "win32crypt"):
        assert decrypt("") == ""
        assert decrypt("sk-plaintext-key") == "sk-plaintext-key"
        assert decrypt("any-non-enc-value") == "any-non-enc-value"
