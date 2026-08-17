from __future__ import annotations

import asyncio
import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

# hub/search 专用独立线程池：search_hub 内部嵌套 3 层 ThreadPoolExecutor，
# 与 NPU/onnxruntime 推理争抢 asyncio 默认池会把 5s 搜索拖到 12s+。
# 独立池避免竞争，且进程级单例避免每次请求重建。
_hub_search_executor = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="hub-search"
)

from local_ai.catalog.curated import CatalogLoader
from local_ai.catalog.hf_repo import HuggingFaceRepository
from local_ai.catalog.modelscope import InvalidRevisionError, ModelScopeRepository
from local_ai.contracts import CatalogFile, CatalogModel
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
        """调度后台任务，并在结束时检索异常，避免静默失败。

        done_callback 调用 task.exception() 会消费未检索的异常：
        若任务失败但无人 await（如 downloads.start / _start_instance 内部的
        非异常路径），这里将其记录到日志，避免 asyncio 的
        "Task exception was never retrieved" 警告与状态黑洞。
        """
        task = asyncio.create_task(coroutine)

        def _on_done(completed: asyncio.Task[Any]) -> None:
            self.background_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                exc = completed.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.error(
                    "background task failed: %s",
                    getattr(coroutine, "__qualname__", type(coroutine).__name__),
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_on_done)

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
    # 设备探测（subprocess + onnxruntime 会话创建）是重 CPU/IO 同步操作，
    # 必须在线程池执行，避免阻塞事件循环导致整个 WebUI 冻结
    devices = await asyncio.to_thread(_services(request).devices.scan)
    records = _records(devices)
    # 附加实时负载（CPU 利用率 / GPU 占用等），供前端算力设备页轮询观测
    from local_ai.devices.stats import attach_device_stats

    stats_by_id = await asyncio.to_thread(attach_device_stats, devices)
    for record in records:
        device_stats = stats_by_id.get(record.get("id"))
        if device_stats:
            record["stats"] = device_stats
    return Envelope(data=records)


@router.post("/local-ai/devices/rescan", response_model=Envelope[list[dict[str, Any]]])
async def rescan_devices(request: Request) -> Any:
    services = _services(request)
    devices = await asyncio.to_thread(services.devices.scan, True)
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
    # 根据已探测算力设备自动标注每个模型的可跑性（cpu/gpu/npu）
    from local_ai.devices.compat import annotate_catalog_models

    devices = await asyncio.to_thread(_services(request).devices.scan)
    return Envelope(data=annotate_catalog_models(models, devices))


@router.get("/local-ai/hub/categories", response_model=Envelope[list[dict[str, Any]]])
async def hub_categories(request: Request) -> Any:
    """模型广场分类节点配置（真实来自后端 pipeline 能力映射，非前端写死）。

    返回各分类的中文名、说明与所属 pipeline 集合；前端据此渲染分类节点，
    避免节点配置硬编码在前端。
    """
    from local_ai.catalog.hub_search import _CATEGORY_PIPELINES, _FUNCTIONAL_PIPELINES

    order = ["all", "embedding", "rerank", "chat", "other"]
    labels = {
        "all": "全部",
        "embedding": "向量嵌入",
        "rerank": "语义重排",
        "chat": "对话",
        "other": "其他",
    }
    descs = {
        "all": "功能节点（已过滤对话大模型）",
        "embedding": "Embedding 小模型",
        "rerank": "Rerank 小模型",
        "chat": "大模型，本机通常无法运行，慎用",
        "other": "分类 / 翻译 / 语音等",
    }
    categories: list[dict[str, Any]] = []
    for key in order:
        if key == "all":
            pipelines = sorted(_FUNCTIONAL_PIPELINES)
        else:
            pipelines = sorted(_CATEGORY_PIPELINES.get(key, set()))
        categories.append(
            {
                "key": key,
                "label": labels.get(key, key),
                "desc": descs.get(key, ""),
                "pipelines": pipelines,
            }
        )
    return Envelope(data=categories)


@router.get("/local-ai/hub/search", response_model=Envelope[dict[str, Any]])
async def hub_search(
    request: Request,
    q: str,
    source: str = "all",
    limit: int = 20,
    category: str = "all",
) -> Any:
    """在线仓库搜索（hf-mirror / ModelScope），供模型广场自定义搜索使用。

    只允许固定的镜像主机（SSRF 白名单），用户输入仅作搜索关键字；
    实时搜索不预设目录，搜索结果由用户检视后自行下载。
    关键词为空时返回各源功能性小模型（默认过滤 chat 大模型），
    模型广场进入页面自动获取；category 可切换模型分类。

    返回统一结构 {"results": [...], "errors": [...]}：双源并发搜索、
    跨源同 id 合并为一行（来源标注全部源）、单源失败不阻断另一源。
    """
    from local_ai.catalog.hub_search import HubSearchError, search_hub

    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    # hub/search 内部嵌套 3 层 ThreadPoolExecutor（pipeline 拉取 + revision 解析
    # + 双源并发），与 onnxruntime/NPU 推理、subprocess 一起争抢 asyncio 默认
    # 线程池导致 5s 的搜索被拖到 12s+。改用独立大线程池，避免与默认池竞争。
    try:
        payload = await asyncio.get_event_loop().run_in_executor(
            _hub_search_executor,
            lambda: search_hub(q, source, limit=limit, category=category),
        )
    except HubSearchError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    return Envelope(data=payload)


class HubDownloadRequest(BaseModel):
    repository: str = Field(min_length=1, pattern=r"^[^/]+/[^/]+$")
    revision: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    source: str = Field(default="modelscope", pattern=r"^(modelscope|hf-mirror)$")


@router.post(
    "/local-ai/hub/download",
    response_model=Envelope[dict[str, Any]],
    status_code=status.HTTP_202_ACCEPTED,
)
async def download_hub_repository(body: HubDownloadRequest, request: Request) -> Any:
    """检视并下载用户从在线搜索选中的仓库（ModelScope 契约）。

    与 /local-ai/downloads 相同的约束：只接受不可变 commit hash；
    布局不可识别或缺必需文件时拒绝下载（可运行性在检视时已评估）。
    """
    services = _services(request)
    try:
        inspection = await asyncio.to_thread(
            _run_remote_inspect, body.repository, body.revision, None, body.source
        )
    except InvalidRevisionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if inspection.missing:
        raise HTTPException(
            status_code=422,
            detail=f"仓库缺少必需文件: {'、'.join(inspection.missing)}",
        )
    if inspection.purpose is None:
        raise HTTPException(status_code=422, detail="无法识别模型布局，拒绝下载")
    files: list[CatalogFile] = []
    for item in inspection.files:
        if item.sha256 is None:
            raise HTTPException(
                status_code=422,
                detail=f"文件 {item.path} 缺少 SHA256，无法校验下载",
            )
        files.append(
            CatalogFile(path=item.path, size=item.size, sha256=item.sha256)
        )
    model = CatalogModel(
        id=body.repository,
        source=body.source,
        repository=inspection.repository,
        revision=inspection.revision,
        purpose=inspection.purpose,
        files=tuple(files),
        download_size=sum(item.size for item in files),
        license=None,
    )
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
    request_input = (body.repository, body.revision, validation.path)
    existing = services.request_results.get(key)
    if existing is not None:
        if services.request_inputs.get(key) != request_input:
            raise HTTPException(status_code=409, detail="request_id conflicts with a different download")
        return Envelope(data={"task": existing.to_dict()})
    task = services.downloads.create(model, validation.path)
    services.request_results[key] = task
    services.request_inputs[key] = request_input
    services.spawn(services.downloads.start(task.id))
    return Envelope(data={"task": task.to_dict()})


def _run_remote_inspect(
    repository: str, revision: str, token: str | None, source: str = "modelscope"
) -> Any:
    """在独立线程事件循环中执行远程检视。

    ModelScopeRepository / HuggingFaceRepository 内部含同步 DNS 解析（SSRF 防护）
    与网络 IO，整体放线程池执行，避免阻塞 WebUI 事件循环（同 C1/C2 修复原则）。
    source 决定走哪个仓库适配器（hf-mirror / modelscope），保证两个获取源
    的检视与下载契约统一。
    """
    adapter = (
        HuggingFaceRepository()
        if source == "hf-mirror"
        else ModelScopeRepository()
    )
    return asyncio.run(adapter.inspect(repository, revision, token))


@router.get("/local-ai/hub/revision", response_model=Envelope[dict[str, Any]])
async def resolve_hub_revision(
    request: Request,
    repository: str,
    source: str = "modelscope",
) -> Any:
    """解析仓库默认分支的不可变 commit hash（检视/下载前按需调用）。

    搜索阶段为提速跳过 revision 解析（50 个仓库各一次 HTTP 要 3s+），
    用户点「查看解析」时前端按需解析单个仓库的 revision，再调检视端点。
    hf-mirror 搜索结果已带 sha，无需调本端点；仅 ModelScope 需要。
    """
    if source not in {"modelscope", "hf-mirror"}:
        raise HTTPException(status_code=422, detail="source must be modelscope or hf-mirror")
    if not isinstance(repository, str) or "/" not in repository:
        raise HTTPException(status_code=422, detail="repository must be owner/name")
    if source == "hf-mirror":
        # hf-mirror 搜索结果已带 sha，无需解析
        raise HTTPException(status_code=422, detail="hf-mirror 搜索结果已带 sha，无需解析 revision")
    from local_ai.catalog.hub_search import _modelscope_revision

    revision = await asyncio.get_event_loop().run_in_executor(
        _hub_search_executor, _modelscope_revision, repository
    )
    return Envelope(data={"repository": repository, "revision": revision})


@router.get("/local-ai/remote/inspect", response_model=Envelope[dict[str, Any]])
async def inspect_remote_repository(
    request: Request,
    repository: str,
    revision: str,
    source: str = "modelscope",
) -> Any:
    """检视远程仓库：识别布局 / 目的 / 可运行性（hf-mirror / ModelScope 双源统一）。

    只接受不可变 commit hash（与下载契约一致）；检视结果以 error 状态返回，
    不向调用方泄漏原始异常。source 决定走哪个仓库适配器。
    """
    if source not in {"modelscope", "hf-mirror"}:
        raise HTTPException(status_code=422, detail="source must be modelscope or hf-mirror")
    if not isinstance(repository, str) or "/" not in repository:
        raise HTTPException(status_code=422, detail="repository must be owner/name")
    try:
        inspection = await asyncio.to_thread(
            _run_remote_inspect, repository, revision, None, source
        )
    except InvalidRevisionError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PermissionError as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except ValueError as error:  # SSRF 拦截等校验失败
        raise HTTPException(status_code=400, detail=str(error)) from error
    return Envelope(data={
        "repository": inspection.repository,
        "revision": inspection.revision,
        "files": [
            {"path": item.path, "size": item.size, "sha256": item.sha256}
            for item in inspection.files
        ],
        "purpose": inspection.purpose.value if inspection.purpose is not None else None,
        "runnable": inspection.runnable,
        "state": inspection.state,
        "evidence": dict(inspection.evidence),
        "missing": list(inspection.missing),
    })


@router.get("/local-ai/models", response_model=Envelope[list[dict[str, Any]]])
async def list_models(request: Request) -> Any:
    return Envelope(data=_records(await _services(request).models.list()))


@router.post("/local-ai/models/{model_id}/benchmark", response_model=Envelope[dict[str, Any]])
async def benchmark_model(model_id: str, request: Request, body: dict | None = None) -> Any:
    """对已部署模型执行真实推理测速（embedding / reranker / chat）。

    已运行实例直接复用；未运行则临时启动测速后停止并恢复原选中实例。
    """
    iterations = int((body or {}).get("iterations", 3) or 3)
    if iterations < 1 or iterations > 20:
        raise HTTPException(status_code=422, detail="iterations must be between 1 and 20")
    services = _services(request)
    installed = await services.models.get(model_id)
    if installed is None:
        raise HTTPException(status_code=404, detail=f"Model not found: {model_id}")
    result = await services.instances.benchmark(model_id, iterations=iterations)
    return Envelope(data=result)


@router.delete("/local-ai/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_model(model_id: str, request: Request) -> Response:
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(status_code=400, detail="缺少 X-Confirm: yes 确认头")
    services = _services(request)
    installed = await services.models.get(model_id)
    if installed is None:
        raise HTTPException(status_code=404, detail=f"Model not found: {model_id}")
    if services.instances.model_in_use(model_id):
        raise HTTPException(
            status_code=409,
            detail=f"模型 {model_id} 正在被运行中的实例使用，请先停止实例再删除",
        )
    active_downloads = services.downloads.active_for_model(model_id)
    if active_downloads:
        raise HTTPException(
            status_code=409,
            detail=f"模型 {model_id} 仍有 {len(active_downloads)} 个未完成的下载任务",
        )
    try:
        await services.models.remove(model_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    directory = Path(installed.directory)
    if directory.is_dir() and directory.exists():
        # 仅清理受管模型目录：绝对路径、非文件系统根、且目录名非空
        resolved = directory.resolve()
        if resolved != resolved.parent and directory.name.strip():
            shutil.rmtree(directory, ignore_errors=True)
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


@router.delete("/local-ai/downloads/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_download(task_id: str, request: Request) -> Response:
    try:
        await _services(request).downloads.delete(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
