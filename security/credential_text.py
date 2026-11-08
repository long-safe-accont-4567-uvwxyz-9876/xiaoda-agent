"""凭证类文本（登录密码 / 找回问答）的共用校验与常量。

WebUI 的登录密码、找回问题与找回答案都允许中文、英文、数字与常见符号：
本模块不做任何字符集白名单（没有 ``[A-Za-z0-9]``，也没有 ``\\w``），长度
一律按 Unicode 字符数计算（``len(str)`` 即字符数，不是 UTF-8 字节数），
因此「中文密码」与「纯 ASCII 密码」走完全相同的规则。

配套前端规则在 ``web/frontend/src/utils/credentialInput.ts``，两边常量必须
保持一致（tests/test_recovery_charset_unicode.py 会断言这一点）。

编码注意：``hmac.compare_digest`` 对 str 只支持纯 ASCII，含中文会抛
TypeError，所以比较一律先 ``str.encode("utf-8")`` 成 bytes。
"""
from __future__ import annotations

import hmac

# 最小登录密码长度（字符数）
MIN_PASSWORD_LEN = 8
# 最小找回答案长度（字符数）
MIN_ANSWER_LEN = 6
# 找回问题最大长度（字符数）
MAX_QUESTION_LEN = 200


def normalize_credential(value: object) -> str:
    """去首尾空白；None 与非字符串按空串处理（与前端 trim 语义一致）。"""
    if value is None:
        return ""
    return str(value).strip()


def char_length(value: str | None) -> int:
    """按 Unicode 字符数计长度，不按字节。"""
    return len(value or "")


def constant_time_equal(left: str, right: str) -> bool:
    """Unicode 安全的常量时间比较（UTF-8 编码后比对 bytes）。"""
    return hmac.compare_digest(
        (left or "").encode("utf-8"), (right or "").encode("utf-8")
    )


def validate_answer(value: object) -> str:
    """校验找回答案：只限最小字符数，不限字符集。"""
    answer = normalize_credential(value)
    if char_length(answer) < MIN_ANSWER_LEN:
        raise ValueError(f"找回答案至少需要 {MIN_ANSWER_LEN} 个字符")
    return answer


def validate_question(value: object) -> str:
    """校验找回问题：1~MAX_QUESTION_LEN 个字符，不限字符集。"""
    question = normalize_credential(value)
    if not 1 <= char_length(question) <= MAX_QUESTION_LEN:
        raise ValueError(f"找回问题长度需在 1~{MAX_QUESTION_LEN} 个字符之间")
    return question
