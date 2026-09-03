"""上传读取公共工具：分块读取 + 限额即时拒绝（2026-08-26 web 审计 P2 治本）。

反模式（五连）：`content = await file.read()` 把整个 body 读进内存后才检查
10/20MB 上限--攻击者提交超大文件时内存峰值等于文件大小。改为每次读 1MB
累计，一超限立即 413/400，内存峰值最多超限 1MB。
"""
from __future__ import annotations

import io
import zipfile
from typing import Any

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

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
    if total == 0:
        raise HTTPException(400, f"{label}不能为空")
    return b"".join(chunks)


_IMAGE_FORMATS = {
    ".gif": "GIF",
    ".jpeg": "JPEG",
    ".jpg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}
_OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_OOXML_MARKERS = {
    ".docx": "word/",
    ".pptx": "ppt/",
    ".xlsx": "xl/",
}


def validate_image_content(content: bytes, extension: str) -> None:
    """Decode an uploaded image and require its format to match the extension."""
    expected = _IMAGE_FORMATS.get(extension.lower())
    if expected is None:
        raise HTTPException(400, "不支持的图片格式")
    try:
        with Image.open(io.BytesIO(content)) as image:
            actual = (image.format or "").upper()
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise HTTPException(400, "图片内容无效或已损坏") from None
    if actual != expected:
        raise HTTPException(400, "图片内容与文件扩展名不匹配")


def validate_document_content(content: bytes, extension: str) -> None:
    """Validate supported document containers without executing their contents."""
    ext = extension.lower()
    if ext == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise HTTPException(400, "PDF 文件内容无效")
        return
    if ext in _OOXML_MARKERS:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = archive.namelist()
                if "[Content_Types].xml" not in names or not any(
                    name.startswith(_OOXML_MARKERS[ext]) for name in names
                ):
                    raise HTTPException(400, "Office 文件结构无效")
        except (zipfile.BadZipFile, OSError, ValueError):
            raise HTTPException(400, "Office 文件内容无效或已损坏") from None
        return
    if ext in {".doc", ".ppt", ".xls"}:
        if not content.startswith(_OLE_SIGNATURE):
            raise HTTPException(400, "旧版 Office 文件内容无效")
        return
    if ext in {".txt", ".md"}:
        try:
            content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise HTTPException(400, "文本文件必须使用 UTF-8 编码") from None
        return
    raise HTTPException(400, "不支持的文档格式")


def b64_length_within_limit(payload_b64: str, decoded_limit: int) -> bool:
    """base64 字符数预检：不解码即可判断解码后是否可能超限。

    base64 每 4 字符解码 3 字节；字符数上限含 padding 余量。
    用于在 b64decode 之前拒绝超大壁纸（P1：先查大小后解码）。
    """
    return len(payload_b64) <= decoded_limit * 4 // 3 + 4
