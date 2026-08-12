"""功能节点：API(硅基流动免费) / 本地模型 选择注册表与运行时热更新。

system 服务节点——RAG 链路与系统内部 AI 功能依赖的免费模型入口（主 LLM 除外）。
每个节点在两种后端之间选择（config.local_deploy.nodes.<id> 持久化）：

- local: 强制本地（编码型=本地 ONNX 模型；生成型=主 LLM 执行），长驻服务、重启自动恢复
- api:   强制远程 API（硅基流动免费模型），并停止该节点的本地推理常驻

（auto/off 为历史默认值与内部禁用值，前端不再暴露。）

PUT /local-deploy/model-nodes 修改后通过 apply_to_runtime 立即热生效。
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger

# 合法后端值
_BACKENDS = ("auto", "local", "api", "off")

# 节点注册表：kind=encoder 编码型（本地有专有模型）/ generative 生成型（本地=主 LLM）
NODES: list[dict[str, Any]] = [
    {
        "id": "embedding",
        "name": "向量嵌入",
        "kind": "encoder",
        "desc": "记忆/知识库向量化，RAG 检索入口",
        "api_model": "硅基流动免费模型",
        "local_desc": "本地 BGE 模型（ONNX / NPU 自适应）",
        "local_model": "bge-small-zh-v1.5",
        "default": "api",
    },
    {
        "id": "reranker",
        "name": "语义重排",
        "kind": "encoder",
        "desc": "检索结果相关性精排",
        "api_model": "硅基流动免费模型",
        "local_desc": "本地 bge-reranker-base（int8）",
        "local_model": "bge-reranker-base",
        "default": "api",
    },
    {
        "id": "query_transform",
        "name": "查询改写",
        "kind": "generative",
        "desc": "查询扩展 / HyDE / 意图分类",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "instinct",
        "name": "本能提取",
        "kind": "generative",
        "desc": "对话规律提取（instinct 规则）",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "error_rule",
        "name": "失败规则",
        "kind": "generative",
        "desc": "工具失败 → 可复用预防规则",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "kg_extract",
        "name": "知识图谱提取",
        "kind": "generative",
        "desc": "对话实体/关系抽取（KG）",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "asr",
        "name": "语音识别",
        "kind": "other",
        "desc": "语音消息转文字（语音输入）",
        "api_model": "硅基流动免费模型",
        "local_desc": "",
        "default": "api",
    },
]

_NODE_MAP = {node["id"]: node for node in NODES}


def valid_backend(value: str) -> bool:
    return value in _BACKENDS


def get_backend(cfg: Any, node_id: str) -> str:
    """读取节点当前配置的后端值（默认 auto；asr 默认 api）。"""
    if node_id not in _NODE_MAP:
        raise ValueError(f"unknown model node: {node_id}")
    try:
        value = str(
            cfg.get(f"local_deploy.nodes.{node_id}", "")
            or _NODE_MAP[node_id]["default"]
        ).strip().lower()
    except Exception:  # noqa: BLE001
        value = _NODE_MAP[node_id]["default"]
    return value if valid_backend(value) else _NODE_MAP[node_id]["default"]


def set_backend(cfg: Any, node_id: str, backend: str, local_model: str | None = None) -> str:
    """校验并持久化节点后端选择，返回归一化后的值。

    选择 local 时可指定具体本地模型（local_model，如已安装的 bge 仓库）；
    未指定时使用节点默认本地模型（内置模型 / 主 LLM）。
    """
    if node_id not in _NODE_MAP:
        raise ValueError(f"unknown model node: {node_id}")
    backend = str(backend or "").strip().lower()
    if not valid_backend(backend):
        raise ValueError(f"invalid backend: {backend!r}, must be one of {list(_BACKENDS)}")
    cfg.set(f"local_deploy.nodes.{node_id}", backend)
    # 注意：local_model 必须存独立路径（local_deploy.node_models.<id>），
    # 不能挂在 nodes.<id> 下——那会把 backend 字符串替换成 dict 导致读取失败
    if local_model and local_model.strip():
        cfg.set(f"local_deploy.node_models.{node_id}", local_model.strip())
    logger.info("local_deploy.node_set node={} backend={} local_model={}", node_id, backend, local_model or "-")
    return backend


def get_local_model(cfg: Any, node_id: str) -> str:
    """读取节点已选择的本地模型（local_model）；未设置时返回节点默认本地模型。"""
    if node_id not in _NODE_MAP:
        raise ValueError(f"unknown model node: {node_id}")
    try:
        value = str(
            cfg.get(f"local_deploy.node_models.{node_id}", "")
            or _NODE_MAP[node_id].get("local_model", "")
        ).strip()
    except Exception:  # noqa: BLE001
        value = _NODE_MAP[node_id].get("local_model", "")
    return value or ""


def _api_configured() -> bool:
    return bool(
        os.getenv("SILICONFLOW_API_KEY")
        or os.getenv("EMBED_API_KEY")
    )


def _router_available(core: Any) -> bool:
    router = getattr(core, "router", None)
    return router is not None


async def _installed_models(core: Any) -> list[dict[str, str]]:
    """已安装模型清单（来自本地模型注册表），供节点「本地模型」选择。

    返回 [{id, catalog_id, purpose, source}]；获取失败返回空（不影响页面）。
    注意 registry.list() 是异步方法，必须 await（否则拿到 coroutine 导致遍历失败）。
    """
    try:
        manager = getattr(core, "local_ai_instances", None)
        registry = getattr(manager, "_model_registry", None)
        if registry is None:
            return []
        records = await registry.list()
        models: list[dict[str, str]] = []
        for record in records or []:
            model_id = getattr(record, "id", None) or getattr(record, "catalog_id", None)
            if not model_id:
                continue
            purpose = getattr(record, "purpose", None)
            models.append({
                "id": str(model_id),
                "catalog_id": str(getattr(record, "catalog_id", None) or ""),
                "purpose": str(purpose.value) if hasattr(purpose, "value") else str(purpose or ""),
                "source": str(getattr(record, "source", None) or ""),
                "ownership": str(getattr(record, "ownership", None) or ""),
            })
        return models
    except Exception:  # noqa: BLE001
        return []


def _catalog_candidates(purpose: str) -> list[dict[str, str]]:
    """该用途的全部目录候选（含未下载），供节点选择；catalog 读取失败返回空。

    返回 [{id, purpose, source}]——与 installed 合并后由前端区分已下载（白）/未下载（灰）。
    """
    try:
        from local_ai.catalog.curated import CatalogLoader
        models = CatalogLoader().load_curated()
        return [
            {
                "id": str(model.id),
                "catalog_id": str(model.id),
                "purpose": model.purpose.value if hasattr(model.purpose, "value") else str(model.purpose),
                "source": str(model.source),
            }
            for model in models
            if (model.purpose.value if hasattr(model.purpose, "value") else str(model.purpose)) == purpose
        ]
    except Exception:  # noqa: BLE001
        return []


def _norm_model_id(raw: str) -> str:
    """规范化模型 id：去掉内置/本地的 id 前缀（builtin:/local:），用于与目录候选匹配与展示。"""
    if isinstance(raw, str) and ":" in raw:
        prefix, name = raw.split(":", 1)
        if prefix in ("builtin", "local"):
            return name
    return str(raw)


def _find_registry_id(installed: list[dict[str, str]], display_name: str) -> str | None:
    """按展示名（规范名）在已安装列表中找真实 id（registry id 可能带 builtin:/local: 前缀）。

    用于「本地模型常驻服务」启动：前端传回的 local_model 是规范名
    （如 bge-small-zh-v1.5），而实例管理器需要真实 registry id
    （如 builtin:bge-small-zh-v1.5）才能 start。
    """
    if not display_name:
        return None
    target = _norm_model_id(display_name)
    for model in installed:
        if _norm_model_id(model.get("id", "")) == target:
            return model.get("id") or None
        if _norm_model_id(model.get("catalog_id", "")) == target:
            return model.get("catalog_id") or None
    return None


async def ensure_local_instance(core: Any, node_id: str, local_model: str) -> bool:
    """确保节点绑定的本地模型实例已启动（常驻服务）。

    - 已安装模型（registry）→ 通过实例管理器启动对应实例，幂等（已运行则复用）
    - 未找到安装记录（如节点默认模型）→ 返回 False，由 apply_to_runtime 走既有热切路径

    返回 True 表示实例已由本函数托管启动（长驻）。
    """
    installed = await _installed_models(core)
    real_id = _find_registry_id(installed, local_model)
    if real_id is None:
        logger.info("local_deploy.node_no_installed_model node={} model={}", node_id, local_model)
        return False
    manager = getattr(core, "local_ai_instances", None)
    if manager is None or not hasattr(manager, "start"):
        return False
    try:
        await manager.start(real_id)
        logger.info("local_deploy.node_instance_started node={} model={} registry_id={}", node_id, local_model, real_id)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("local_deploy.node_instance_start_failed node={} model={} error={}", node_id, local_model, str(e))
        return False


async def stop_node_instance(core: Any, node_id: str, local_model: str) -> None:
    """停止节点绑定的本地模型实例（切回 API / 关闭时调用，关闭本地推理常驻）。"""
    installed = await _installed_models(core)
    real_id = _find_registry_id(installed, local_model)
    if real_id is None:
        return
    manager = getattr(core, "local_ai_instances", None)
    if manager is None:
        return
    try:
        instance = manager.instance_for_model(real_id)
        if instance is not None:
            await manager.stop(instance.id)
            logger.info("local_deploy.node_instance_stopped node={} model={}", node_id, local_model)
    except Exception as e:  # noqa: BLE001
        logger.warning("local_deploy.node_instance_stop_failed node={} model={} error={}", node_id, local_model, str(e))


async def restore_local_instances(core: Any, cfg: Any) -> None:
    """启动时恢复常驻本地推理：读取配置，对 backend=local 且选了模型的节点自动启动实例。"""
    if core is None:
        return
    for node in NODES:
        node_id = node["id"]
        if get_backend(cfg, node_id) != "local":
            continue
        local_model = get_local_model(cfg, node_id)
        if not local_model:
            continue
        await ensure_local_instance(core, node_id, local_model)


def _node_local_models(node: dict[str, Any], installed: list[dict[str, str]]) -> list[dict[str, Any]]:
    """按节点用途分类的本地模型候选 —— 只来自已安装模型，不硬编码。

    每个节点对应的分类：
    - embedding 节点 → 已安装的向量嵌入模型
    - reranker 节点 → 已安装的语义重排模型
    - 生成型节点 → 已安装的对话小模型（替代主 LLM 执行）
    - asr 节点 → 已安装的语音识别模型

    内置模型在注册表中 id 为 builtin:bge-small-zh-v1.5，规范化后（bge-small-zh-v1.5）
    展示，避免出现前缀；未安装任何对应模型的节点返回空列表。
    """
    node_id = node["id"]
    if node_id == "embedding":
        purposes = ("embedding", "embed")
    elif node_id == "reranker":
        purposes = ("rerank", "reranker")
    elif node["kind"] == "generative":
        purposes = ("chat", "text-generation", "llm")
    elif node_id == "asr":
        purposes = ("asr", "stt", "speech", "whisper")
    else:
        return []

    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for model in installed:
        if model["purpose"] not in purposes:
            continue
        key = _norm_model_id(model["id"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "id": key,
            "catalog_id": _norm_model_id(model["catalog_id"]) if model["catalog_id"] else key,
            "purpose": model["purpose"],
            "source": "内置" if model.get("ownership") == "bundled" else (model.get("source") or ""),
            "installed": True,
            "ownership": model.get("ownership", ""),
        })
    return candidates


async def build_status(core: Any, vs: Any, cfg: Any) -> list[dict[str, Any]]:
    """计算每个节点的状态：当前选择 / API 可用性 / 本地可用性 / 生效后端。"""
    api_ok = _api_configured()
    router_ok = _router_available(core)
    installed = await _installed_models(core)
    result: list[dict[str, Any]] = []
    for node in NODES:
        node_id = node["id"]
        backend = get_backend(cfg, node_id)
        local_available = False
        if node_id == "embedding":
            local_available = bool(
                vs is not None
                and getattr(vs, "_local_provider", None) is not None
                and getattr(vs, "_embed_mode", None) == "local"
            )
        elif node_id == "reranker":
            memory = getattr(core, "memory", None)
            if memory is not None:
                service = getattr(memory, "_reranker_service", None)
                if service is not None:
                    try:
                        local_available = bool(service.available)
                    except Exception:  # noqa: BLE001
                        local_available = False
        elif node_id == "asr":
            local_available = bool(_node_local_models(node, installed))
        else:  # generative 节点：本地=已安装的对话小模型（替代主 LLM）
            local_available = bool(_node_local_models(node, installed))
        result.append({
            **node,
            "backend": backend,
            "api_configured": api_ok,
            "local_available": local_available,
            "local_model": get_local_model(cfg, node_id),
            "local_models": _node_local_models(node, installed),
        })
    return result


def apply_to_runtime(core: Any, vs: Any, node_id: str, backend: str) -> None:
    """将节点后端选择立即应用到运行时对象（热生效，无重启）。"""
    try:
        if node_id == "embedding":
            # embedding 复用向量库引擎切换（热生效）
            if vs is not None and backend in ("local", "api"):
                mode = "local" if backend == "local" else "remote"
                vs.set_embed_mode(mode)
            return
        if node_id == "reranker":
            memory = getattr(core, "memory", None)
            service = getattr(memory, "_reranker_service", None)
            if service is not None and hasattr(service, "set_backend"):
                service.set_backend(backend)
            return
        if node_id == "query_transform":
            memory = getattr(core, "memory", None)
            qt = getattr(memory, "_query_transformer", None)
            if qt is not None and hasattr(qt, "set_backend"):
                qt.set_backend(backend)
            return
        if node_id == "instinct":
            manager = getattr(core, "instinct_manager", None)
            if manager is not None and hasattr(manager, "set_backend"):
                manager.set_backend(backend)
            return
        if node_id == "error_rule":
            pipeline = getattr(core, "error_pipeline", None)
            if pipeline is not None and hasattr(pipeline, "set_backend"):
                pipeline.set_backend(backend)
            return
        if node_id == "kg_extract":
            kg = getattr(core, "knowledge_graph", None)
            if kg is not None and hasattr(kg, "set_backend"):
                kg.set_backend(backend)
            kg_v2 = getattr(kg, "_kg_v2", None) if kg is not None else None
            if kg_v2 is not None and hasattr(kg_v2, "set_backend"):
                kg_v2.set_backend(backend)
            return
        if node_id == "asr":
            # ASR 每次请求读取配置，无需运行时对象
            return
    except Exception as e:  # noqa: BLE001
        logger.warning("local_deploy.node_apply_failed node={} error={}", node_id, str(e))
