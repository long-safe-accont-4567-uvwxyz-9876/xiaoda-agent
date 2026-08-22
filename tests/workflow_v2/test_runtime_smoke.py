# tests/workflow_v2/test_runtime_smoke.py
"""M1 装配冒烟：build_runtime 把真实 core 能力接进 UnifiedExecutor，
整体（scheduler→executor→driver→库）端到端跑通一个 TOOL 节点工作流。"""
import asyncio
import time

import aiosqlite
import pytest

from db.db_workflow import create_schema
from tool_engine.tool_registry import ToolResult
from workflow_v2.app import build_runtime
from workflow_v2.models import (
    EdgeSpec, NodeSpec, NodeType, RunStatus, StepStatus,
    WorkflowRevision, WorkflowRun, WorkflowRunEvent,
)
from workflow_v2.repository import WorkflowRepository


class _Tool:
    def __init__(self):
        self.calls = []

    async def execute(self, tool_name, args, user_id=""):
        self.calls.append((tool_name, args, user_id))
        return ToolResult.ok({"echo": args})


class _Router:
    def __init__(self):
        self.calls = []

    async def route(self, task_type=None, messages=None, **kw):
        self.calls.append((task_type, messages, kw))
        return "smoke answer"

    async def route_config(self, config, messages, **kw):
        self.calls.append(("route_config", config, messages, kw))
        return "smoke answer"


class _Core:
    """富 fake core：只有 workflow 执行需要的能力。"""

    def __init__(self):
        self.tool_executor = _Tool()
        self.router = _Router()
        self.security = None
        self.secrets_broker = None


def _rev():
    nodes = [
        NodeSpec(id="start", type=NodeType.START, name="start"),
        NodeSpec(id="t1", type=NodeType.TOOL, name="t1",
                 config={"tool_ref": "smoke_tool", "arguments": {"q": 1}}),
        NodeSpec(id="m1", type=NodeType.MODEL, name="m1", config={"note": "总结"}),
        NodeSpec(id="end", type=NodeType.END, name="end"),
    ]
    edges = [EdgeSpec(source="start", target="t1"),
             EdgeSpec(source="t1", target="m1"),
             EdgeSpec(source="m1", target="end")]
    return WorkflowRevision(revision_id="rev1", workflow_id="wf1", nodes=nodes, edges=edges)


@pytest.mark.asyncio
async def test_build_runtime_runs_tool_and_model_node(tmp_path):
    db_path = str(tmp_path / "wf_smoke.db")
    # 与生产一致：先迁移建表（legacy_migration v27），再启动 driver
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    repo = WorkflowRepository(conn)
    await repo.upsert_definition(workflow_id="wf1", name="smoke")
    rev = _rev()
    await repo.insert_revision(rev)
    await repo.set_current_revision("wf1", "rev1")
    run = WorkflowRun(run_id="r1", workflow_id="wf1", revision_id="rev1",
                      created_at=time.time())
    await repo.create_run(run, [], WorkflowRunEvent(
        run_id="r1", seq=1, event_type="run_queued", run_status=RunStatus.QUEUED,
        timestamp=time.time()))
    await conn.close()

    core = _Core()
    svc, driver = await build_runtime(core, db_path)
    # M3 灰度门控已接线：测试库默认关，显式打开全局开关再驱动轮询
    await svc.set_config("workflow_v2.enabled", True)
    try:
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        repo2 = WorkflowRepository(conn)
        # 手动驱动轮询，等 run 终态
        final = None
        for _ in range(30):
            await driver._poll_once()
            final = await repo2.get_run("r1")
            if final.status in (RunStatus.SUCCEEDED, RunStatus.FAILED,
                                RunStatus.CANCELLED):
                break
            await asyncio.sleep(0.05)
        assert final is not None
        assert final.status == RunStatus.SUCCEEDED, final.status
        # 工具与模型通道各自被命中
        assert core.tool_executor.calls == [("smoke_tool", {"q": 1}, "workflow")]
        assert core.router.calls and core.router.calls[0][0] == "route_config"
        steps = await repo2.list_steps("r1")
        by_id = {s.node_id: s.status for s in steps}
        assert by_id["t1"] == StepStatus.SUCCEEDED
        assert by_id["m1"] == StepStatus.SUCCEEDED
    finally:
        await driver.stop()