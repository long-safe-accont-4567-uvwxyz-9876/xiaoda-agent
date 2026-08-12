from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from local_ai.contracts import CatalogModel, InstalledModel, ModelPurpose, TaskState
from local_ai.downloads.manager import DownloadManager
from local_ai.downloads.transport import DownloadStream, HttpDownloadTransport


class FakeRegistry:
    def __init__(self) -> None:
        self.models: dict[str, InstalledModel] = {}

    async def register(self, model: InstalledModel) -> InstalledModel:
        self.models[model.id] = model
        return model

    async def get(self, model_id: str) -> InstalledModel | None:
        return self.models.get(model_id)


class StrictRegistry(FakeRegistry):
    async def register(self, model: InstalledModel) -> InstalledModel:
        if model.id in self.models:
            raise ValueError("duplicate registration")
        return await super().register(model)


class FakeTransport:
    def __init__(self, files: dict[str, bytes], *, ignore_range: bool = False) -> None:
        self.files = files
        self.ignore_range = ignore_range
        self.requests: list[tuple[str, int]] = []
        self.gate: asyncio.Event | None = None

    async def open(self, model: CatalogModel, path: str, offset: int) -> DownloadStream:
        self.requests.append((path, offset))
        content = self.files[path]
        status_code = 200 if offset == 0 or self.ignore_range else 206
        body = content if status_code == 200 else content[offset:]

        async def chunks():
            for index in range(0, len(body), 2):
                if self.gate is not None:
                    await self.gate.wait()
                yield body[index:index + 2]

        return DownloadStream(
            status_code=status_code,
            total_size=len(content),
            chunks=chunks(),
            range_start=offset if status_code == 206 else None,
            range_total=len(content) if status_code == 206 else None,
        )


def make_model(content: bytes, *, sha256: str | None = None) -> CatalogModel:
    return CatalogModel(
        id="model:test",
        source="modelscope",
        repository="owner/model",
        revision="abcdef0",
        purpose=ModelPurpose.EMBEDDING,
        files=(
            {
                "path": "nested/model.onnx",
                "size": len(content),
                "sha256": sha256 or hashlib.sha256(content).hexdigest(),
            },
        ),
        download_size=len(content),
    )


def make_manager(tmp_path: Path, transport: FakeTransport, registry: FakeRegistry, events: list[dict]):
    async def emit(event: dict) -> None:
        events.append(event)

    return DownloadManager(
        registry=registry,
        transport=transport,
        event_sink=emit,
        state_path=tmp_path / "downloads.json",
    )


@pytest.mark.asyncio
async def test_resume_uses_range_from_existing_part(tmp_path):
    content = b"first-second"
    destination = tmp_path / "model"
    part = destination / "nested" / "model.onnx.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"first")
    transport = FakeTransport({"nested/model.onnx": content})
    manager = make_manager(tmp_path, transport, FakeRegistry(), [])
    task = manager.create(make_model(content), destination)

    completed = await manager.start(task.id)

    assert transport.requests == [("nested/model.onnx", 5)]
    assert completed.state is TaskState.COMPLETED
    assert (destination / "nested" / "model.onnx").read_bytes() == content


@pytest.mark.asyncio
async def test_range_ignored_restarts_without_duplicate_bytes(tmp_path):
    content = b"complete"
    destination = tmp_path / "model"
    part = destination / "nested" / "model.onnx.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"comp")
    transport = FakeTransport({"nested/model.onnx": content}, ignore_range=True)
    events: list[dict] = []
    manager = make_manager(tmp_path, transport, FakeRegistry(), events)
    task = manager.create(make_model(content), destination)

    completed = await manager.start(task.id)

    assert completed.bytes_downloaded == len(content)
    assert (destination / "nested" / "model.onnx").read_bytes() == content
    downloaded = [event["task"]["bytes_downloaded"] for event in events]
    assert downloaded == sorted(downloaded)


@pytest.mark.asyncio
async def test_progress_events_contain_full_download_task(tmp_path):
    content = b"progress"
    events: list[dict] = []
    manager = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        FakeRegistry(),
        events,
    )
    task = manager.create(make_model(content), tmp_path / "model")

    await manager.start(task.id)

    assert events
    assert all(event["type"] == "local_ai_download_updated" for event in events)
    assert all(set(task.to_dict()) <= set(event["task"]) for event in events)
    downloaded = [event["task"]["bytes_downloaded"] for event in events]
    assert downloaded == sorted(downloaded)
    assert events[-1]["task"]["state"] == "completed"


@pytest.mark.asyncio
async def test_pause_preserves_partial_and_resume_completes(tmp_path):
    content = b"pause-resume"
    transport = FakeTransport({"nested/model.onnx": content})
    transport.gate = asyncio.Event()
    manager = make_manager(tmp_path, transport, FakeRegistry(), [])
    task = manager.create(make_model(content), tmp_path / "model")
    part = tmp_path / "model" / "nested" / "model.onnx.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"pau")
    running = asyncio.create_task(manager.start(task.id))
    await asyncio.sleep(0)

    paused = await manager.pause(task.id)
    transport.gate.set()
    await running

    assert paused.state is TaskState.PAUSED
    assert part.read_bytes() == b"pau"
    completed = await manager.resume(task.id)
    assert completed.state is TaskState.COMPLETED


@pytest.mark.asyncio
async def test_cancel_can_keep_or_discard_partials(tmp_path):
    content = b"cancel-me"
    destination = tmp_path / "model"
    transport = FakeTransport({"nested/model.onnx": content})
    manager = make_manager(tmp_path, transport, FakeRegistry(), [])
    task = manager.create(make_model(content), destination)
    part = destination / "nested" / "model.onnx.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"can")

    kept = await manager.cancel(task.id)
    assert kept.state is TaskState.CANCELLED
    assert part.exists()

    discarded = await manager.cancel(task.id, discard_partials=True)
    assert discarded.state is TaskState.CANCELLED
    assert not part.exists()


@pytest.mark.asyncio
async def test_recover_turns_interrupted_download_into_paused_task(tmp_path):
    content = b"restart"
    destination = tmp_path / "model"
    first = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        FakeRegistry(),
        [],
    )
    task = first.create(make_model(content), destination)
    first._tasks[task.id] = first._replace_task(task, state=TaskState.DOWNLOADING)
    await first._persist()
    part = destination / "nested" / "model.onnx.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"res")

    restarted = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        FakeRegistry(),
        [],
    )
    recovered = await restarted.recover()

    assert recovered[0].id == task.id
    assert recovered[0].state is TaskState.PAUSED
    assert recovered[0].bytes_downloaded == 3
    completed = await restarted.resume(task.id)
    assert completed.state is TaskState.COMPLETED


@pytest.mark.asyncio
async def test_recover_isolates_corrupt_state_file(tmp_path):
    state_path = tmp_path / "downloads.json"
    corrupt = b'{"tasks": ['
    state_path.write_bytes(corrupt)
    manager = make_manager(
        tmp_path,
        FakeTransport({}),
        FakeRegistry(),
        [],
    )

    recovered = await manager.recover()

    assert recovered == []
    assert manager.list() == []
    assert state_path.read_bytes() == corrupt


@pytest.mark.asyncio
async def test_recover_isolates_corrupt_entry_and_keeps_valid_tasks(tmp_path):
    content = b"restart"
    first = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        FakeRegistry(),
        [],
    )
    valid = first.create(make_model(content), tmp_path / "model")
    state_path = tmp_path / "downloads.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["tasks"].insert(0, {"task": {"id": "broken"}, "model": {}})
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    restarted = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        FakeRegistry(),
        [],
    )

    recovered = await restarted.recover()

    assert [task.id for task in recovered] == [valid.id]
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert [entry["task"]["id"] for entry in persisted["tasks"]] == [valid.id]


@pytest.mark.asyncio
async def test_hash_mismatch_quarantines_file_and_never_registers_model(tmp_path):
    content = b"corrupt"
    registry = FakeRegistry()
    manager = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        registry,
        [],
    )
    task = manager.create(make_model(content, sha256="0" * 64), tmp_path / "model")

    quarantined = await manager.start(task.id)

    assert quarantined.state is TaskState.QUARANTINED
    assert await registry.get(task.model_id) is None
    assert not (tmp_path / "model" / "nested" / "model.onnx").exists()
    assert list((tmp_path / "model" / ".quarantine").iterdir())


@pytest.mark.asyncio
async def test_success_registers_only_after_atomic_final_file_exists(tmp_path):
    content = b"verified"
    destination = tmp_path / "model"

    class InspectingRegistry(FakeRegistry):
        async def register(self, model: InstalledModel) -> InstalledModel:
            assert (destination / "nested" / "model.onnx").read_bytes() == content
            assert not (destination / "nested" / "model.onnx.part").exists()
            return await super().register(model)

    registry = InspectingRegistry()
    manager = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        registry,
        [],
    )
    task = manager.create(make_model(content), destination)

    completed = await manager.start(task.id)

    installed = await registry.get(task.model_id)
    assert completed.state is TaskState.COMPLETED
    assert installed is not None
    assert installed.directory == str(destination.resolve())


@pytest.mark.asyncio
async def test_part_symlink_cannot_write_outside_destination(tmp_path):
    content = b"verified"
    destination = tmp_path / "model"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    part = destination / "nested" / "model.onnx.part"
    part.parent.mkdir(parents=True)
    part.symlink_to(outside)
    registry = FakeRegistry()
    manager = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        registry,
        [],
    )
    with pytest.raises(ValueError, match="symbolic link"):
        manager.create(make_model(content), destination)

    assert outside.read_bytes() == b"outside"
    assert await registry.get("model:test") is None


@pytest.mark.asyncio
async def test_final_symlink_cannot_be_registered(tmp_path):
    content = b"verified"
    destination = tmp_path / "model"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(content)
    final = destination / "nested" / "model.onnx"
    final.parent.mkdir(parents=True)
    final.symlink_to(outside)
    registry = FakeRegistry()
    manager = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        registry,
        [],
    )
    with pytest.raises(ValueError, match="symbolic link"):
        manager.create(make_model(content), destination)

    assert final.is_symlink()
    assert await registry.get("model:test") is None


def test_parent_symlink_is_rejected_before_partial_accounting(tmp_path):
    content = b"verified"
    destination = tmp_path / "model"
    outside = tmp_path / "outside"
    outside.mkdir()
    destination.mkdir()
    (destination / "nested").symlink_to(outside, target_is_directory=True)
    manager = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        FakeRegistry(),
        [],
    )

    with pytest.raises(ValueError, match="escapes destination"):
        manager.create(make_model(content), destination)


@pytest.mark.asyncio
async def test_discard_rejects_part_symlink_without_touching_target(tmp_path):
    content = b"verified"
    destination = tmp_path / "model"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    manager = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        FakeRegistry(),
        [],
    )
    task = manager.create(make_model(content), destination)
    part = destination / "nested" / "model.onnx.part"
    part.parent.mkdir(parents=True)
    part.symlink_to(outside)

    with pytest.raises(ValueError, match="symbolic link"):
        await manager.cancel(task.id, discard_partials=True)

    assert part.is_symlink()
    assert outside.read_bytes() == b"outside"


@pytest.mark.asyncio
async def test_concurrent_start_is_idempotent(tmp_path):
    content = b"concurrent"
    registry = StrictRegistry()
    manager = make_manager(
        tmp_path,
        FakeTransport({"nested/model.onnx": content}),
        registry,
        [],
    )
    task = manager.create(make_model(content), tmp_path / "model")

    first, second = await asyncio.gather(manager.start(task.id), manager.start(task.id))

    assert first.state is TaskState.COMPLETED
    assert second.state is TaskState.COMPLETED
    assert manager._tasks[task.id].state is TaskState.COMPLETED
    assert list(registry.models) == [task.model_id]


@pytest.mark.asyncio
async def test_complete_partial_finishes_without_range_request(tmp_path):
    content = b"complete"
    destination = tmp_path / "model"
    part = destination / "nested" / "model.onnx.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(content)
    transport = FakeTransport({"nested/model.onnx": content})
    manager = make_manager(tmp_path, transport, FakeRegistry(), [])
    task = manager.create(make_model(content), destination)

    completed = await manager.start(task.id)

    assert completed.state is TaskState.COMPLETED
    assert transport.requests == []
    assert (destination / "nested" / "model.onnx").read_bytes() == content


@pytest.mark.asyncio
async def test_mismatched_content_range_restarts_from_zero(tmp_path):
    content = b"complete"
    destination = tmp_path / "model"
    part = destination / "nested" / "model.onnx.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"comp")

    class MismatchedRangeTransport(FakeTransport):
        def __init__(self, files: dict[str, bytes]) -> None:
            super().__init__(files)
            self.mismatched_closed = False

        async def open(self, model: CatalogModel, path: str, offset: int) -> DownloadStream:
            stream = await super().open(model, path, offset)
            if offset:
                async def close() -> None:
                    self.mismatched_closed = True

                return DownloadStream(
                    status_code=206,
                    total_size=stream.total_size,
                    chunks=stream.chunks,
                    range_start=offset + 1,
                    range_total=stream.range_total,
                    close=close,
                )
            return stream

    transport = MismatchedRangeTransport({"nested/model.onnx": content})
    manager = make_manager(tmp_path, transport, FakeRegistry(), [])
    task = manager.create(make_model(content), destination)

    completed = await manager.start(task.id)

    assert completed.state is TaskState.COMPLETED
    assert transport.requests == [("nested/model.onnx", 4), ("nested/model.onnx", 0)]
    assert transport.mismatched_closed
    assert (destination / "nested" / "model.onnx").read_bytes() == content


@pytest.mark.asyncio
async def test_http_transport_closes_error_response():
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(416, request=request)
        responses.append(response)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HttpDownloadTransport(client=client)
        with pytest.raises(httpx.HTTPStatusError):
            await transport.open(make_model(b"complete"), "nested/model.onnx", 8)

    assert responses[0].is_closed
