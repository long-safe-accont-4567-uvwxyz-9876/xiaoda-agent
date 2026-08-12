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

    async def start(self, task_id: str) -> DownloadTask:
        self._require_task(task_id)
        lock = self._locks.setdefault(task_id, asyncio.Lock())
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
                return self._tasks[task_id]
            except HashMismatchError as error:
                return await self._update(
                    task_id,
                    state=TaskState.QUARANTINED,
                    speed_bps=None,
                    eta_seconds=None,
                    error=str(error),
                )
            except Exception as error:
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

    async def pause(self, task_id: str) -> DownloadTask:
        self._require_task(task_id)
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
        token = self._tokens.get(task_id)
        if token is not None:
            token.cancel("cancel")
        if discard_partials:
            self._discard_partials(self._models[task_id], Path(task.destination))
        return await self._update(
            task_id,
            state=TaskState.CANCELLED,
            bytes_downloaded=self._partial_bytes(self._models[task_id], Path(task.destination)),
            speed_bps=None,
            eta_seconds=None,
        )

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
                handle.write(chunk)
                handle.flush()
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
            metadata={"source": model.source, "repository": model.repository},
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
        os.replace(temporary, self._state_path)

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
