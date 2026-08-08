"""本地部署路由：向量嵌入引擎管理（API / 本地模型切换、启动/停止、日志）。

WebUI 侧边栏"本地部署"页：选择向量嵌入引擎——远程 API（硅基流动）
或内置本地 BGE 模型（NPU/CPU）。使用本地模型前必须先"启动"，
页面下方展示启动/运行日志。
"""
from __future__ import annotations
from typing import Any

import asyncio
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user
from web.config_service import get_config_service

router = APIRouter(tags=["local-deploy"], dependencies=[Depends(get_current_user)])


def _get_vector_store(request: Request) -> Any:
    """从全局 core 获取 VectorStore 单例（可能为 None：向量库未初始化）。"""
    core = getattr(request.app.state, "core", None)
    return getattr(core, "_vec_store", None) if core is not None else None


def _fallback_status() -> dict:
    """向量库不可用时返回的环境状态（只读，页面仍可展示）。"""
    return {
        "mode": os.getenv("EMBED_MODE", "remote"),
        "engine_running": False,
        "backend": os.getenv("LOCAL_EMBED_BACKEND", "auto"),
        "api_configured": bool(
            os.getenv("SILICONFLOW_API_KEY") or os.getenv("EMBED_API_KEY")),
        "model_dir": "",
        "dimensions": 0,
        "available": False,
    }


@router.get("/local-deploy/status", response_model=Envelope[dict])
async def local_deploy_status(request: Request) -> Any:
    """当前 embedding 引擎状态：模式 / 本地引擎运行状态 / API 配置。"""
    vs = _get_vector_store(request)
    if vs is None:
        logger.info("local_deploy.status vector_store_unavailable")
        return Envelope(data=_fallback_status())
    status = vs.embed_engine_status()
    status["available"] = True
    return Envelope(data=status)


@router.post("/local-deploy/mode", response_model=Envelope[dict])
async def local_deploy_set_mode(request: Request, body: dict) -> Any:
    """切换 embedding 引擎：local=本地模型 / remote=远程 API（热生效 + 持久化）。"""
    vs = _get_vector_store(request)
    if vs is None:
        raise HTTPException(status_code=409, detail="Vector store not initialized")
    mode = str((body or {}).get("mode", "")).strip().lower()
    if mode not in ("local", "remote"):
        raise HTTPException(status_code=422, detail="mode must be 'local' or 'remote'")
    status = await asyncio.to_thread(vs.set_embed_mode, mode)
    get_config_service().set("local_deploy.mode", mode)
    logger.info("local_deploy.mode_switched mode={}", mode)
    return Envelope(data=status)


@router.post("/local-deploy/start", response_model=Envelope[dict])
async def local_deploy_start(request: Request) -> Any:
    """启动本地 embedding 引擎：预加载模型（含 NPU 探测），必须先启动再使用。"""
    vs = _get_vector_store(request)
    if vs is None:
        raise HTTPException(status_code=409, detail="Vector store not initialized")
    status = await asyncio.to_thread(vs.start_local_engine)
    get_config_service().set("local_deploy.mode", "local")
    return Envelope(data=status)


@router.post("/local-deploy/stop", response_model=Envelope[dict])
async def local_deploy_stop(request: Request) -> Any:
    """停止本地 embedding 引擎：释放 onnxruntime session / NPU 常驻进程。"""
    vs = _get_vector_store(request)
    if vs is None:
        raise HTTPException(status_code=409, detail="Vector store not initialized")
    status = await asyncio.to_thread(vs.stop_local_engine)
    return Envelope(data=status)


@router.get("/local-deploy/logs", response_model=Envelope[list[str]])
async def local_deploy_logs(request: Request, limit: int = 60) -> Any:
    """返回本地部署相关日志（agent.log 尾部，过滤 embedding/NPU 关键词）。"""
    from config import LOG_DIR
    log_file = LOG_DIR / "agent.log"
    n = min(max(int(limit), 10), 500)
    tail: list[str] = []
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                tail = f.readlines()[-500:]
        except OSError:
            tail = []
    keywords = ("embed", "npu", "vector_store.local", "adaptive", "local_deploy")
    lines = [ln.rstrip("\n") for ln in tail
             if any(k in ln.lower() for k in keywords)][-n:]
    return Envelope(data=lines)
