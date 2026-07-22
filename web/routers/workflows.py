"""工作流路由：CRUD + 提示词预览 + Skill 文件同步。

工作流 JSON 存储到 WORKSPACE_DIR/workflows/{wf_id}.json，
保存时自动生成 Skill 文件到 WORKSPACE_DIR/skills/wf_{name}.md，
供 prompt_builder.load_skills() 注入 system prompt。
"""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger

from web.routers.auth import get_current_user
from web.schemas import Envelope

router = APIRouter(tags=["workflows"], dependencies=[Depends(get_current_user)])


# 节点 type → 中文名映射
_TYPE_CN = {
    "tool": "工具调用",
    "skill": "技能引用",
    "mcp": "MCP工具",
    "agent": "子智能体委托",
    "model": "模型调用",
    "step": "操作说明",
}

_VALID_TYPES = set(_TYPE_CN.keys())


def _workflows_dir() -> Any:
    from config import WORKSPACE_DIR
    d = WORKSPACE_DIR / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _skills_dir() -> Any:
    from config import WORKSPACE_DIR
    d = WORKSPACE_DIR / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_wf_id(wf_id: str) -> str:
    """校验 wf_id 防路径穿越，参考 tools._safe_skill_name。"""
    wf_id = (wf_id or "").strip()
    if not re.fullmatch(r"[\w一-鿿-]{1,64}", wf_id):
        raise HTTPException(400, "工作流 ID 只能含字母/数字/下划线/中文/连字符，≤64字符")
    return wf_id


def _wf_path(wf_id: str) -> Any:
    return _workflows_dir() / f"{wf_id}.json"


def _skill_path(name: str) -> Any:
    name = (name or "").strip().replace("/", "").replace("\\", "").replace("..", "")
    if not name:
        raise HTTPException(400, "工作流 name 不能为空")
    return _skills_dir() / f"wf_{name}.md"


def _validate_nodes(nodes: list) -> None:
    """校验节点 type 枚举。"""
    if not isinstance(nodes, list):
        raise HTTPException(400, "nodes 必须是数组")
    for node in nodes:
        if not isinstance(node, dict):
            raise HTTPException(400, "节点必须是对象")
        ntype = node.get("type", "step")
        if ntype not in _VALID_TYPES:
            raise HTTPException(400, f"节点 type 非法: {ntype}，允许值: {', '.join(sorted(_VALID_TYPES))}")


def generate_workflow_prompt(workflow: dict) -> str:
    """将工作流节点链转为结构化 Markdown 提示词。"""
    name = workflow.get("name", "")
    description = workflow.get("description", "")
    nodes = workflow.get("nodes", []) or []

    lines: list[str] = []
    lines.append(f"# 工作流: {name}")
    lines.append("")
    if description:
        lines.append(description)
        lines.append("")
    lines.append("## 执行规则")
    lines.append("1. 严格按照下方步骤顺序执行，不可跳过或重排")
    lines.append("2. 每个步骤完成后检查输出是否符合“预期结果”")
    lines.append("3. 如步骤失败，向用户报告错误信息，不要自行跳到下一步")
    lines.append("4. 需要用户操作时（如浏览器授权），明确提示并等待")
    lines.append("5. 全部步骤完成后，输出最终结果摘要")
    lines.append("")
    lines.append("## 执行步骤")
    lines.append("")

    for i, node in enumerate(nodes, 1):
        label = node.get("label", node.get("id", f"步骤{i}"))
        ntype = node.get("type", "step")
        type_cn = _TYPE_CN.get(ntype, ntype)
        ref = node.get("ref", "")
        params = node.get("params", {}) or {}
        note = node.get("note", "")
        expect = node.get("expect", "")

        lines.append(f"### 步骤 {i}: {label}")
        lines.append(f"- **类型**: {type_cn}")
        if ntype != "step":
            lines.append(f"- **工具/目标**: {ref}")
            param_str = ", ".join(f"{k}={v}" for k, v in params.items())
            lines.append(f"- **参数**: {param_str}")
        if note:
            lines.append(f"- **操作说明**: {note}")
        if expect:
            lines.append(f"- **预期结果**: {expect}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _write_skill_file(workflow: dict) -> None:
    """根据工作流生成 Skill 提示词文件。"""
    name = workflow.get("name", "")
    if not name:
        return
    try:
        content = generate_workflow_prompt(workflow)
        _skill_path(name).write_text(content, encoding="utf-8-sig")
        logger.info("workflow.skill_written name={}", name)
    except Exception as e:
        logger.warning("workflow.skill_write_failed name={} error={}", name, str(e))


def _remove_skill_file(name: str) -> None:
    """删除工作流对应的 Skill 文件。"""
    if not name:
        return
    fp = _skill_path(name)
    if fp.exists():
        try:
            fp.unlink()
            logger.info("workflow.skill_removed name={}", name)
        except Exception as e:
            logger.warning("workflow.skill_remove_failed name={} error={}", name, str(e))


@router.get("/workflows", response_model=Envelope[list[dict]])
async def list_workflows() -> Any:
    """列出所有工作流。"""
    out = []
    d = _workflows_dir()
    for fp in sorted(d.glob("*.json")):
        try:
            wf = json.loads(fp.read_text(encoding="utf-8"))
            wf["node_count"] = len(wf.get("nodes", []))
            out.append(wf)
        except Exception as e:
            logger.warning("workflow.list_read_failed file={} error={}", fp.name, str(e))
    return Envelope(data=out)


@router.get("/workflows/{wf_id}", response_model=Envelope[dict])
async def get_workflow(wf_id: str) -> Any:
    """获取单个工作流。"""
    wf_id = _safe_wf_id(wf_id)
    fp = _wf_path(wf_id)
    if not fp.exists():
        raise HTTPException(404, f"工作流 {wf_id} 不存在")
    try:
        wf = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"工作流文件读取失败: {e}") from None
    return Envelope(data=wf)


@router.post("/workflows", response_model=Envelope[dict])
async def create_workflow(body: dict, request: Request) -> Any:
    """新建工作流。"""
    wf_id = _safe_wf_id(body.get("id", ""))
    if not body.get("name"):
        raise HTTPException(400, "name 不能为空")
    nodes = body.get("nodes", []) or []
    _validate_nodes(nodes)
    fp = _wf_path(wf_id)
    if fp.exists():
        raise HTTPException(409, f"工作流 {wf_id} 已存在")
    body["id"] = wf_id
    fp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    if body.get("enabled", True):
        _write_skill_file(body)
    core = request.app.state.core
    try:
        await core.db.insert_audit_log("webui.workflows.create", "webui", wf_id)
        await core.db.commit()
    except Exception:
        logger.debug("workflows.audit_create_failed", exc_info=True)
    logger.info("workflow.created id={}", wf_id)
    return Envelope(data=body)


@router.put("/workflows/{wf_id}", response_model=Envelope[dict])
async def update_workflow(wf_id: str, body: dict, request: Request) -> Any:
    """更新工作流。"""
    wf_id = _safe_wf_id(wf_id)
    fp = _wf_path(wf_id)
    if not fp.exists():
        raise HTTPException(404, f"工作流 {wf_id} 不存在")
    if not body.get("name"):
        raise HTTPException(400, "name 不能为空")
    nodes = body.get("nodes", []) or []
    _validate_nodes(nodes)

    # 读取旧工作流，处理 name 变更导致的旧 Skill 文件残留
    old_name = ""
    try:
        old_wf = json.loads(fp.read_text(encoding="utf-8"))
        old_name = old_wf.get("name", "")
    except Exception:
        logger.debug("workflows.read_old_name_failed", exc_info=True)

    body["id"] = wf_id
    fp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")

    new_name = body.get("name", "")
    if old_name and old_name != new_name:
        _remove_skill_file(old_name)

    if body.get("enabled", True):
        _write_skill_file(body)
    else:
        _remove_skill_file(new_name)

    core = request.app.state.core
    try:
        await core.db.insert_audit_log("webui.workflows.update", "webui", wf_id)
        await core.db.commit()
    except Exception:
        logger.debug("workflows.audit_update_failed", exc_info=True)
    logger.info("workflow.updated id={}", wf_id)
    return Envelope(data=body)


@router.delete("/workflows/{wf_id}", response_model=Envelope[dict])
async def delete_workflow(wf_id: str, request: Request) -> Any:
    """删除工作流。"""
    wf_id = _safe_wf_id(wf_id)
    fp = _wf_path(wf_id)
    if not fp.exists():
        raise HTTPException(404, f"工作流 {wf_id} 不存在")

    # 读取 name 以清理对应 Skill 文件
    name = ""
    try:
        wf = json.loads(fp.read_text(encoding="utf-8"))
        name = wf.get("name", "")
    except Exception:
        logger.debug("workflows.read_name_before_delete_failed", exc_info=True)

    fp.unlink()
    _remove_skill_file(name)
    core = request.app.state.core
    try:
        await core.db.insert_audit_log("webui.workflows.delete", "webui", wf_id)
        await core.db.commit()
    except Exception:
        logger.debug("workflows.audit_delete_failed", exc_info=True)
    logger.info("workflow.deleted id={}", wf_id)
    return Envelope(data={"deleted": wf_id})


@router.get("/workflows/{wf_id}/preview", response_model=Envelope[dict])
async def preview_workflow(wf_id: str) -> Any:
    """预览生成的提示词。"""
    wf_id = _safe_wf_id(wf_id)
    fp = _wf_path(wf_id)
    if not fp.exists():
        raise HTTPException(404, f"工作流 {wf_id} 不存在")
    try:
        wf = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"工作流文件读取失败: {e}") from None
    prompt = generate_workflow_prompt(wf)
    return Envelope(data={"prompt": prompt})
