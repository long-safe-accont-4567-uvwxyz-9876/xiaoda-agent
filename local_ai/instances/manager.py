from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from inspect import isawaitable
from typing import Any
from uuid import uuid4

from loguru import logger

from local_ai.contracts import (
    CatalogFile,
    CatalogModel,
    DeviceState,
    InstalledModel,
    ModelInstance,
    ModelPurpose,
)
from local_ai.models.registry import ModelNotFoundError, ModelRegistry
from local_ai.runtimes.base import RuntimeValidationError
from local_ai.runtimes.registry import RuntimeAdapter, RuntimeRegistry


class InstanceNotFoundError(ValueError):
    pass


class InstanceInUseError(RuntimeError):
    pass


class _BenchmarkCancelToken:
    """测速用无操作取消令牌：ORT GenAI stream 需要的 cancel_token 协议。"""

    def is_cancelled(self) -> bool:
        return False

    def check(self) -> None:
        return


def _norm_model_id(model_id: Any) -> str:
    """规范化模型 id：去掉内置/本地的 id 前缀（builtin:/local:），用于匹配展示名。"""
    if isinstance(model_id, str) and ":" in model_id:
        prefix, name = model_id.split(":", 1)
        if prefix in ("builtin", "local"):
            return name
    return str(model_id)


class InstanceManager:
    def __init__(
        self,
        model_registry: ModelRegistry,
        device_registry: Any,
        runtime_registry: RuntimeRegistry,
        *,
        database: Any | None = None,
        owns_database: bool = False,
        catalog_resolver: Callable[[InstalledModel], CatalogModel] | None = None,
    ) -> None:
        self._model_registry = model_registry
        self._device_registry = device_registry
        self._runtime_registry = runtime_registry
        self._database = database
        self._owns_database = owns_database
        self._database_closed = False
        self.catalog_resolver = catalog_resolver
        self._instances: dict[str, ModelInstance] = {}
        self._runtimes: dict[str, RuntimeAdapter] = {}
        self._model_instances: dict[str, str] = {}
        self._instance_purposes: dict[str, ModelPurpose] = {}
        self._selected_instances: dict[ModelPurpose, str] = {}
        self._selection_generations: dict[ModelPurpose, int] = {}
        self._route_bindings: dict[tuple[str, str], int] = {}
        self._model_locks: dict[str, asyncio.Lock] = {}
        self._stopping_instances: set[str] = set()
        self._shutdown_lock = asyncio.Lock()
        self._shutting_down = False
        self._shutdown_completed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._lifecycle_tasks: set[asyncio.Task[Any]] = set()
        self._model_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._pending_cleanup: dict[str, tuple[str, RuntimeAdapter]] = {}
        self._state_lock = threading.RLock()

    @property
    def active_count(self) -> int:
        with self._state_lock:
            return len(self._instances)

    def model_in_use(self, model_id: str) -> bool:
        """Return True if a running instance is bound to the given model."""
        with self._state_lock:
            return model_id in self._model_instances

    def instance_for_model(self, model_id: str) -> ModelInstance | None:
        """Return the running instance bound to ``model_id``, or None."""
        with self._state_lock:
            instance_id = self._model_instances.get(model_id)
            if instance_id is None:
                return None
            return self._instances.get(instance_id)

    def _register_instance(
        self, instance: ModelInstance, runtime: Any, installed: Any, *, select: bool = True
    ) -> None:
        with self._state_lock:
            self._instances[instance.id] = instance
            self._runtimes[instance.id] = runtime
            self._model_instances[instance.model_id] = instance.id
            self._instance_purposes[instance.id] = installed.purpose
            if select:
                self._selected_instances[installed.purpose] = instance.id
                self._selection_generations[installed.purpose] = (
                    self._selection_generations.get(installed.purpose, 0) + 1
                )

    async def start(
        self,
        model_id: str,
        backend_override: str | None = None,
        *,
        select: bool = True,
    ) -> ModelInstance:
        lock = self._model_locks.setdefault(model_id, asyncio.Lock())
        async with lock:
            with self._state_lock:
                if self._shutting_down:
                    raise RuntimeError("instance manager is shutting down")
                existing_id = self._model_instances.get(model_id)
                if existing_id is not None:
                    return self._instances[existing_id]
            installed = await self._model_registry.get(model_id)
            if installed is None:
                raise ModelNotFoundError(f"model not found: {model_id!r}")
            catalog = await self._catalog_model(installed)
            # recommend() 内部触发设备 scan（subprocess + onnxruntime 会话创建），
            # 是重同步操作，必须在线程池执行避免阻塞事件循环
            profile = await asyncio.to_thread(
                self._device_registry.recommend, catalog, backend_override
            )
            runtime = self._runtime_registry.create(profile, installed_model=installed)
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
                self._register_instance(
                    instance, runtime, installed, select=select
                )
                return instance
            except BaseException as start_error:
                try:
                    await self._run_sync(model_id, runtime.stop)
                except BaseException as rollback_error:
                    with self._state_lock:
                        cleanup_id = uuid4().hex
                        self._pending_cleanup[cleanup_id] = (model_id, runtime)
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
        with self._state_lock:
            instance = self._instances.get(instance_id)
        if instance is None:
            return
        lock = self._model_locks.setdefault(instance.model_id, asyncio.Lock())
        async with lock:
            with self._state_lock:
                current = self._instances.get(instance_id)
                if current is None:
                    return
                if current.active_routes:
                    raise InstanceInUseError(
                        f"instance {instance_id!r} is used by routes {current.active_routes!r}"
                    )
            await self._stop_locked(current)

    def get(self, instance_id: str) -> ModelInstance | None:
        with self._state_lock:
            return self._instances.get(instance_id)

    def list(self) -> list[ModelInstance]:
        with self._state_lock:
            return list(self._instances.values())

    async def benchmark(
        self,
        model_id: str,
        *,
        iterations: int = 3,
    ) -> dict[str, Any]:
        """对已部署模型执行真实推理测速。

        仅允许对已启动的实例测速（直接复用其 runtime）；模型未启动时
        拒绝测速并给出明确提示，不再自动临时启动（避免无意义的冷启动
        结果干扰用户对运行中负载的观测）。
        返回 {ok, purpose, latency_ms, throughput, device_id, provider, error, ...}。
        """
        if type(iterations) is not int or iterations <= 0:
            raise ValueError("iterations must be a positive integer")
        lock = self._model_locks.setdefault(model_id, asyncio.Lock())
        async with lock:
            with self._state_lock:
                existing_id = self._model_instances.get(model_id)
                runtime: RuntimeAdapter | None = None
                instance: ModelInstance | None = None
                purpose: ModelPurpose | None = None
                if existing_id is not None:
                    instance = self._instances.get(existing_id)
                    runtime = self._runtimes.get(existing_id)
                    purpose = self._instance_purposes.get(existing_id)
            if runtime is None or instance is None or purpose is None:
                return {
                    "ok": False,
                    "model_id": model_id,
                    "purpose": None,
                    "error": "模型尚未启动，无法测速。请先在「部署」中启动该模型后再测速。",
                }
            result = await self._benchmark_runtime(model_id, runtime, purpose, iterations)
            result["device_id"] = instance.device_id
            result["model_id"] = model_id
            return result

    async def _benchmark_runtime(
        self,
        model_id: str,
        runtime: RuntimeAdapter | None,
        purpose: ModelPurpose | None,
        iterations: int,
    ) -> dict[str, Any]:
        import time as time_module

        result: dict[str, Any] = {"ok": False, "purpose": purpose.value if purpose else None, "error": None}
        if runtime is None or purpose is None:
            result["error"] = "runtime unavailable"
            return result
        try:
            if purpose == ModelPurpose.EMBEDDING:
                samples = [
                    "本地向量模型推理性能基准测试：请将这句话编码为向量。",
                    "跨设备测速样本：语义检索与重排服务的延迟观测。",
                ]
                start = time_module.perf_counter()
                vectors = None
                for _ in range(iterations):
                    vectors = await self._run_sync(model_id, runtime.embed, samples)
                elapsed = max(time_module.perf_counter() - start, 1e-9)
                result["ok"] = True
                result["iterations"] = iterations
                result["samples"] = len(samples)
                result["latency_ms"] = round(elapsed / iterations * 1000, 2)
                result["samples_per_second"] = round(iterations * len(samples) / elapsed, 1)
                if vectors:
                    result["dimensions"] = len(vectors[0])
            elif purpose == ModelPurpose.RERANKER:
                query = "本地语义重排测速"
                documents = [
                    "本地向量模型推理性能基准测试文档一。",
                    "语义重排服务的延迟观测文档二。",
                    "跨设备测速样本文档三。",
                ]
                start = time_module.perf_counter()
                for _ in range(iterations):
                    scores = await self._run_sync(model_id, runtime.score, query, documents)
                elapsed = max(time_module.perf_counter() - start, 1e-9)
                result["ok"] = True
                result["iterations"] = iterations
                result["documents"] = len(documents)
                result["latency_ms"] = round(elapsed / iterations * 1000, 2)
                result["samples_per_second"] = round(iterations * len(documents) / elapsed, 1)
                if isinstance(scores, list):
                    result["scores"] = [round(float(score), 4) for score in scores]
            elif purpose == ModelPurpose.CHAT:
                messages = [{"role": "user", "content": "请用一句话回答：你叫什么？"}]
                tokens = 0
                start = time_module.perf_counter()
                for _ in range(iterations):
                    async for _chunk in runtime.stream(
                        messages,
                        {"max_tokens": 24},
                        _BenchmarkCancelToken(),
                    ):
                        tokens += 1
                elapsed = max(time_module.perf_counter() - start, 1e-9)
                result["ok"] = True
                result["iterations"] = iterations
                result["tokens"] = tokens
                result["latency_ms"] = round(elapsed / iterations * 1000, 2)
                result["tokens_per_second"] = round(tokens / elapsed, 1)
            else:
                result["error"] = f"unsupported benchmark purpose: {purpose.value}"
        except (OSError, RuntimeError, ValueError) as error:
            result["ok"] = False
            result["error"] = str(error)
        except Exception as error:
            logger.exception("instance_manager._benchmark_runtime.unexpected_error")
            result["ok"] = False
            result["error"] = str(error)
        return result

    async def resolve_runtime(
        self,
        purpose: ModelPurpose | str,
    ) -> RuntimeAdapter | None:
        resolved_purpose = ModelPurpose(purpose)
        with self._state_lock:
            selected_id = self._selected_instances.get(resolved_purpose)
            if selected_id is None:
                return None
            instance = self._instances.get(selected_id)
            runtime = self._runtimes.get(selected_id)
        if instance is None:
            raise RuntimeValidationError(
                f"selected {resolved_purpose.value} runtime is unavailable"
            )
        if runtime is None:
            raise RuntimeValidationError(
                f"selected {resolved_purpose.value} runtime is unavailable"
            )
        if (
            instance.state != "running"
            or instance.health != "healthy"
            or not await self._run_sync(instance.model_id, runtime.health)
        ):
            raise RuntimeValidationError(
                f"selected {resolved_purpose.value} runtime is unavailable"
            )
        return runtime

    async def acquire_runtime(
        self,
        purpose: ModelPurpose | str,
        route: str,
    ) -> tuple[str, RuntimeAdapter] | None:
        resolved_purpose = ModelPurpose(purpose)
        runtime = await self.resolve_runtime(resolved_purpose)
        if runtime is None:
            return None
        with self._state_lock:
            selected_id = self._selected_instances.get(resolved_purpose)
            if selected_id is None or self._runtimes.get(selected_id) is not runtime:
                raise RuntimeValidationError(
                    f"selected {resolved_purpose.value} runtime changed during acquisition"
                )
            self.bind_route(selected_id, route)
        return selected_id, runtime

    async def acquire_runtime_for_model(
        self,
        model_id: str,
        route: str,
    ) -> tuple[str, RuntimeAdapter] | None:
        """按指定模型定位运行实例的 runtime（供功能节点独立选择本地模型）。

        与 acquire_runtime 不同，不依赖全局按 purpose 的选中实例，而是直接按
        model_id 查找已启动实例。model_id 兼容 registry id（builtin:/local: 前缀）
        与规范化名。实例未启动/不健康时返回 None。
        """
        target = _norm_model_id(model_id)
        with self._state_lock:
            instance_id = self._model_instances.get(model_id)
            if instance_id is None:
                for mid, iid in self._model_instances.items():
                    if _norm_model_id(mid) == target:
                        instance_id = iid
                        break
            instance = self._instances.get(instance_id) if instance_id else None
            runtime = self._runtimes.get(instance_id) if instance_id else None
        if instance is None or runtime is None:
            return None
        if (
            instance.state != "running"
            or instance.health != "healthy"
            or not await self._run_sync(instance.model_id, runtime.health)
        ):
            return None
        self.bind_route(instance_id, route)
        return instance_id, runtime

    def release_runtime(self, instance_id: str, route: str) -> None:
        self.unbind_route(instance_id, route)

    def select_instance(
        self,
        purpose: ModelPurpose | str,
        instance_id: str | None,
        *,
        expected_generation: int | None = None,
    ) -> int:
        """显式选择用途实例；expected_generation 提供 CAS 保护。"""
        resolved_purpose = ModelPurpose(purpose)
        with self._state_lock:
            current_generation = self._selection_generations.get(resolved_purpose, 0)
            if expected_generation is not None and current_generation != expected_generation:
                raise RuntimeError(
                    f"selection changed concurrently for {resolved_purpose.value}"
                )
            if instance_id is None:
                self._selected_instances.pop(resolved_purpose, None)
            else:
                instance = self._instances.get(instance_id)
                if instance is None:
                    raise ValueError(f"unknown instance: {instance_id}")
                if self._instance_purposes.get(instance_id) != resolved_purpose:
                    raise ValueError(
                        f"instance {instance_id} purpose does not match {resolved_purpose.value}"
                    )
                self._selected_instances[resolved_purpose] = instance_id
            self._selection_generations[resolved_purpose] = current_generation + 1
            return current_generation + 1

    def selection_generation(self, purpose: ModelPurpose | str) -> int:
        resolved_purpose = ModelPurpose(purpose)
        with self._state_lock:
            return self._selection_generations.get(resolved_purpose, 0)

    def selection_identity(self, purpose: ModelPurpose | str) -> tuple[str, int] | None:
        resolved_purpose = ModelPurpose(purpose)
        with self._state_lock:
            selected_id = self._selected_instances.get(resolved_purpose)
            if selected_id is None:
                return None
            return (
                selected_id,
                self._selection_generations.get(resolved_purpose, 0),
            )

    def selection_available(self, purpose: ModelPurpose | str) -> bool:
        resolved_purpose = ModelPurpose(purpose)
        with self._state_lock:
            selected_id = self._selected_instances.get(resolved_purpose)
            if selected_id is None:
                return False
            instance = self._instances.get(selected_id)
            runtime = self._runtimes.get(selected_id)
        if instance is None or runtime is None:
            return False
        return instance.state == "running" and instance.health == "healthy"

    def bind_route(self, instance_id: str, route: str) -> ModelInstance:
        with self._state_lock:
            if instance_id in self._stopping_instances:
                raise InstanceInUseError(f"instance {instance_id!r} is stopping")
            instance = self._required_instance(instance_id)
            key = (instance_id, route)
            self._route_bindings[key] = self._route_bindings.get(key, 0) + 1
            routes = tuple(dict.fromkeys((*instance.active_routes, route)))
            updated = replace(instance, active_routes=routes, updated_at=self._now())
            self._instances[instance_id] = updated
            return updated

    def unbind_route(self, instance_id: str, route: str) -> ModelInstance:
        with self._state_lock:
            instance = self._required_instance(instance_id)
            key = (instance_id, route)
            count = self._route_bindings.get(key, 0)
            if count > 1:
                self._route_bindings[key] = count - 1
                routes = instance.active_routes
            else:
                self._route_bindings.pop(key, None)
                routes = tuple(item for item in instance.active_routes if item != route)
            updated = replace(instance, active_routes=routes, updated_at=self._now())
            self._instances[instance_id] = updated
            return updated

    async def refresh_health(self) -> list[ModelInstance]:
        # scan 内部做同步设备探测（/sys 遍历、subprocess、onnxruntime provider
        # 验证含一次真实推理）——若某次探测 hang，裸调会直接冻结事件循环数百秒
        # 且无法被 wait_for 取消（同步阻塞不响应 CancelledError）。移入线程池：
        # 健康轮询只关心结果，不占事件循环。
        # 2026-08-27：不再 force=True 强扫。DeviceRegistry.scan 自带 TTL 缓存
        # （DEVICE_SCAN_TTL_SECONDS，默认 300s），健康环每 60s 到此只是复用
        # 缓存结果，TTL 到期才真正重探。此前强扫导致每分钟一次 sudo 探针
        # 子进程 + NPU 争抢成为常驻负载（instance health 走 _run_sync，
        # 不受本次改动影响）。
        devices = {
            device.id: device
            for device in await asyncio.to_thread(self._device_registry.scan)
        }
        with self._state_lock:
            instances = tuple(self._instances.items())
        for instance_id, instance in instances:
            lock = self._model_locks.setdefault(instance.model_id, asyncio.Lock())
            async with lock:
                with self._state_lock:
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
                with self._state_lock:
                    latest = self._instances.get(instance_id)
                    if self._shutting_down:
                        continue
                    if latest is None:
                        continue
                    self._instances[instance_id] = replace(
                        latest,
                        state=state,
                        health=health,
                        updated_at=self._now(),
                    )
        return self.list()

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            if self._shutdown_completed:
                shutdown_task = self._shutdown_task
            elif self._shutdown_task is None or (
                self._shutdown_task.done() and not self._shutdown_completed
            ):
                with self._state_lock:
                    self._shutting_down = True
                self._shutdown_task = asyncio.create_task(self._shutdown())
                shutdown_task = self._shutdown_task
            else:
                shutdown_task = self._shutdown_task
        if shutdown_task is None:
            raise RuntimeError("instance manager shutdown state is invalid")
        try:
            await self._await_completion(shutdown_task)
        finally:
            # 失败时保留 task 引用，便于下次 shutdown() 检测到
            # 已完成的失败 task 并重新执行。
            pass

    async def _stop_all_instances(self) -> list[Exception]:
        errors: list[Exception] = []
        for lock in tuple(self._model_locks.values()):
            async with lock:
                pass
        with self._state_lock:
            instances = tuple(self._instances.values())
        for instance in instances:
            lock = self._model_locks.setdefault(instance.model_id, asyncio.Lock())
            async with lock:
                with self._state_lock:
                    current = self._instances.get(instance.id)
                if current is None:
                    continue
                try:
                    await self._stop_locked(current)
                except (OSError, RuntimeError) as error:
                    logger.warning("instance_manager.stop_locked_failed id={} error={}", current.id if hasattr(current, 'id') else '?', str(error)[:200])
                    errors.append(error)
                except Exception as error:
                    logger.exception("instance_manager._shutdown.stop_locked_unexpected id={}", current.id if hasattr(current, 'id') else '?')
                    errors.append(error)
        return errors

    async def _cleanup_pending(self) -> list[Exception]:
        errors: list[Exception] = []
        await self._await_lifecycle_tasks()
        with self._state_lock:
            pending_cleanup = tuple(self._pending_cleanup.items())
        for cleanup_id, (model_id, runtime) in pending_cleanup:
            try:
                await self._run_sync(model_id, runtime.stop)
            except (OSError, RuntimeError) as error:
                logger.warning("instance_manager.cleanup_stop_failed id={} error={}", cleanup_id, str(error)[:200])
                errors.append(error)
            except Exception as error:
                logger.exception("instance_manager._shutdown.cleanup_stop_unexpected id={}", cleanup_id)
                errors.append(error)
            else:
                with self._state_lock:
                    self._pending_cleanup.pop(cleanup_id, None)
        return errors

    async def _shutdown(self) -> None:
        errors: list[Exception] = []
        try:
            errors.extend(await self._stop_all_instances())
            errors.extend(await self._cleanup_pending())
            with self._state_lock:
                can_close_database = not self._instances and not self._pending_cleanup
            if can_close_database and self._database is not None and self._owns_database and not self._database_closed:
                try:
                    await self._database.close()
                except (OSError, RuntimeError) as error:
                    logger.warning("instance_manager.db_close_failed error={}", str(error)[:200])
                    errors.append(error)
                except Exception as error:
                    logger.exception("instance_manager._shutdown.db_close_unexpected")
                    errors.append(error)
                else:
                    self._database_closed = True
            if errors:
                raise ExceptionGroup("instance manager shutdown failed", errors)
        finally:
            with self._state_lock:
                owned_database_closed = (
                    self._database is None
                    or not self._owns_database
                    or self._database_closed
                )
                self._shutdown_completed = (
                    not self._instances
                    and not self._pending_cleanup
                    and owned_database_closed
                )

    async def _stop_locked(self, instance: ModelInstance) -> None:
        with self._state_lock:
            runtime = self._runtimes[instance.id]
            self._stopping_instances.add(instance.id)
        try:
            await self._run_sync(instance.model_id, runtime.stop)
        except BaseException:
            with self._state_lock:
                current = self._instances.get(instance.id)
                if current is not None:
                    self._instances[instance.id] = replace(
                        current,
                        state="degraded",
                        health="stop_failed",
                        updated_at=self._now(),
                    )
            raise
        finally:
            with self._state_lock:
                self._stopping_instances.discard(instance.id)
        with self._state_lock:
            for key in tuple(self._route_bindings):
                if key[0] == instance.id:
                    self._route_bindings.pop(key, None)
            self._instances.pop(instance.id, None)
            self._runtimes.pop(instance.id, None)
            self._model_instances.pop(instance.model_id, None)
            self._instance_purposes.pop(instance.id, None)

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
                except Exception as e:
                    logger.warning("local_ai.await_lifecycle_tasks_failed error={}", str(e))

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
            except (ImportError, OSError, RuntimeError, ValueError):
                if cancelled and propagate_cancel:
                    raise asyncio.CancelledError from None
                raise
            except Exception:
                logger.exception(".local_ai.instances.manager._await_completion_unexpected")
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
