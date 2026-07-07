"""Skills/工具路由（R6）：列表、全局开关、调试执行、统计。"""
from __future__ import annotations
from typing import Any

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user

router = APIRouter(tags=["tools"], dependencies=[Depends(get_current_user)])


def _cfg() -> Any:
    from web.config_service import get_config_service
    return get_config_service()


def _tool_source(name: str) -> str:
    if name.startswith("mcp_"):
        parts = name.split("_", 2)
        if len(parts) >= 3:
            return f"mcp:{parts[1]}"
    if name.startswith("sdk_"):
        parts = name.split("_", 2)
        if len(parts) >= 3:
            return f"sdk_mcp:{parts[1]}"
    return "builtin"


def list_tools_meta() -> list[dict]:
    from tool_engine.tool_registry import to_openai_tools, list_tools
    to_openai_tools()  # 确保所有工具模块已导入注册
    out = []
    for t in list_tools():
        perm = t.get("permission")
        out.append({
            "name": t["name"],
            "description": t.get("description", ""),
            "category": t.get("category", "general"),
            "permission": getattr(perm, "value", str(perm or "")),
            "max_frequency": t.get("max_frequency", 10),
            "requires_confirmation": bool(t.get("requires_confirmation", False)),
            "source": t.get("source", "builtin"),
            "plugin_id": t.get("plugin_id", ""),
            "enabled": t.get("enabled", True) is not False and t.get("max_frequency", 10) != 0,
            "schema": t.get("schema", {}),
        })
    return sorted(out, key=lambda x: (x["source"], x["category"], x["name"]))


def apply_tool_overrides() -> None:
    """启动时调用：把 webui_overrides 中的工具设置应用到 registry。"""
    from tool_engine.tool_registry import get_tool
    overrides = _cfg().get("tools", {}) or {}
    for name, o in overrides.items():
        tool = get_tool(name)
        if not tool or not isinstance(o, dict):
            continue
        if "enabled" in o:
            tool["enabled"] = bool(o["enabled"])
        if "max_frequency" in o:
            tool["max_frequency"] = int(o["max_frequency"])
        if "requires_confirmation" in o:
            tool["requires_confirmation"] = bool(o["requires_confirmation"])


@router.get("/tools", response_model=Envelope[list[dict]])
async def get_tools() -> Any:
    return Envelope(data=list_tools_meta())


@router.put("/tools/{name}", response_model=Envelope[dict])
async def update_tool(name: str, body: dict, request: Request) -> Any:
    from tool_engine.tool_registry import get_tool, to_openai_tools
    to_openai_tools()
    tool = get_tool(name)
    if not tool:
        raise HTTPException(404, f"工具 {name} 不存在")
    cfg = _cfg()
    override = cfg.get(f"tools.{name}", {}) or {}
    if "enabled" in body:
        tool["enabled"] = bool(body["enabled"])
        override["enabled"] = bool(body["enabled"])
    if "max_frequency" in body and body["max_frequency"] is not None:
        mf = max(0, min(int(body["max_frequency"]), 6000))
        tool["max_frequency"] = mf
        override["max_frequency"] = mf
    if "requires_confirmation" in body:
        tool["requires_confirmation"] = bool(body["requires_confirmation"])
        override["requires_confirmation"] = bool(body["requires_confirmation"])
    cfg.set(f"tools.{name}", override)
    core = request.app.state.core
    await core.db.insert_audit_log("webui.tools.update", "webui",
                                   json.dumps({name: body}, ensure_ascii=False))
    await core.db.commit()
    try:
        from web.ws_hub import manager
        await manager.broadcast({"type": "config_changed", "domain": "tools"})
    except Exception:
        logger.debug("tools.broadcast_config_changed_failed", exc_info=True)
    return Envelope(data={"name": name,
                          "enabled": tool.get("enabled", True) is not False,
                          "max_frequency": tool.get("max_frequency"),
                          "requires_confirmation": tool.get("requires_confirmation")})


@router.post("/tools/{name}/invoke", response_model=Envelope[dict])
async def invoke_tool(name: str, body: dict, request: Request) -> Any:
    """调试执行（真实执行，走完整审计）。"""
    from tool_engine.tool_registry import get_tool, to_openai_tools
    to_openai_tools()
    tool = get_tool(name)
    if not tool:
        raise HTTPException(404, f"工具 {name} 不存在")
    if tool.get("requires_confirmation") and request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "该工具需要确认执行，缺少 X-Confirm: yes 头")
    core = request.app.state.core
    args = body.get("args") or {}
    if not isinstance(args, dict):
        raise HTTPException(400, "args 必须是对象")
    t0 = time.time()
    result = await core.tool_executor.execute(name, args, user_id="webui")
    return Envelope(data={
        "success": result.success,
        "data": str(result.data)[:5000] if result.data is not None else None,
        "error": result.error,
        "elapsed_ms": int((time.time() - t0) * 1000),
    })


@router.get("/tools/{name}/stats", response_model=Envelope[dict])
async def tool_stats(name: str, request: Request, days: int = Query(default=7, ge=1, le=90)) -> Any:
    core = request.app.state.core
    since = time.time() - days * 86400
    row = await core.db.fetch_one(
        "SELECT COUNT(*) AS calls, "
        "SUM(CASE WHEN detail LIKE '%\"success\": true%' OR detail LIKE '%success=True%' THEN 1 ELSE 0 END) AS ok "
        "FROM audit_logs WHERE event_type LIKE 'tool%' AND detail LIKE ? AND timestamp > ?",
        (f"%{name}%", since))
    from utils.metrics import metrics
    snap = metrics.get_snapshot()
    counters = snap.get("counters", {}) if isinstance(snap, dict) else {}
    return Envelope(data={
        "name": name,
        "calls": (row or {}).get("calls", 0),
        "ok": (row or {}).get("ok", 0),
        "success_counter": counters.get(f"tool_execute.{name}.success", 0),
        "failure_counter": counters.get(f"tool_execute.{name}.failure", 0),
    })


@router.get("/tools/limits", response_model=Envelope[dict])
async def tool_limits() -> Any:
    """获取工具数量上限和当前使用情况"""
    from tool_engine.tool_registry import get_tool_stats
    stats = get_tool_stats()
    return Envelope(data=stats)


@router.post("/tools/{name}/test", response_model=Envelope[dict])
async def test_tool(name: str, request: Request) -> Any:
    """测试单个工具是否可用（真实执行，安全参数）"""
    import asyncio
    from tool_engine.tool_registry import get_tool
    from tool_engine.tool_executor import ToolExecutor

    tool = get_tool(name)
    if not tool:
        raise HTTPException(404, f"工具 '{name}' 不存在")

    # 构造安全的测试参数
    test_args = _build_safe_test_args(tool)
    if test_args is None:
        return Envelope(data={
            "name": name,
            "status": "skip",
            "message": "该工具没有安全的测试方式（需要用户参数或涉及危险操作）",
        })

    # 执行测试
    core = request.app.state.core
    executor = ToolExecutor(core.db)
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(
            executor.execute(name, test_args, "webui-test"),
            timeout=15,
        )
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        if hasattr(result, "success"):
            ok = result.success
            data = str(result.data)[:500] if result.data else ""
            error = result.error if not ok else ""
        else:
            ok = True
            data = str(result)[:500]
            error = ""
        return Envelope(data={
            "name": name,
            "status": "ok" if ok else "fail",
            "elapsed_ms": elapsed_ms,
            "data": data,
            "error": error,
        })
    except asyncio.TimeoutError:
        return Envelope(data={
            "name": name,
            "status": "timeout",
            "elapsed_ms": 15000,
            "error": "测试超时（15 秒）",
        })
    except Exception as e:
        elapsed_ms = round((time.monotonic() - t0) * 1000, 1)
        return Envelope(data={
            "name": name,
            "status": "error",
            "elapsed_ms": elapsed_ms,
            "error": str(e)[:500],
        })


def _build_safe_test_args(tool: dict) -> dict | None:
    """为工具构造安全的测试参数，无法安全测试时返回 None"""
    name = tool.get("name", "")
    schema = tool.get("schema", {})
    props = schema.get("properties", {})
    required = schema.get("required", [])

    # 无需参数的工具直接调用
    if not props or not required:
        return {}

    # 只需要简单类型参数且有默认值的工具
    # 对于需要复杂参数的工具，返回 None 表示无法自动测试
    safe_tools = {
        # 文件工具：list 不需要路径
        "list_files": {},
        # 记忆工具
        "recall_memory": {"query": "test", "top_k": 1},
        # 天气工具
        "get_weather": {"city": "北京"},
        # 搜索工具
        "multi_search": {"query": "test"},
        # 角色扮演
        "manage_persona": {"action": "get"},
        # 情感记忆
        "emotional_memory": {"action": "get_stats"},
        # 知识库
        "rag_search": {"query": "test", "top_k": 1},
    }
    if name in safe_tools:
        return safe_tools[name]

    # MCP 工具：尝试无参数调用
    if name.startswith("mcp_") or name.startswith("sdk_"):
        return {}

    # 其他工具：无法安全测试
    return None


# ── Skills（SKILL.md 知识注入）────────────────────────────────────

def _skills_dir() -> Any:
    from config import WORKSPACE_DIR
    d = WORKSPACE_DIR / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_skill_name(name: str) -> str:
    import re
    name = (name or "").strip()
    if not re.fullmatch(r"[\w一-鿿-]{1,64}", name):
        raise HTTPException(400, "skill 名称只能含字母/数字/下划线/中文/连字符，≤64字符")
    return name


@router.get("/skills", response_model=Envelope[list[dict]])
async def list_skills() -> Any:
    from config import load_skills
    return Envelope(data=[
        {"name": s["name"], "size": len(s["content"]),
         "preview": s["content"][:120]}
        for s in load_skills()])


@router.get("/skills/{name}", response_model=Envelope[dict])
async def get_skill(name: str) -> Any:
    fp = _skills_dir() / f"{_safe_skill_name(name)}.md"
    if not fp.exists():
        raise HTTPException(404, f"Skill {name} 不存在")
    return Envelope(data={"name": name, "content": fp.read_text(encoding="utf-8-sig")})


@router.put("/skills/{name}", response_model=Envelope[dict])
async def save_skill(name: str, body: dict, request: Request) -> Any:
    """新建/覆盖 skill。content 即 SKILL.md 全文，保存后下一条消息生效。"""
    name = _safe_skill_name(name)
    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(400, "content 不能为空")
    if len(content) > 256 * 1024:
        raise HTTPException(400, "SKILL.md 不能超过 256KB")
    (_skills_dir() / f"{name}.md").write_text(content, encoding="utf-8-sig")
    core = request.app.state.core
    try:
        await core.db.insert_audit_log("webui.skills.save", "webui", name)
        await core.db.commit()
    except Exception:
        logger.debug("tools.audit_save_failed", exc_info=True)
    return Envelope(data={"name": name, "saved": True})


@router.delete("/skills/{name}", response_model=Envelope[dict])
async def delete_skill(name: str, request: Request) -> Any:
    fp = _skills_dir() / f"{_safe_skill_name(name)}.md"
    if not fp.exists():
        raise HTTPException(404, f"Skill {name} 不存在")
    fp.unlink()
    core = request.app.state.core
    try:
        await core.db.insert_audit_log("webui.skills.delete", "webui", name)
        await core.db.commit()
    except Exception:
        logger.debug("tools.audit_delete_failed", exc_info=True)
    return Envelope(data={"deleted": name})