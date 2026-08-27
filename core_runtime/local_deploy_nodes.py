"""功能节点：API(硅基流动免费) / 本地模型 选择注册表与运行时热更新。

system 服务节点——RAG 链路与系统内部 AI 功能依赖的免费模型入口（主 LLM 除外）。
每个节点在两种后端之间选择（config.local_deploy.nodes.<id> 持久化）：

- local: 强制本地（编码型=本地 ONNX 模型；生成型=本地对话小模型），长驻服务、重启自动恢复
- api:   强制远程 API（硅基流动免费模型），并停止该节点的本地推理常驻

（auto/off 为历史默认值与内部禁用值，前端不再暴露。）

PUT /local-deploy/model-nodes 修改后通过 apply_to_runtime 立即热生效。
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger

# 合法后端值（下沉到 node_registry，re-export 保持兼容）
from core_runtime.node_registry import (  # noqa: E402,F401
    _BACKENDS,
    _NODE_MAP,
    NODES,
    get_backend,
    get_local_model,
    set_backend,
    valid_backend,
)


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
    except (RuntimeError, ImportError, OSError, ValueError):  # noqa: BLE001
        return []


async def validate_local_selection(
    core: Any, node: dict[str, Any], local_model: str
) -> str:
    """只读校验节点本地模型选择；不启动实例、不写配置。"""
    node_id = node["id"]
    if node_id == "asr":
        raise ValueError("ASR 本地运行时尚未实现，请使用 API 或关闭节点")
    model_name = str(local_model or "").strip()
    if not model_name:
        raise ValueError(f"node {node_id} requires local_model for local backend")
    installed = await _installed_models(core)
    registry_id = _find_registry_id(installed, model_name)
    if registry_id is None:
        raise ValueError(f"local model is not installed: {model_name}")
    expected = node["model_purpose"]
    record = next(
        (item for item in installed
         if item.get("id") == registry_id or item.get("catalog_id") == registry_id),
        None,
    )
    actual = str((record or {}).get("purpose") or "")
    aliases = {
        "embedding": {"embedding", "embed"},
        "reranker": {"reranker", "rerank"},
        "chat": {"chat", "text-generation", "llm"},
        "asr": {"asr", "stt", "speech", "whisper"},
    }
    if actual not in aliases[expected]:
        raise ValueError(
            f"model purpose mismatch for {node_id}: expected {expected}, got {actual or 'unknown'}"
        )
    return registry_id


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
    except (RuntimeError, ImportError, OSError, ValueError):  # noqa: BLE001
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


async def ensure_local_instance(
    core: Any, node_id: str, local_model: str, *, required: bool = False,
    select: bool = True,
) -> Any:
    """确保节点绑定的本地模型实例已启动（常驻服务）。

    - 已安装模型（registry）→ 通过实例管理器启动对应实例，幂等（已运行则复用）
    - 未找到安装记录（如节点默认模型）→ 返回 False，由 apply_to_runtime 走既有热切路径

    返回 True 表示实例已由本函数托管启动（长驻）。
    """
    installed = await _installed_models(core)
    real_id = _find_registry_id(installed, local_model)
    if real_id is None:
        logger.info("local_deploy.node_no_installed_model node={} model={}", node_id, local_model)
        if required:
            raise ValueError(f"local model is not installed: {local_model}")
        return False
    manager = getattr(core, "local_ai_instances", None)
    if manager is None or not hasattr(manager, "start"):
        if required:
            raise ValueError("local instance manager is unavailable")
        return False
    try:
        instance = (
            await manager.start(real_id)
            if select else await manager.start(real_id, select=False)
        )
        logger.info("local_deploy.node_instance_started node={} model={} registry_id={}", node_id, local_model, real_id)
        return instance
    except (RuntimeError, OSError, ValueError, ImportError) as e:  # noqa: BLE001
        logger.warning("local_deploy.node_instance_start_failed node={} model={} error={}", node_id, local_model, str(e))
        if required:
            raise ValueError(f"failed to start local model {local_model}: {e}") from e
        return False


async def model_has_other_local_references(
    cfg: Any, node_id: str, local_model: str
) -> bool:
    target = _norm_model_id(local_model)
    for node in NODES:
        other_id = node["id"]
        if other_id == node_id or get_backend(cfg, other_id) != "local":
            continue
        if _norm_model_id(get_local_model(cfg, other_id)) == target:
            return True
    return False


async def stop_node_instance(core: Any, node_id: str, local_model: str,
                             cfg: Any | None = None) -> None:
    """停止节点绑定实例；仍被其他 local 节点引用时保留。"""
    if cfg is not None and await model_has_other_local_references(
        cfg, node_id, local_model
    ):
        logger.info(
            "local_deploy.node_instance_kept_shared node={} model={}",
            node_id, local_model,
        )
        return
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
    except (RuntimeError, OSError, ValueError) as e:  # noqa: BLE001
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


def _node_catalog_purpose(node: dict[str, Any]) -> str | None:
    """功能节点 → 目录（catalog）用途，用于把「已收录但未下载」的目录候选并入节点候选列表。

    - embedding 节点 → embedding
    - reranker 节点 → reranker
    - 生成型节点 → chat
    - asr 等其他节点暂无目录预置，返回 None（不并入）
    """
    node_id = node["id"]
    if node_id == "embedding":
        return "embedding"
    if node_id == "reranker":
        return "reranker"
    if node["kind"] == "generative":
        return "chat"
    return None


def _node_local_models(node: dict[str, Any], installed: list[dict[str, str]]) -> list[dict[str, Any]]:
    """按节点用途分类的本地模型候选 = 已安装（白）+ 目录中未下载（灰），不硬编码。

    每个节点对应的分类：
    - embedding 节点 → 向量嵌入模型
    - reranker 节点 → 语义重排模型
    - 生成型节点 → 对话小模型（替代主 LLM 执行）
    - asr 节点 → 语音识别模型

    内置模型在注册表中 id 为 builtin:bge-small-zh-v1.5，规范化后（bge-small-zh-v1.5）
    展示，避免出现前缀。已下载候选 installed=True，目录中未下载候选 installed=False，
    由前端区分白/灰并提供「一键下载」。
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

    # 并入目录中「已收录但未下载」的候选（installed=False），供前端灰色展示 + 一键下载
    catalog_purpose = _node_catalog_purpose(node)
    if catalog_purpose is not None:
        for catalog_item in _catalog_candidates(catalog_purpose):
            if catalog_item["id"] in seen:
                continue
            seen.add(catalog_item["id"])
            candidates.append({
                "id": catalog_item["id"],
                "catalog_id": catalog_item["catalog_id"],
                "purpose": catalog_item["purpose"],
                "source": catalog_item["source"],
                "installed": False,
                "ownership": "",
            })
    return candidates


async def build_status(core: Any, vs: Any, cfg: Any) -> list[dict[str, Any]]:
    """计算每个节点的状态：当前选择 / API 可用性 / 本地可用性 / 生效后端。"""
    from core_runtime.prompt_profiles import profiles_for_node

    api_ok = _api_configured()
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
                    except (RuntimeError, OSError, ValueError):  # noqa: BLE001
                        local_available = False
        elif node_id == "asr":
            local_available = any(c.get("installed") for c in _node_local_models(node, installed))
        else:  # generative 节点：本地=已安装的对话小模型
            local_available = any(c.get("installed") for c in _node_local_models(node, installed))
        result.append({
            **node,
            "backend": backend,
            "api_configured": api_ok,
            "local_available": local_available,
            "local_model": get_local_model(cfg, node_id),
            "local_models": _node_local_models(node, installed),
            "prompt_profiles": [
                profile.public_summary() for profile in profiles_for_node(node_id)
            ],
        })
    return result


def _try_set_backend(obj: Any, backend: str, local_model: str | None = None) -> None:
    """若对象有 set_backend 方法，则热切换其后端；否则静默跳过。

    统一 apply_to_runtime 各节点「获取对象 → 存在且有 set_backend → 切换」
    的重复模式（对象/模块/引擎实例均适用）。
    """
    if obj is not None and hasattr(obj, "set_backend"):
        obj.set_backend(backend, local_model)


def _apply_service_node(core: Any, vs: Any, node_id: str, backend: str, local_model: str | None) -> None:
    if node_id == "embedding":
        if vs is not None and backend in ("local", "api"):
            vs.set_embed_mode("local" if backend == "local" else "remote")
        return
    if node_id == "reranker":
        memory = getattr(core, "memory", None)
        _try_set_backend(getattr(memory, "_reranker_service", None), backend, local_model)
        return
    if node_id == "query_transform":
        memory = getattr(core, "memory", None)
        _try_set_backend(getattr(memory, "_query_transformer", None), backend, local_model)
        return
    if node_id == "instinct":
        _try_set_backend(getattr(core, "instinct_manager", None), backend, local_model)
        return
    if node_id == "error_rule":
        _try_set_backend(getattr(core, "error_pipeline", None), backend, local_model)
        return
    if node_id == "kg_extract":
        kg = getattr(core, "knowledge_graph", None)
        _try_set_backend(kg, backend, local_model)
        _try_set_backend(getattr(kg, "_kg_v2", None) if kg is not None else None, backend, local_model)
        return
    if node_id == "asr":
        if backend == "local":
            raise ValueError("ASR local runtime is not implemented")
        return
    if node_id == "intent_decomposition":
        # 经 web.routers.jspace 转调：那是历史稳定的运行时热切换缝，
        # 测试以它为 patch 点；jspace 真身只是转调 core.j_space_bootstrap
        from web.routers.jspace import set_intent_backend

        set_intent_backend(backend, local_model)
        return


def _apply_llm_node(core: Any, node_id: str, backend: str, local_model: str | None, app: Any) -> None:
    if node_id == "emotion_llm":
        from emotion import emotion_llm
        _try_set_backend(emotion_llm, backend, local_model)
        return
    if node_id == "reunion":
        from emotion import reunion_reflection
        _try_set_backend(reunion_reflection, backend, local_model)
        return
    if node_id == "portrait":
        _try_set_backend(getattr(core, "portrait_manager", None), backend, local_model)
        return
    if node_id == "memory_distill":
        memory = getattr(core, "memory", None)
        _try_set_backend(getattr(memory, "distiller", None), backend, local_model)
        return
    if node_id == "dream":
        from core.dream_engine_v2 import get_dream_engine_v2
        _try_set_backend(get_dream_engine_v2(), backend, local_model)
        return
    if node_id == "nudge":
        try:
            import qq_bot_adapter
            bot = getattr(qq_bot_adapter, "get_active_bot", lambda: None)()
            nudge = getattr(bot, "nudge_engine", None) if bot is not None else None
            _try_set_backend(nudge, backend, local_model)
        except (ImportError, AttributeError):
            logger.debug("local_deploy.nudge_not_available")
        return
    if node_id == "spontaneous_recall":
        obj = getattr(app.state, "spontaneous_recall", None) if app is not None else None
        _try_set_backend(obj, backend, local_model)
        return
    if node_id == "growth":
        obj = getattr(app.state, "growth_narrative", None) if app is not None else None
        _try_set_backend(obj, backend, local_model)
        return


_SERVICE_NODES = {
    "embedding", "reranker", "query_transform", "instinct", "error_rule",
    "kg_extract", "asr", "intent_decomposition",
}


def apply_to_runtime(core: Any, vs: Any, node_id: str, backend: str, app: Any = None,
                     local_model: str | None = None, strict: bool = False) -> None:
    try:
        if node_id in _SERVICE_NODES:
            _apply_service_node(core, vs, node_id, backend, local_model)
        else:
            _apply_llm_node(core, node_id, backend, local_model, app)
    except (RuntimeError, OSError, ValueError, ImportError) as e:
        logger.warning("local_deploy.node_apply_failed node={} error={}", node_id, str(e))
        if strict:
            raise
