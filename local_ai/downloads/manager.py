from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from core.cancel_token import CancellationError, CancelToken
from loguru import logger
from local_ai.contracts import (
    CatalogFile,
    CatalogModel,
    DownloadTask,
    InstalledModel,
    TaskState,
)
from local_ai.downloads.transport import DownloadTransport, HttpDownloadTransport
from local_ai.downloads.verifier import sha256_file

EventSink = Callable[[dict[str, Any]], Awaitable[None] | None]


def _make_hf_progress_bar(
    token: CancelToken,
    task_id: str,
    manager: DownloadManager,
    completed_before: int,
    starting_bytes: int,
    started: float,
    loop: asyncio.AbstractEventLoop,
) -> type:
    from huggingface_hub.utils import tqdm as hf_tqdm

    class _ProgressBar(hf_tqdm):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._cancelled = False
            super().__init__(*args, **kwargs)

        def update(self, n: int = 1) -> None:
            if token.cancelled:
                self._cancelled = True
                raise KeyboardInterrupt("cancelled")
            super().update(n)
            downloaded = max(
                manager._tasks[task_id].bytes_downloaded,
                completed_before + self.n,
            )
            elapsed = max(time.monotonic() - started, 1e-9)
            speed = max(downloaded - starting_bytes, 0) / elapsed
            remaining = max(manager._tasks[task_id].total_bytes - downloaded, 0)
            try:
                asyncio.run_coroutine_threadsafe(
                    manager._update(
                        task_id,
                        bytes_downloaded=downloaded,
                        speed_bps=speed,
                        eta_seconds=remaining / speed if speed else None,
                    ),
                    loop,
                )
            except (OSError, RuntimeError, ValueError):
                logger.debug("download.progress_report_failed", exc_info=True)
            except Exception:
                logger.exception("download._download_file_hf.progress_unexpected")

        def close(self) -> None:
            try:
                if token.cancelled:
                    self._cancelled = True
                    raise KeyboardInterrupt("cancelled")
            finally:
                super().close()

    return _ProgressBar


class DownloadManager:
    def __init__(
        self,
        registry: Any,
        transport: DownloadTransport | None = None,
        event_sink: EventSink | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        self._registry = registry
        self._transport = transport or HttpDownloadTransport()
        self._event_sink = event_sink
        self._state_path = Path(state_path) if state_path is not None else Path("downloads.json")
        self._tasks: dict[str, DownloadTask] = {}
        self._models: dict[str, CatalogModel] = {}
        self._tokens: dict[str, CancelToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # 正在运行下载循环的 task_id 集合：pause/cancel 据此判断是否可立即
        # 删除 .part，还是需要等循环退出（关闭文件句柄）后再清理。
        self._running: set[str] = set()
        self._discard_on_exit: set[str] = set()

    def create(self, model: CatalogModel, destination: str | Path) -> DownloadTask:
        destination_path = Path(destination).expanduser().resolve()
        now = datetime.now(timezone.utc)
        task = DownloadTask(
            id=f"download:{uuid.uuid4().hex}",
            model_id=model.id,
            state=TaskState.PENDING,
            bytes_downloaded=self._partial_bytes(model, destination_path),
            total_bytes=sum(file.size for file in model.files),
            destination=str(destination_path),
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.id] = task
        self._models[task.id] = model
        self._persist_sync()
        return task

    def list(self) -> list[DownloadTask]:
        return list(self._tasks.values())

    def active_for_model(self, model_id: str) -> list[DownloadTask]:
        """Return non-terminal download tasks referencing the given model.

        completed / failed / cancelled / quarantined 视为终态，不再占用模型。
        """
        terminal = {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.QUARANTINED,
        }
        return [
            task
            for task in self._tasks.values()
            if task.model_id == model_id and task.state not in terminal
        ]

    async def start(self, task_id: str) -> DownloadTask:
        self._require_task(task_id)
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        self._running.add(task_id)
        try:
            async with lock:
                task = self._require_task(task_id)
                if task.state is TaskState.COMPLETED:
                    return task
                token = CancelToken(timeout=None)
                self._tokens[task_id] = token
                await self._update(task_id, state=TaskState.DOWNLOADING, error=None)
                started = time.monotonic()
                starting_bytes = self._tasks[task_id].bytes_downloaded
                try:
                    model = self._models[task_id]
                    for manifest in model.files:
                        token.check()
                        await self._download_file(task_id, model, manifest, token, started, starting_bytes)
                    token.check()
                    await self._register(task_id, model)
                    return await self._update(
                        task_id,
                        state=TaskState.COMPLETED,
                        bytes_downloaded=self._tasks[task_id].total_bytes,
                        speed_bps=None,
                        eta_seconds=None,
                    )
                except CancellationError:
                    # pause/cancel 已设置目标状态（PAUSED/CANCELLED），此处只返回
                    # 不覆盖，避免状态被下载循环的异常路径改写成 FAILED。
                    return self._tasks[task_id]
                except HashMismatchError as error:
                    return await self._update(
                        task_id,
                        state=TaskState.QUARANTINED,
                        speed_bps=None,
                        eta_seconds=None,
                        error=str(error),
                    )
                except (OSError, RuntimeError, ConnectionError, ValueError) as error:
                    logger.warning("download.task_failed task={} error={}", task_id, str(error)[:200])
                    return await self._update(
                        task_id,
                        state=TaskState.FAILED,
                        speed_bps=None,
                        eta_seconds=None,
                        error=str(error),
                    )
                except Exception as error:
                    logger.exception("download.start.unexpected_error task={}", task_id)
                    return await self._update(
                        task_id,
                        state=TaskState.FAILED,
                        speed_bps=None,
                        eta_seconds=None,
                        error=str(error),
                    )
                finally:
                    token.cleanup()
                    self._tokens.pop(task_id, None)
        finally:
            self._running.discard(task_id)
            # 循环退出（文件句柄已关闭）后再清理 .part，避免与 cancel(discard)
            # 交错导致 os.replace 抛 FileNotFoundError。
            if task_id in self._discard_on_exit:
                self._discard_on_exit.discard(task_id)
                self._discard_partials(
                    self._models[task_id], Path(self._tasks[task_id].destination)
                )

    async def pause(self, task_id: str) -> DownloadTask:
        self._require_task(task_id)
        # 不能等待下载循环退出：循环可能阻塞在慢 IO 上（互相等待即死锁）。
        # 只取消 token 并立即置 PAUSED，循环在下一次 token.check() 退出且不覆盖状态。
        token = self._tokens.get(task_id)
        if token is not None:
            token.cancel("pause")
        return await self._update(task_id, state=TaskState.PAUSED, speed_bps=None, eta_seconds=None)

    async def resume(self, task_id: str) -> DownloadTask:
        task = self._require_task(task_id)
        if task.state not in {TaskState.PAUSED, TaskState.CANCELLED, TaskState.FAILED}:
            return task
        return await self.start(task_id)

    async def cancel(self, task_id: str, discard_partials: bool = False) -> DownloadTask:
        task = self._require_task(task_id)
        # 与 pause 同理：不等待下载循环，直接取消 token 并置 CANCELLED。
        token = self._tokens.get(task_id)
        if token is not None:
            token.cancel("cancel")
        if discard_partials:
            if task_id in self._running:
                # 下载循环仍可能持有 .part 文件句柄：标记为退出后清理，
                # 由 start() 的外层 finally 在句柄关闭后执行删除。
                self._discard_on_exit.add(task_id)
            else:
                self._discard_partials(self._models[task_id], Path(task.destination))
        return await self._update(
            task_id,
            state=TaskState.CANCELLED,
            bytes_downloaded=self._partial_bytes(self._models[task_id], Path(task.destination)),
            speed_bps=None,
            eta_seconds=None,
        )

    async def delete(self, task_id: str) -> None:
        """删除任务记录（不可恢复）。

        若仍在下载则取消下载循环，随后移除任务与模型映射并持久化。
        已下载的文件/分片不会删除（仅移除任务登记）。
        """
        self._require_task(task_id)
        token = self._tokens.get(task_id)
        if token is not None:
            token.cancel("deleted")
        self._tasks.pop(task_id, None)
        self._models.pop(task_id, None)
        self._tokens.pop(task_id, None)
        self._running.discard(task_id)
        self._discard_on_exit.discard(task_id)
        await self._persist()

    async def recover(self) -> list[DownloadTask]:
        if not self._state_path.exists():
            return []
        try:
            data = await asyncio.to_thread(self._read_state)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            return []
        recovered: list[DownloadTask] = []
        for entry in data["tasks"]:
            try:
                task = DownloadTask.from_dict(entry["task"])
                model = CatalogModel.from_dict(entry["model"])
                state = TaskState.PAUSED if task.state is TaskState.DOWNLOADING else task.state
                task = self._replace_task(
                    task,
                    state=state,
                    bytes_downloaded=self._partial_bytes(model, Path(task.destination)),
                    speed_bps=None,
                    eta_seconds=None,
                )
            except (KeyError, TypeError, ValueError, OSError):
                continue
            self._tasks[task.id] = task
            self._models[task.id] = model
            recovered.append(task)
            await self._emit(task)
        await self._persist()
        return recovered

    async def _download_file(
        self,
        task_id: str,
        model: CatalogModel,
        manifest: CatalogFile,
        token: CancelToken,
        started: float,
        starting_bytes: int,
    ) -> None:
        if model.source == "hf-mirror":
            await self._download_file_hf(task_id, model, manifest, token, started, starting_bytes)
            return
        destination = Path(self._tasks[task_id].destination)
        final_path = self._safe_file_path(destination, manifest.path)
        part_path = final_path.with_name(final_path.name + ".part")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink(final_path)
        self._reject_symlink(part_path)
        if final_path.exists() and await sha256_file(final_path) == manifest.sha256.lower():
            return
        offset = part_path.stat().st_size if part_path.exists() else 0
        if offset == manifest.size:
            if await sha256_file(part_path) == manifest.sha256.lower():
                self._reject_symlink(final_path)
                self._reject_symlink(part_path)
                os.replace(part_path, final_path)
                return
            part_path.unlink()
            offset = 0
        if offset > manifest.size:
            part_path.unlink()
            offset = 0
        stream = await self._transport.open(model, manifest.path, offset)
        append = (
            offset > 0
            and stream.status_code == 206
            and stream.range_start == offset
            and stream.range_total == manifest.size
        )
        if offset > 0 and stream.status_code == 206 and not append:
            await _close_stream(stream)
            stream = await self._transport.open(model, manifest.path, 0)
        if offset > 0 and not append:
            offset = 0
        mode = "ab" if append else "wb"
        completed_before = self._completed_bytes_before(model, destination, manifest.path)
        current = offset
        with part_path.open(mode) as handle:
            async for chunk in stream.chunks:
                token.check()
                # 逐块 flush() 是同步磁盘 IO，会阻塞事件循环；依赖文件缓冲，
                # 写满自动落盘，close 时统一 flush
                handle.write(chunk)
                current += len(chunk)
                elapsed = max(time.monotonic() - started, 1e-9)
                downloaded = max(self._tasks[task_id].bytes_downloaded, completed_before + current)
                speed = max(downloaded - starting_bytes, 0) / elapsed
                remaining = max(self._tasks[task_id].total_bytes - downloaded, 0)
                await self._update(
                    task_id,
                    bytes_downloaded=downloaded,
                    speed_bps=speed,
                    eta_seconds=remaining / speed if speed else None,
                )
                token.check()
        if current != manifest.size:
            raise IOError(f"size mismatch for {manifest.path}: expected {manifest.size}, got {current}")
        actual = await sha256_file(part_path)
        if actual != manifest.sha256.lower():
            quarantine = destination / ".quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            target = quarantine / f"{Path(manifest.path).name}.{uuid.uuid4().hex}.bad"
            self._reject_symlink(part_path)
            os.replace(part_path, target)
            raise HashMismatchError(manifest.path, manifest.sha256, actual)
        self._reject_symlink(final_path)
        self._reject_symlink(part_path)
        os.replace(part_path, final_path)

    async def _download_file_hf(
        self,
        task_id: str,
        model: CatalogModel,
        manifest: CatalogFile,
        token: CancelToken,
        started: float,
        starting_bytes: int,
    ) -> None:
        import os as _os
        from huggingface_hub import hf_hub_download

        _os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        _os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        _os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "0")

        destination = Path(self._tasks[task_id].destination)
        final_path = self._safe_file_path(destination, manifest.path)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink(final_path)
        if final_path.exists() and await sha256_file(final_path) == manifest.sha256.lower():
            return
        completed_before = self._completed_bytes_before(model, destination, manifest.path)
        loop = asyncio.get_running_loop()
        ProgressBar = _make_hf_progress_bar(
            token, task_id, self, completed_before, starting_bytes, started, loop,
        )
        try:
            await asyncio.to_thread(
                hf_hub_download,
                model.repository,
                manifest.path,
                revision=model.revision,
                local_dir=str(destination),
                force_download=False,
                token=None,
                tqdm_class=ProgressBar,
            )
        except KeyboardInterrupt:
            raise CancellationError("cancelled by user") from None
        actual = await sha256_file(final_path)
        if actual != manifest.sha256.lower():
            quarantine = destination / ".quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            target = quarantine / f"{Path(manifest.path).name}.{uuid.uuid4().hex}.bad"
            self._reject_symlink(final_path)
            os.replace(final_path, target)
            raise HashMismatchError(manifest.path, manifest.sha256, actual)

    async def _register(self, task_id: str, model: CatalogModel) -> None:
        checksum = hashlib.sha256(
            json.dumps(model.to_dict(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        installed = InstalledModel(
            id=model.id,
            catalog_id=model.id,
            revision=model.revision,
            purpose=model.purpose,
            directory=self._tasks[task_id].destination,
            manifest_checksum=checksum,
            validation_state="validated",
            ownership="user",
            installed_at=datetime.now(timezone.utc),
            metadata={
                "source": model.source,
                "repository": model.repository,
                "compatibility": dict(model.compatibility),
                "runtime_requirements": dict(model.runtime_requirements),
            },
        )
        await self._registry.register(installed)

    async def _update(self, task_id: str, **changes: Any) -> DownloadTask:
        task = self._replace_task(self._tasks[task_id], **changes)
        self._tasks[task_id] = task
        await self._persist()
        await self._emit(task)
        return task

    def _replace_task(self, task: DownloadTask, **changes: Any) -> DownloadTask:
        changes.setdefault("updated_at", datetime.now(timezone.utc))
        return replace(task, **changes)

    async def _emit(self, task: DownloadTask) -> None:
        if self._event_sink is None:
            return
        result = self._event_sink({"type": "local_ai_download_updated", "task": task.to_dict()})
        if inspect.isawaitable(result):
            await result

    async def _persist(self) -> None:
        await asyncio.to_thread(self._persist_sync)

    def _persist_sync(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tasks": [
                {"task": task.to_dict(), "model": self._models[task_id].to_dict()}
                for task_id, task in self._tasks.items()
            ]
        }
        temporary = self._state_path.with_name(
            f"{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self._replace_state(temporary)

    def _replace_state(self, temporary: Path) -> None:
        """原子替换状态文件，Windows 上 os.replace 覆盖被短暂占用的
        目标文件会抛 PermissionError（WinError 5）；复用 utils.atomic_write
        的带重试 replace（50ms→800ms 指数退避），失败时抛异常由调用方处理。"""
        from utils.atomic_write import _atomic_replace_with_retry

        _atomic_replace_with_retry(temporary, self._state_path)

    def _read_state(self) -> dict[str, Any]:
        return json.loads(self._state_path.read_text(encoding="utf-8"))

    def _require_task(self, task_id: str) -> DownloadTask:
        try:
            return self._tasks[task_id]
        except KeyError as error:
            raise KeyError(f"download task not found: {task_id}") from error

    @classmethod
    def _partial_bytes(cls, model: CatalogModel, destination: Path) -> int:
        total = 0
        for manifest in model.files:
            final_path = cls._safe_file_path(destination, manifest.path)
            part_path = final_path.with_name(final_path.name + ".part")
            cls._reject_symlink(final_path)
            cls._reject_symlink(part_path)
            if final_path.exists() and final_path.stat().st_size == manifest.size:
                total += manifest.size
            elif part_path.exists():
                total += min(part_path.stat().st_size, manifest.size)
        return total

    @classmethod
    def _completed_bytes_before(cls, model: CatalogModel, destination: Path, current_path: str) -> int:
        total = 0
        for manifest in model.files:
            if manifest.path == current_path:
                break
            final_path = cls._safe_file_path(destination, manifest.path)
            cls._reject_symlink(final_path)
            if final_path.exists() and final_path.stat().st_size == manifest.size:
                total += manifest.size
        return total

    @staticmethod
    def _safe_file_path(destination: Path, relative_path: str) -> Path:
        path = PurePosixPath(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe catalog file path: {relative_path}")
        result = destination.joinpath(*path.parts)
        resolved_parent = result.parent.resolve()
        resolved_destination = destination.resolve()
        if resolved_parent != resolved_destination and resolved_destination not in resolved_parent.parents:
            raise ValueError(f"catalog file escapes destination: {relative_path}")
        return result

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise ValueError(f"download path is a symbolic link: {path}")

    @classmethod
    def _discard_partials(cls, model: CatalogModel, destination: Path) -> None:
        for manifest in model.files:
            final_path = cls._safe_file_path(destination, manifest.path)
            part_path = final_path.with_name(final_path.name + ".part")
            cls._reject_symlink(part_path)
            part_path.unlink(missing_ok=True)


async def _close_stream(stream: Any) -> None:
    close = stream.close or getattr(stream.chunks, "aclose", None)
    if close is not None:
        await close()


class HashMismatchError(ValueError):
    def __init__(self, path: str, expected: str, actual: str) -> None:
        super().__init__(f"SHA256 mismatch for {path}: expected {expected}, got {actual}")