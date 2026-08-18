"""功能节点注册表与后端选择读写 —— 下沉自 web.local_deploy_nodes。

拆分动机：消除 qq_bot_adapter ↔ local_deploy_nodes 导入环。
qq_bot_adapter 只需读取 nudge 节点的后端/本地模型配置，不应依赖
local_deploy_nodes 的运行时热更新逻辑（apply_to_runtime 等）。
本模块只含 NODES 注册表 + 纯配置读写（无反向依赖）。
"""
from __future__ import annotations

import os
from typing import Any

from loguru import logger


# 节点后端只有三种（与前端按钮对齐，无 auto）：
#   local = 本地模型；api = 硅基流动免费模型；off = 关闭。
# 存量配置里的 "auto"（历史值：有本地用本地否则 API）读取时映射为 "api"。
_BACKENDS = ("local", "api", "off")
_BACKEND_ALIASES = {"auto": "api"}

# 节点注册表：kind=encoder 编码型（本地有专有模型）/ generative 生成型（本地=对话小模型）
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
    {
        "id": "emotion_llm",
        "name": "情绪深度分析",
        "kind": "generative",
        "desc": "LLM 情绪 PAD 分析与深层需求提取",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "portrait",
        "name": "用户画像",
        "kind": "generative",
        "desc": "用户画像整合与更新（memory_encoding）",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "nudge",
        "name": "主动问候",
        "kind": "generative",
        "desc": "主动问候 / 轻推生成（nudge）",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "reunion",
        "name": "重聚反思",
        "kind": "generative",
        "desc": "用户回来时的重聚欢迎语",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "growth",
        "name": "成长叙事",
        "kind": "generative",
        "desc": "每日成长叙事生成",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "memory_distill",
        "name": "记忆蒸馏",
        "kind": "generative",
        "desc": "旧记忆压缩为摘要（memory_encoding）",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "spontaneous_recall",
        "name": "自发回忆",
        "kind": "generative",
        "desc": "空闲时随机回忆生成内心独白",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
    {
        "id": "dream",
        "name": "梦境整合",
        "kind": "generative",
        "desc": "梦境 6 阶段偏好结晶（LLM 蒸馏）",
        "api_model": "硅基流动免费模型",
        "local_model": "",
        "local_desc": "本地部署的对话小模型",
        "default": "api",
    },
]

_NODE_MAP = {node["id"]: node for node in NODES}


def valid_backend(value: str) -> bool:
    return value in _BACKENDS


def get_backend(cfg: Any, node_id: str) -> str:
    """读取节点当前配置的后端值（默认 api；asr 默认 api）。

    历史值 "auto" 映射为 "api"（后端选择已取消 auto，只剩 local/api/off）。
    """
    if node_id not in _NODE_MAP:
        raise ValueError(f"unknown model node: {node_id}")
    try:
        value = str(
            cfg.get(f"local_deploy.nodes.{node_id}", "")
            or _NODE_MAP[node_id]["default"]
        ).strip().lower()
    except Exception:  # noqa: BLE001
        value = _NODE_MAP[node_id]["default"]
    value = _BACKEND_ALIASES.get(value, value)
    return value if valid_backend(value) else _NODE_MAP[node_id]["default"]


def set_backend(cfg: Any, node_id: str, backend: str, local_model: str | None = None) -> str:
    """校验并持久化节点后端选择，返回归一化后的值。

    选择 local 时可指定具体本地模型（local_model，如已安装的 bge 仓库）；
    未指定时使用节点默认本地模型（内置模型 / 对话小模型）。
    """
    if node_id not in _NODE_MAP:
        raise ValueError(f"unknown model node: {node_id}")
    backend = str(backend or "").strip().lower()
    backend = _BACKEND_ALIASES.get(backend, backend)
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
