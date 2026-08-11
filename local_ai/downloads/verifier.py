from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path


async def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    return await asyncio.to_thread(_sha256_file, path, chunk_size)


def _sha256_file(path: Path, chunk_size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()
