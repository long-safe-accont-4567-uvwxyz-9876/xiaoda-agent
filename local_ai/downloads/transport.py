from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx
from loguru import logger

from local_ai.contracts import CatalogModel


@dataclass(frozen=True)
class DownloadStream:
    status_code: int
    total_size: int
    chunks: AsyncIterator[bytes]
    range_start: int | None = None
    range_total: int | None = None
    close: Callable[[], Awaitable[None]] | None = None


class DownloadTransport(Protocol):
    async def open(self, model: CatalogModel, path: str, offset: int) -> DownloadStream: ...


class HttpDownloadTransport:
    def __init__(self, client: httpx.AsyncClient | None = None, chunk_size: int = 256 * 1024) -> None:
        # 显式超时而非 timeout=None：连接挂起/断流时不能无限等待，
        # 否则下载任务永久卡在 downloading 状态。
        timeout = httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=15.0)
        self._client = client or httpx.AsyncClient(follow_redirects=True, timeout=timeout)
        self._chunk_size = chunk_size

    async def open(self, model: CatalogModel, path: str, offset: int) -> DownloadStream:
        if model.source != "modelscope":
            raise ValueError(f"unsupported download source: {model.source}")
        repository = quote(model.repository, safe="/")
        revision = quote(model.revision, safe="")
        file_path = quote(path, safe="/")
        url = f"https://www.modelscope.cn/models/{repository}/resolve/{revision}/{file_path}"
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        request = self._client.build_request("GET", url, headers=headers)
        response = await self._client.send(request, stream=True)
        try:
            response.raise_for_status()
        except (OSError, RuntimeError, ConnectionError, ValueError):
            await response.aclose()
            raise
        except Exception:
            logger.exception("transport.open_stream.unexpected_error url={}", url)
            await response.aclose()
            raise
        total_size = _total_size(response, offset)
        range_start = _range_start(response)
        range_total = _range_total(response)

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes(self._chunk_size):
                    if chunk:
                        yield chunk
            finally:
                await response.aclose()

        return DownloadStream(
            response.status_code,
            total_size,
            chunks(),
            range_start,
            range_total,
            response.aclose,
        )


def _total_size(response: httpx.Response, offset: int) -> int:
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        value = content_range.rsplit("/", 1)[1]
        if value.isdigit():
            return int(value)
    length = response.headers.get("Content-Length")
    if length and length.isdigit():
        return int(length) + (offset if response.status_code == 206 else 0)
    return 0


def _range_start(response: httpx.Response) -> int | None:
    content_range = response.headers.get("Content-Range", "")
    if not content_range.startswith("bytes ") or "/" not in content_range:
        return None
    interval = content_range[6:].split("/", 1)[0]
    start = interval.split("-", 1)[0]
    return int(start) if start.isdigit() else None


def _range_total(response: httpx.Response) -> int | None:
    content_range = response.headers.get("Content-Range", "")
    if "/" not in content_range:
        return None
    total = content_range.rsplit("/", 1)[1]
    return int(total) if total.isdigit() else None
