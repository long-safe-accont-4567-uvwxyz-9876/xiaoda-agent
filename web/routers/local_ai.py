from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from local_ai.catalog.curated import CatalogLoader
from local_ai.devices.registry import DeviceRegistry
from local_ai.downloads.manager import DownloadManager
from local_ai.instances.manager import InstanceManager
from local_ai.models.registry import ModelRegistry
from local_ai.models.storage import StoragePolicy
from local_ai.runtimes.registry import RuntimeRegistry
from web.routers.auth import get_current_user
from web.schemas import Envelope
from web.ws_hub import local_ai_event

router = APIRouter(tags=["local-ai"], dependencies=[Depends(get_current_user)])


class DownloadRequest(BaseModel):
    model_id: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)


class CancelRequest(BaseModel):
    discard_partials: bool = False


class StartInstanceRequest(BaseModel):
    model_id: str = Field(min_length=1)
    device_id: str | None = None
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)


@dataclass
class LocalAIServices:
    devices: DeviceRegistry
    catalog: CatalogLoader
    models: ModelRegistry
    downloads: DownloadManager
    instances: InstanceManager
    broadcast: Any
    storage_policy: StoragePolicy = field(default_factory=StoragePolicy)
    request_results: dict[tuple[str, str], Any] = field(default_factory=dict)
    request_inputs: dict[tuple[str, str], tuple[Any, ...]] = field(default_factory=dict)
    background_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def shutdown(self) -> None:
        for task in tuple(self.background_tasks):
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        await self.instances.shutdown()


def local_ai_event_sink(broadcast: Any) -> Any:
    async def emit(event: dict[str, Any]) -> None:
        payload = event.get("download", event.get("task"))
        await broadcast(local_ai_event("download", payload))

    return emit


def create_local_ai_services(core: Any, broadcast: Any, state_path: str | Path) -> LocalAIServices:
    devices = DeviceRegistry()
    catalog = CatalogLoader()
    models = ModelRegistry(core.db)
    downloads = DownloadManager(
        models,
        event_sink=local_ai_event_sink(broadcast),
        state_path=state_path,
    )
    instances = getattr(core, "local_ai_instances", None)
    if instances is None:
        instances = InstanceManager(models, devices, RuntimeRegistry())
        core.local_ai_instances = instances
    return LocalAIServices(devices, catalog, models, downloads, instances, broadcast)


def attach_local_ai_services(
    app: Any,
    core: Any,
    broadcast: Any,
    state_path: str | Path,
) -> LocalAIServices:
    services = create_local_ai_services(core, broadcast, state_path)
    app.state.local_ai = services
    return services


async def initialize_local_ai_services(
    app: Any,
    core: Any,
    broadcast: Any,
    state_path: str | Path,
) -> LocalAIServices:
    services = attach_local_ai_services(app, core, broadcast, state_path)
    await services.downloads.recover()
    return services


def _services(request: Request) -> Any:
    services = getattr(request.app.state, "local_ai", None)
    if services is None:
        raise HTTPException(status_code=503, detail="Local AI services are unavailable")
    if not hasattr(services, "request_results"):
        services.request_results = {}
    if not hasattr(services, "request_inputs"):
        services.request_inputs = {}
    if not hasattr(services, "spawn"):
        services.spawn = lambda coroutine: asyncio.create_task(coroutine)
    return services


def _records(items: Any) -> list[dict[str, Any]]:
    return [item.to_dict() for item in items]


def _catalog_model(services: Any, model_id: str) -> Any:
    model = next(
        (
            item
            for item in services.catalog.filter(None, None, True)
            if item.id == model_id
        ),
        None,
    )
    if model is None:
        raise HTTPException(status_code=404, detail=f"Catalog model not found: {model_id}")
    return model


async def _start_instance(services: Any, request_id: str, model_id: str, device_id: str | None) -> None:
    key = ("instance", request_id)
    try:
        instance = await services.instances.start(model_id, device_id)
        services.request_results[key] = instance
        await services.broadcast(
            {"type": "local_ai_instance_updated", "instance": instance.to_dict()}
        )
    except Exception as error:
        services.request_results[key] = error
        await services.broadcast({
            "type": "local_ai_instance_updated",
            "request_id": request_id,
            "model_id": model_id,
            "operation": "start",
            "status": "failed",
            "error": {
                "code": "instance_start_failed",
                "message": str(error),
                "retryable": True,
            },
        })


@router.get("/local-ai/devices", response_model=Envelope[list[dict[str, Any]]])
async def list_devices(request: Request) -> Any:
    return Envelope(data=_records(_services(request).devices.scan()))


@router.post("/local-ai/devices/rescan", response_model=Envelope[list[dict[str, Any]]])
async def rescan_devices(request: Request) -> Any:
    services = _services(request)
    devices = services.devices.scan(force=True)
    for device in devices:
        await services.broadcast(
            {"type": "local_ai_device_updated", "device": device.to_dict()}
        )
    return Envelope(data=_records(devices))


@router.get("/local-ai/catalog", response_model=Envelope[list[dict[str, Any]]])
async def list_catalog(
    request: Request,
    purpose: str | None = None,
    max_download_bytes: int | None = None,
    advanced: bool = False,
) -> Any:
    try:
        models = _services(request).catalog.filter(purpose, max_download_bytes, advanced)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Envelope(data=_records(models))


@router.get("/local-ai/models", response_model=Envelope[list[dict[str, Any]]])
async def list_models(request: Request) -> Any:
    return Envelope(data=_records(await _services(request).models.list()))


@router.delete("/local-ai/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_model(model_id: str, request: Request) -> Response:
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(status_code=400, detail="缺少 X-Confirm: yes 确认头")
    try:
        await _services(request).models.remove(model_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/local-ai/downloads", response_model=Envelope[list[dict[str, Any]]])
async def list_downloads(request: Request) -> Any:
    return Envelope(data=_records(_services(request).downloads.list()))


@router.post(
    "/local-ai/downloads",
    response_model=Envelope[dict[str, Any]],
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_download(body: DownloadRequest, request: Request) -> Any:
    services = _services(request)
    model = _catalog_model(services, body.model_id)
    validation = services.storage_policy.validate_destination(
        body.destination,
        model.download_size,
    )
    if not validation.writable:
        raise HTTPException(
            status_code=422,
            detail=validation.reason or validation.error or "invalid download destination",
        )
    key = ("download", body.request_id)
    request_input = (body.model_id, validation.path)
    existing = services.request_results.get(key)
    if existing is not None:
        if services.request_inputs.get(key) != request_input:
            raise HTTPException(status_code=409, detail="request_id conflicts with a different download")
        return Envelope(data={"task": existing.to_dict()})
    task = services.downloads.create(
        model,
        validation.path,
    )
    services.request_results[key] = task
    services.request_inputs[key] = request_input
    services.spawn(services.downloads.start(task.id))
    return Envelope(data={"task": task.to_dict()})


@router.post("/local-ai/downloads/{task_id}/pause", response_model=Envelope[dict[str, Any]])
async def pause_download(task_id: str, request: Request) -> Any:
    try:
        task = await _services(request).downloads.pause(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Envelope(data=task.to_dict())


@router.post("/local-ai/downloads/{task_id}/resume", response_model=Envelope[dict[str, Any]])
async def resume_download(task_id: str, request: Request) -> Any:
    try:
        task = await _services(request).downloads.resume(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Envelope(data=task.to_dict())


@router.post("/local-ai/downloads/{task_id}/cancel", response_model=Envelope[dict[str, Any]])
async def cancel_download(task_id: str, body: CancelRequest, request: Request) -> Any:
    try:
        task = await _services(request).downloads.cancel(task_id, body.discard_partials)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Envelope(data=task.to_dict())


@router.get("/local-ai/instances", response_model=Envelope[list[dict[str, Any]]])
async def list_instances(request: Request) -> Any:
    return Envelope(data=_records(_services(request).instances.list()))


@router.get("/local-ai/instances/tasks/{task_id}", response_model=Envelope[dict[str, Any]])
async def get_instance_task(task_id: str, request: Request) -> Any:
    result = _services(request).request_results.get(("instance", task_id))
    if result is None:
        raise HTTPException(status_code=404, detail=f"Instance task not found: {task_id}")
    if isinstance(result, Exception):
        return Envelope(data={
            "task_id": task_id,
            "status": "failed",
            "error": {
                "code": "instance_start_failed",
                "message": str(result),
                "retryable": True,
            },
        })
    if hasattr(result, "to_dict"):
        return Envelope(data={
            "task_id": task_id,
            "status": "completed",
            "instance": result.to_dict(),
        })
    return Envelope(data={"task_id": task_id, "status": "pending"})


@router.post(
    "/local-ai/instances",
    response_model=Envelope[dict[str, Any]],
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_instance(body: StartInstanceRequest, request: Request) -> Any:
    services = _services(request)
    key = ("instance", body.request_id)
    request_input = (body.model_id, body.device_id)
    existing = services.request_results.get(key)
    if existing is None:
        services.request_results[key] = body.request_id
        services.request_inputs[key] = request_input
        services.spawn(
            _start_instance(
                services,
                body.request_id,
                body.model_id,
                body.device_id,
            )
        )
        return Envelope(data={"task_id": body.request_id})
    if services.request_inputs.get(key) != request_input:
        raise HTTPException(status_code=409, detail="request_id conflicts with a different instance start")
    if hasattr(existing, "to_dict"):
        return Envelope(data={"task_id": body.request_id, "instance": existing.to_dict()})
    return Envelope(data={"task_id": body.request_id})


@router.post("/local-ai/instances/{instance_id}/stop", response_model=Envelope[dict[str, Any]])
async def stop_instance(instance_id: str, request: Request) -> Any:
    services = _services(request)
    instance = services.instances.get(instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"Instance not found: {instance_id}")
    try:
        await services.instances.stop(instance_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    stopped = {**instance.to_dict(), "state": "stopped", "health": "stopped"}
    await services.broadcast(
        {"type": "local_ai_instance_updated", "instance": stopped}
    )
    return Envelope(data=stopped)
