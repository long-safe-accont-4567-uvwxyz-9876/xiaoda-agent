from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from inspect import isawaitable
from typing import Any
from uuid import uuid4

from local_ai.contracts import (
    CatalogFile,
    CatalogModel,
    DeviceState,
    InstalledModel,
    ModelInstance,
    RuntimeProfile,
)
from local_ai.models.registry import ModelNotFoundError, ModelRegistry
from local_ai.runtimes.registry import RuntimeAdapter, RuntimeRegistry


class InstanceNotFoundError(ValueError):
    pass


class InstanceInUseError(RuntimeError):
    pass


class InstanceManager:
    def __init__(
        self,
        model_registry: ModelRegistry,
        device_registry: Any,
        runtime_registry: RuntimeRegistry,
        *,
        database: Any | None = None,
        catalog_resolver: Callable[[InstalledModel], CatalogModel] | None = None,
    ) -> None:
        self._model_registry = model_registry
        self._device_registry = device_registry
        self._runtime_registry = runtime_registry
        self._database = database
        self.catalog_resolver = catalog_resolver
        self._instances: dict[str, ModelInstance] = {}
        self._runtimes: dict[str, RuntimeAdapter] = {}
        self._model_instances: dict[str, str] = {}
        self._model_locks: dict[str, asyncio.Lock] = {}
        self._stopping_instances: set[str] = set()
        self._shutdown_lock = asyncio.Lock()
        self._shutting_down = False
        self._shutdown_completed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._lifecycle_tasks: set[asyncio.Task[Any]] = set()
        self._model_tasks: dict[str, set[asyncio.Task[Any]]] = {}

    @property
    def active_count(self) -> int:
        return len(self._instances)

    async def start(
        self,
        model_id: str,
        backend_override: str | None = None,
    ) -> ModelInstance:
        lock = self._model_locks.setdefault(model_id, asyncio.Lock())
        async with lock:
            if self._shutting_down:
                raise RuntimeError("instance manager is shutting down")
            existing_id = self._model_instances.get(model_id)
            if existing_id is not None:
                return self._instances[existing_id]
            installed = await self._model_registry.get(model_id)
            if installed is None:
                raise ModelNotFoundError(f"model not found: {model_id!r}")
            catalog = await self._catalog_model(installed)
            profile = self._device_registry.recommend(catalog, backend_override)
            profile = self._runtime_profile(installed, profile)
            runtime = self._runtime_registry.create(profile)
            try:
                started = await self._run_sync(model_id, runtime.start, profile)
                if not started:
                    raise RuntimeError(f"runtime failed to start model {model_id!r}")
                now = datetime.now(timezone.utc)
                instance = ModelInstance(
                    id=f"instance:{uuid4().hex}",
                    model_id=model_id,
                    runtime=profile.runtime,
                    device_id=profile.device_id,
                    state="running",
                    health="healthy",
                    started_at=now,
                    updated_at=now,
                )
                self._instances[instance.id] = instance
                self._runtimes[instance.id] = runtime
                self._model_instances[model_id] = instance.id
                return instance
            except BaseException as start_error:
                try:
                    await self._run_sync(model_id, runtime.stop)
                except BaseException as rollback_error:
                    if isinstance(start_error, asyncio.CancelledError):
                        raise start_error
                    if isinstance(rollback_error, asyncio.CancelledError):
                        raise rollback_error
                    group_type = (
                        ExceptionGroup
                        if isinstance(start_error, Exception)
                        and isinstance(rollback_error, Exception)
                        else BaseExceptionGroup
                    )
                    raise group_type(
                        "runtime start and rollback failed",
                        [start_error, rollback_error],
                    ) from start_error
                raise

    async def stop(self, instance_id: str) -> None:
        instance = self._instances.get(instance_id)
        if instance is None:
            return
        lock = self._model_locks.setdefault(instance.model_id, asyncio.Lock())
        async with lock:
            current = self._instances.get(instance_id)
            if current is None:
                return
            if current.active_routes:
                raise InstanceInUseError(
                    f"instance {instance_id!r} is used by routes {current.active_routes!r}"
                )
            await self._stop_locked(current)

    def get(self, instance_id: str) -> ModelInstance | None:
        return self._instances.get(instance_id)

    def list(self) -> list[ModelInstance]:
        return list(self._instances.values())

    def bind_route(self, instance_id: str, route: str) -> ModelInstance:
        if instance_id in self._stopping_instances:
            raise InstanceInUseError(f"instance {instance_id!r} is stopping")
        instance = self._required_instance(instance_id)
        routes = tuple(dict.fromkeys((*instance.active_routes, route)))
        updated = replace(instance, active_routes=routes, updated_at=self._now())
        self._instances[instance_id] = updated
        return updated

    def unbind_route(self, instance_id: str, route: str) -> ModelInstance:
        instance = self._required_instance(instance_id)
        routes = tuple(item for item in instance.active_routes if item != route)
        updated = replace(instance, active_routes=routes, updated_at=self._now())
        self._instances[instance_id] = updated
        return updated

    async def refresh_health(self) -> list[ModelInstance]:
        devices = {
            device.id: device for device in self._device_registry.scan(force=True)
        }
        for instance_id, instance in tuple(self._instances.items()):
            lock = self._model_locks.setdefault(instance.model_id, asyncio.Lock())
            async with lock:
                current = self._instances.get(instance_id)
                runtime = self._runtimes.get(instance_id)
                if current is None or runtime is None:
                    continue
                device = devices.get(current.device_id)
                if device is None or device.state is not DeviceState.AVAILABLE:
                    state, health = "degraded", "device_unavailable"
                elif await self._run_sync(current.model_id, runtime.health):
                    state, health = "running", "healthy"
                else:
                    state, health = "degraded", "unhealthy"
                if not self._shutting_down:
                    self._instances[instance_id] = replace(
                        current,
                        state=state,
                        health=health,
                        updated_at=self._now(),
                    )
        return self.list()

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_completed:
                shutdown_task = self._shutdown_task
            elif self._shutdown_task is None:
                self._shutting_down = True
                self._shutdown_task = asyncio.create_task(self._shutdown())
                shutdown_task = self._shutdown_task
            else:
                shutdown_task = self._shutdown_task
        if shutdown_task is None:
            raise RuntimeError("instance manager shutdown state is invalid")
        await self._await_completion(shutdown_task)

    async def _shutdown(self) -> None:
        errors: list[Exception] = []
        try:
            for lock in tuple(self._model_locks.values()):
                await lock.acquire()
                lock.release()
            for instance in tuple(self._instances.values()):
                lock = self._model_locks.setdefault(instance.model_id, asyncio.Lock())
                async with lock:
                    current = self._instances.get(instance.id)
                    if current is None:
                        continue
                    try:
                        await self._stop_locked(current)
                    except Exception as error:
                        errors.append(error)
            await self._await_lifecycle_tasks()
            if self._database is not None:
                try:
                    await self._database.close()
                except Exception as error:
                    errors.append(error)
            if errors:
                raise ExceptionGroup("instance manager shutdown failed", errors)
        finally:
            self._shutdown_completed = True

    async def _stop_locked(self, instance: ModelInstance) -> None:
        runtime = self._runtimes[instance.id]
        self._stopping_instances.add(instance.id)
        try:
            await self._run_sync(instance.model_id, runtime.stop)
        finally:
            self._stopping_instances.discard(instance.id)
            self._instances.pop(instance.id, None)
            self._runtimes.pop(instance.id, None)
            self._model_instances.pop(instance.model_id, None)

    async def _run_sync(
        self,
        model_id: str,
        function: Callable[..., Any],
        *args: Any,
    ) -> Any:
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._lifecycle_tasks.add(task)
        model_tasks = self._model_tasks.setdefault(model_id, set())
        model_tasks.add(task)
        task.add_done_callback(lambda completed: self._discard_task(model_id, completed))
        return await self._await_completion(task)

    def _discard_task(self, model_id: str, task: asyncio.Task[Any]) -> None:
        self._lifecycle_tasks.discard(task)
        model_tasks = self._model_tasks.get(model_id)
        if model_tasks is None:
            return
        model_tasks.discard(task)
        if not model_tasks:
            self._model_tasks.pop(model_id, None)

    async def _await_lifecycle_tasks(self) -> None:
        while self._lifecycle_tasks:
            tasks = tuple(self._lifecycle_tasks)
            for task in tasks:
                try:
                    await self._await_completion(task, propagate_cancel=False)
                except Exception:
                    pass

    @staticmethod
    async def _await_completion(
        task: asyncio.Task[Any],
        *,
        propagate_cancel: bool = True,
    ) -> Any:
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    result = task.result()
                    if propagate_cancel:
                        raise
                    return result
                cancelled = True
            except Exception:
                if cancelled and propagate_cancel:
                    raise asyncio.CancelledError from None
                raise
            else:
                if cancelled and propagate_cancel:
                    raise asyncio.CancelledError
                return result

    async def _catalog_model(self, installed: InstalledModel) -> CatalogModel:
        if self.catalog_resolver is not None:
            resolved = self.catalog_resolver(installed)
            if isawaitable(resolved):
                resolved = await resolved
            return resolved
        metadata = dict(installed.metadata)
        compatibility = metadata.get("compatibility", {})
        requirements = metadata.get("runtime_requirements", {})
        return CatalogModel(
            id=installed.catalog_id,
            source=str(metadata.get("source", "installed")),
            repository=str(metadata.get("repository", installed.catalog_id)),
            revision=installed.revision,
            purpose=installed.purpose,
            files=(CatalogFile(path="manifest", size=1, sha256="0" * 64),),
            download_size=1,
            compatibility=compatibility,
            runtime_requirements=requirements,
        )

    @staticmethod
    def _runtime_profile(
        installed: InstalledModel,
        profile: RuntimeProfile,
    ) -> RuntimeProfile:
        return replace(
            profile,
            options={
                **profile.options,
                "model_dir": installed.directory,
                "model_id": installed.id,
                "purpose": installed.purpose.value,
            },
        )

    def _required_instance(self, instance_id: str) -> ModelInstance:
        instance = self.get(instance_id)
        if instance is None:
            raise InstanceNotFoundError(f"instance not found: {instance_id!r}")
        return instance

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


__all__ = [
    "InstanceInUseError",
    "InstanceManager",
    "InstanceNotFoundError",
]
