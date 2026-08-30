"""WebUI 密码找回问答存储（纯逻辑，不 import web.*，可单测）。

文件位置：``<credentials_dir>/webui_recovery.json``，权限 0600。
格式：``{"question": str, "salt": hex, "iterations": 200000, "answer_hash": hex}``。
答案使用 PBKDF2-HMAC-SHA256（hashlib.pbkdf2_hmac）+ 16 字节随机 salt
（secrets.token_bytes）哈希存储，绝不落明文。

容错约定：I/O / JSON 异常一律记 logger.warning 并安全降级，不抛给调用方；
仅参数校验失败时明确 raise ValueError（中文消息）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from pathlib import Path

from loguru import logger

_ITERATIONS = 200000
_FILE_NAME = "webui_recovery.json"
# recover 端点无鉴权（仅靠每 IP 5 次/600s 锁缓解），答案过短可被枚举爆破后
# 直接重置主人密码。最小长度由 web/routers/setup.py 同步 import 复用，防两处漂移
MIN_ANSWER_LEN = 6


def _get_path() -> Path:
    from config import get_credentials_dir
    return get_credentials_dir() / _FILE_NAME


def _load() -> dict | None:
    """读取并校验恢复文件。无文件/解析失败/字段缺失时返回 None（记 warning）。"""
    path = _get_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("recovery_qa.load_failed error={}", str(exc))
        return None
    if not isinstance(data, dict):
        logger.warning("recovery_qa.load_invalid_shape")
        return None
    for field in ("question", "salt", "answer_hash"):
        if not isinstance(data.get(field), str) or not data[field]:
            logger.warning("recovery_qa.load_missing_field field={}", field)
            return None
    if not isinstance(data.get("iterations"), int):
        logger.warning("recovery_qa.load_invalid_iterations")
        return None
    return data


def _hash_answer(answer: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", answer.encode("utf-8"), salt, iterations)


def set_recovery(question: str, answer: str) -> None:
    """保存找回问答（覆盖旧值）。校验失败 raise ValueError（中文消息）。"""
    question = (question or "").strip()
    answer = (answer or "").strip()
    if not (1 <= len(question) <= 200):
        raise ValueError("找回问题长度需在 1~200 个字符之间")
    if len(answer) < MIN_ANSWER_LEN:
        raise ValueError(f"找回答案至少需要 {MIN_ANSWER_LEN} 个字符")

    salt = secrets.token_bytes(16)
    data = {
        "question": question,
        "salt": salt.hex(),
        "iterations": _ITERATIONS,
        "answer_hash": _hash_answer(answer, salt, _ITERATIONS).hex(),
    }
    path = _get_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        path.chmod(0o600)
    except OSError as exc:
        logger.warning("recovery_qa.write_failed error={}", str(exc))


def get_question() -> str | None:
    """返回已配置的找回问题；无文件/解析失败返回 None。"""
    data = _load()
    return data["question"] if data else None


def verify_answer(answer: str) -> bool:
    """hmac.compare_digest 常量时间比对；未配置或校验失败返回 False。

    与 set_recovery 一致：比对前对 answer 做 strip（容忍首尾空格）。
    """
    data = _load()
    if not data or not isinstance(answer, str):
        return False
    answer = answer.strip()
    if not answer:
        return False
    try:
        salt = bytes.fromhex(data["salt"])
        expected = bytes.fromhex(data["answer_hash"])
    except (ValueError, TypeError) as exc:
        logger.warning("recovery_qa.invalid_hex_fields error={}", str(exc))
        return False
    try:
        candidate = _hash_answer(answer, salt, int(data["iterations"]))
    except (ValueError, TypeError) as exc:
        logger.warning("recovery_qa.hash_failed error={}", str(exc))
        return False
    return hmac.compare_digest(candidate, expected)


def clear_recovery() -> None:
    """删除恢复文件（幂等，失败仅记 warning）。"""
    path = _get_path()
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("recovery_qa.clear_failed error={}", str(exc))
