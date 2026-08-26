"""上传读取公共工具：分块读取 + 限额即时拒绝（2026-08-26 web 审计 P2 治本）。

反模式（五连）：`content = await file.read()` 把整个 body 读进内存后才检查
10/20MB 上限--攻击者提交超大文件时内存峰值等于文件大小。改为每次读 1MB
累计，一超限立即 413/400，内存峰值最多超限 1MB。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

_CHUNK = 1024 * 1024


async def read_upload_limited(file: Any, max_bytes: int, label: str) -> bytes:
    """分块读取上传文件，累计超过 max_bytes 立即抛 400。

    label 用于错误消息（如"图片""音频文件"），上限按 MB 取整展示。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(400, f"{label}不能超过 {max_bytes // (1024 * 1024)}MB")
        chunks.append(chunk)
    return b"".join(chunks)


def b64_length_within_limit(payload_b64: str, decoded_limit: int) -> bool:
    """base64 字符数预检：不解码即可判断解码后是否可能超限。

    base64 每 4 字符解码 3 字节；字符数上限含 padding 余量。
    用于在 b64decode 之前拒绝超大壁纸（P1：先查大小后解码）。
    """
    return len(payload_b64) <= decoded_limit * 4 // 3 + 4
