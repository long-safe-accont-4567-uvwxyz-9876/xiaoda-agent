# tests/workflow_v2/test_m3_gray.py
"""M3 灰度/迁移验收：灰度门控（服务/路由/驱动）+ v1→v2 幂等迁移 + CLI。

规范来源 = 立项书 §6（决策"试点白名单"，2026-08-22 已拍板）：
- 开关用 DB config 键，不新增环境变量：workflow_v2.enabled（默认 false）
  + workflow_v2.pilot_wf_ids（JSON 数组）；
- 生效规则：全局开 或 wf_id 在白名单 → 该流可用；路由 503、driver 不调度。
"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import aiosqlite
import pytest

from db.db_workflow import create_schema
from workflow_v2.repository import WorkflowRepository
from workflow_v2.service import WorkflowV2Service

# ---------------------------------------------------------------------------
# 服务层：灰度开关读/写语义
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gray_default_off(repo: WorkflowRepository):
    svc = WorkflowV2Service(repo)
    assert await svc.v2_global_enabled() is False
    assert await svc.v2_pilot_ids() == []
    assert await svc.is_wf_enabled("w1") is False  # 全局关 + 不在白名单


@pytest.mark.asyncio
async def test_global_on_enables_every_wf(repo: WorkflowRepository):
    svc = WorkflowV2Service(repo)
    await svc.set_config("workflow_v2.enabled", True)
    assert await svc.v2_global_enabled() is True
    assert await svc.is_wf_enabled("anything") is True


@pytest.mark.asyncio
async def test_pilot_whitelist_only(repo: WorkflowRepository):
    svc = WorkflowV2Service(repo)
    await svc.set_config("workflow_v2.pilot_wf_ids", ["wf-a", "wf-b"])
    assert await svc.v2_global_enabled() is False
    assert await svc.is_wf_enabled("wf-a") is True
    assert await svc.is_wf_enabled("wf-b") is True
    assert await svc.is_wf_enabled("wf-c") is False


@pytest.mark.asyncio
async def test_config_upsert_and_roundtrip(repo: WorkflowRepository):
    svc = WorkflowV2Service(repo)
    await svc.set_config("workflow_v2.enabled", False)
    await svc.set_config("workflow_v2.enabled", True)  # upsert 覆盖
    assert await svc.v2_global_enabled() is True
    await svc.set_config("workflow_v2.pilot_wf_ids", ["x"])
    await svc.set_config("workflow_v2.pilot_wf_ids", ["x", "y"])
    assert await svc.v2_pilot_ids() == ["x", "y"]


@pytest.mark.asyncio
async def test_pilot_tolerates_legacy_string_value(repo: WorkflowRepository):
    """容忍历史形态：值被直接存为 JSON 字符串而非解码后的数组。"""
    svc = WorkflowV2Service(repo)
    await svc.repo.conn.execute(
        "INSERT INTO wf_config(key, value) VALUES(?,?)",
        ("workflow_v2.pilot_wf_ids", '["old-a"]'),
    )
    await svc.repo.conn.commit()
    assert await svc.v2_pilot_ids() == ["old-a"]


@pytest.mark.asyncio
async def test_get_config_missing_table_falls_back(repo: WorkflowRepository):
    """表未建（旧库未迁移）→ 返回 default 而不抛异常。"""
    svc = WorkflowV2Service(repo)
    await svc.repo.conn.execute("DROP TABLE wf_config")
    await svc.repo.conn.commit()
    assert await svc.get_config("workflow_v2.enabled", "off") == "off"
    assert await svc.is_wf_enabled("w") is False


# ---------------------------------------------------------------------------
# 路由层：POST /runs 门控 + v2-status 端点
# ---------------------------------------------------------------------------

async def test_runs_blocked_when_gray_off(client, app, auth_headers):
    svc = app.state.workflow_v2
    await svc.set_config("workflow_v2.enabled", False)
    r = await client.post("/api/v1/workflows/w1/runs", headers=auth_headers, json={"input": {}})
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "WORKFLOW_V2_DISABLED"


async def test_runs_allowed_when_pilot_listed(client, app, auth_headers, seeded_definition):
    svc = app.state.workflow_v2
    await svc.set_config("workflow_v2.enabled", False)
    await svc.set_config("workflow_v2.pilot_wf_ids", ["w1"])
    r = await client.post("/api/v1/workflows/w1/runs", headers=auth_headers, json={"input": {}})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["status"] == "queued"


async def test_v2_status_endpoint_reflects_gray(client, app, auth_headers):
    svc = app.state.workflow_v2
    await svc.set_config("workflow_v2.enabled", False)
    r = await client.get("/api/v1/workflows/w1/v2-status", headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["data"] == {"enabled": False, "global_enabled": False,
                                "whitelisted": False}
    await svc.set_config("workflow_v2.enabled", True)
    r = await client.get("/api/v1/workflows/w1/v2-status", headers=auth_headers)
    assert r.json()["data"]["enabled"] is True
    assert r.json()["data"]["global_enabled"] is True


# ---------------------------------------------------------------------------
# 驱动层：门控工作机制 —— 未开放工作流保持队列，打开后自动续跑
# ---------------------------------------------------------------------------

class FakeOkExecutor:
    """任意节点直接成功的假执行器（结构无关，只测调度）。"""

    async def __call__(self, node, step, ctx):
        from workflow_v2.models import StepStatus
        from workflow_v2.scheduler import NodeResult
        return NodeResult(status=StepStatus.SUCCEEDED, output={"id": node.id})


@pytest.mark.asyncio
async def test_driver_skips_disabled_queued_run_then_resumes(tmp_path):
    from workflow_v2.app import WorkflowDriver
    from workflow_v2.models import (
        EdgeSpec,
        NodeSpec,
        NodeType,
        RunStatus,
        WorkflowRevision,
        WorkflowRun,
        WorkflowRunEvent,
    )
    from workflow_v2.scheduler import Scheduler

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    repo = WorkflowRepository(conn)
    await repo.upsert_definition(workflow_id="wf-g", name="gray")
    rev = WorkflowRevision(revision_id="rev-g", workflow_id="wf-g", nodes=[
        NodeSpec(id="start", type=NodeType.START, name="start"),
        NodeSpec(id="end", type=NodeType.END, name="end"),
    ], edges=[EdgeSpec(source="start", target="end")])
    await repo.insert_revision(rev)
    await repo.set_current_revision("wf-g", "rev-g")
    run = WorkflowRun(run_id="r-g", workflow_id="wf-g", revision_id="rev-g",
                      created_at=time.time())
    await repo.create_run(run, [], WorkflowRunEvent(
        run_id="r-g", seq=1, event_type="run_queued", run_status=RunStatus.QUEUED,
        timestamp=time.time()))

    state = {"on": False}
    scheduler = Scheduler(repo, FakeOkExecutor(), repo.get_revision)
    async def gate(wf_id: str) -> bool:
        return state["on"]

    driver = WorkflowDriver(repo, scheduler, is_enabled=gate)
    try:
        # 门控关：轮询 3 次仍保持 QUEUED、无 step 被认领
        for _ in range(3):
            await driver._poll_once()
        got = await repo.get_run("r-g")
        assert got.status == RunStatus.QUEUED
        assert await repo.list_steps("r-g") == []
        # 门控开：下一轮自动跑完
        state["on"] = True
        for _ in range(5):
            await driver._poll_once()
            if (await repo.get_run("r-g")).status == RunStatus.SUCCEEDED:
                break
        assert (await repo.get_run("r-g")).status == RunStatus.SUCCEEDED
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 服务层：migrate_workflow 幂等迁移（CLI 核心语义）
# ---------------------------------------------------------------------------

def _v1(name: str, label: str) -> dict:
    return {
        "id": name, "name": name, "description": "",
        "version": "1.0.0", "enabled": True,
        "nodes": [{"id": "n1", "type": "tool", "label": label,
                   "ref": "web_search", "note": "", "params": {"q": 1}}],
        "edges": [],
    }


@pytest.mark.asyncio
async def _migrate_svc(tmp_path):
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    svc = WorkflowV2Service(WorkflowRepository(conn))
    workspace = tmp_path / "workflows"
    workspace.mkdir(parents=True, exist_ok=True)
    svc._v1_path = lambda wf_id: (
        (workspace / f"{wf_id}.json") if (workspace / f"{wf_id}.json").exists() else None)
    return svc, workspace


@pytest.mark.asyncio
async def test_migrate_workflow_creates_rev_first_run(tmp_path):
    svc, workspace = await _migrate_svc(tmp_path)
    (workspace / "a.json").write_text(json.dumps(_v1("a", "v1")), encoding="utf-8")
    r1 = await svc.migrate_workflow("a")
    assert r1["action"] == "migrated"
    assert (await svc.get_definition("a"))["current_revision_id"] == r1["revision_id"]
    # 幂等：同内容再跑 → unchanged，且不重复插入
    r2 = await svc.migrate_workflow("a")
    assert r2["action"] == "unchanged"
    assert r2["revision_id"] == r1["revision_id"]
    assert len(await svc.list_revisions("a")) == 1


@pytest.mark.asyncio
async def test_migrate_workflow_dry_run_writes_nothing(tmp_path):
    svc, workspace = await _migrate_svc(tmp_path)
    (workspace / "b.json").write_text(json.dumps(_v1("b", "x")), encoding="utf-8")
    rep = await svc.migrate_workflow("b", dry_run=True)
    assert rep["action"] == "migrated" and rep["dry_run"] is True
    assert await svc.get_definition("b") is None
    assert await svc.list_revisions("b") == []


@pytest.mark.asyncio
async def test_migrate_workflow_invalid_graph_reported(tmp_path):
    svc, workspace = await _migrate_svc(tmp_path)
    # 重复节点 id → 图校验失败
    v1 = _v1("c", "dup")
    v1["nodes"] = [{"id": "dup", "type": "tool", "note": "a"},
                   {"id": "dup", "type": "step", "note": "b"}]
    (workspace / "c.json").write_text(json.dumps(v1), encoding="utf-8")
    rep = await svc.migrate_workflow("c")
    assert rep["action"] == "invalid"
    assert rep.get("details", {}).get("duplicate") is True
    assert await svc.get_definition("c") is None


@pytest.mark.asyncio
async def test_migrate_workflow_respects_manual_rollback(tmp_path):
    svc, workspace = await _migrate_svc(tmp_path)
    (workspace / "d.json").write_text(json.dumps(_v1("d", "v1")), encoding="utf-8")
    r1 = await svc.migrate_workflow("d")
    (workspace / "d.json").write_text(json.dumps(_v1("d", "v2")), encoding="utf-8")
    r2 = await svc.migrate_workflow("d")
    assert r2["action"] == "migrated"
    # 人工回滚到 v1
    rolled = await svc.set_revision_current("d", r1["revision_id"],
                                            etag=(await svc.get_definition("d"))["etag"])
    assert rolled["current_revision_id"] == r1["revision_id"]
    # 内容未变再迁 → unchanged，且不覆盖回滚指针
    r3 = await svc.migrate_workflow("d")
    assert r3["action"] == "unchanged"
    assert (await svc.get_definition("d"))["current_revision_id"] == r1["revision_id"]


# ---------------------------------------------------------------------------
# CLI：脚本级走查（进程内直接调用 main 逻辑，不依赖子进程环境）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def cli_module():
    spec = importlib.util.spec_from_file_location(
        "wf_migrate_cli", str(Path(__file__).parents[2] / "scripts" / "migrate_v1_workflows.py"))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_cli_migrate_dry_run_then_real(tmp_path, monkeypatch, cli_module):
    cli = cli_module
    workspace = tmp_path / "ws"
    (workspace / "workflows").mkdir(parents=True)
    (workspace / "workflows" / "w1.json").write_text(json.dumps(_v1("w1", "x")), encoding="utf-8")
    monkeypatch.setattr("config.WORKSPACE_DIR", str(workspace), raising=False)
    db_path = str(tmp_path / "cli.db")

    # --dry-run 不写库
    rc = await cli._main(cli._parse_args(["--dry-run", "--db", db_path]))
    assert rc == 0
    conn = await aiosqlite.connect(db_path)
    rc = await conn.execute("SELECT COUNT(*) FROM wf_definition")
    assert (await rc.fetchone())[0] == 0
    await conn.commit()
    await conn.close()

    # 实际迁移 + 状态查询
    rc = await cli._main(cli._parse_args(["--db", db_path]))
    assert rc == 0
    rc = await cli._main(cli._parse_args(["--status", "--db", db_path]))
    assert rc == 0

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    svc = WorkflowV2Service(WorkflowRepository(conn))
    assert (await svc.get_definition("w1"))["current_revision_id"] != ""

    # 灰度默认关：迁移了但尚未开放执行
    await svc.set_config("workflow_v2.enabled", False)
    assert await svc.is_wf_enabled("w1") is False
    await conn.close()


@pytest.mark.asyncio
async def test_cli_pilot_and_rollback(tmp_path, monkeypatch, cli_module):
    cli = cli_module
    workspace = tmp_path / "ws2"
    (workspace / "workflows").mkdir(parents=True)
    (workspace / "workflows" / "w2.json").write_text(json.dumps(_v1("w2", "v1")), encoding="utf-8")
    monkeypatch.setattr("config.WORKSPACE_DIR", str(workspace), raising=False)
    db_path = str(tmp_path / "cli_b.db")

    assert await cli._main(cli._parse_args(["--db", db_path])) == 0
    # 加入试点白名单 → 工作流可用
    assert await cli._main(cli._parse_args(["--pilot", "w2", "--db", db_path])) == 0
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    svc = WorkflowV2Service(WorkflowRepository(conn))
    assert await svc.is_wf_enabled("w2") is True
    # 回滚到不存在的版本 → 失败码 1
    rc = await cli._main(cli._parse_args(["--rollback", "w2", "no_such", "--db", db_path]))
    assert rc == 1
    # 移出白名单 → 不再可用
    assert await cli._main(cli._parse_args(["--unpilot", "w2", "--db", db_path])) == 0
    assert await svc.is_wf_enabled("w2") is False
    await conn.close()
