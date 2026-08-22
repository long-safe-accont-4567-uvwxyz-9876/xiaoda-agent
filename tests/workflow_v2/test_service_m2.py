# tests/workflow_v2/test_service_m2.py
"""M2 服务层测试：版本快照（不升当前）、回滚 CAS、列表富化。"""
import json

import aiosqlite
import pytest

from db.db_workflow import create_schema
from workflow_v2.repository import WorkflowRepository
from workflow_v2.service import WorkflowV2Service


def _v1(name: str, label: str) -> dict:
    return {
        "id": name, "name": name, "description": "",
        "version": "1.0.0", "enabled": True,
        "nodes": [
            {"id": "n1", "type": "tool", "label": label,
             "ref": "web_search", "note": "", "params": {"q": 1}},
        ],
        "edges": [],
    }


@pytest.fixture
async def svc_env(tmp_path):
    """内存库 + 假 v1 路径解析器。

    连接必须随测试结束关闭：aiosqlite 的 worker 线程若在事件循环关闭后
    仍存活，会在 loop 上投递结果时崩溃（PytestUnhandledThreadExceptionWarning）。
    """
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    svc = WorkflowV2Service(WorkflowRepository(conn))
    # 工作区文件不落盘：注入假 v1 路径解析器
    workspace = tmp_path / "workflows"
    workspace.mkdir(parents=True, exist_ok=True)
    svc._v1_path = lambda wf_id: (workspace / f"{wf_id}.json").exists() and (workspace / f"{wf_id}.json")
    yield svc, workspace
    await conn.close()


@pytest.mark.asyncio
async def test_snapshot_creates_revision_without_promoting(svc_env):
    svc, workspace = svc_env
    (workspace / "a.json").write_text(json.dumps(_v1("a", "查询")), encoding="utf-8")
    snap = await svc.snapshot_revision_from_v1("a")
    assert snap is not None
    rev = snap["revision"]
    assert [n["type"] for n in rev["nodes"]] == ["start", "tool", "end"]
    # 存档不自动提升当前
    definition = await svc.get_definition("a")
    assert definition["current_revision_id"] is None


@pytest.mark.asyncio
async def test_publish_promotes_current(svc_env):
    svc, workspace = svc_env
    (workspace / "a.json").write_text(json.dumps(_v1("a", "查询")), encoding="utf-8")
    published = await svc.publish_from_v1("a")
    definition = await svc.get_definition("a")
    assert definition["current_revision_id"] == published["revision"]["revision_id"]


@pytest.mark.asyncio
async def test_rollback_switches_current_with_fresh_etag(svc_env):
    svc, workspace = svc_env
    (workspace / "a.json").write_text(json.dumps(_v1("a", "v1")), encoding="utf-8")
    first = await svc.publish_from_v1("a")          # rev1 置为当前
    # 修改 v1 再发布 → rev2
    (workspace / "a.json").write_text(json.dumps(_v1("a", "v2")), encoding="utf-8")
    second = await svc.publish_from_v1("a")
    rev1, rev2 = first["revision"]["revision_id"], second["revision"]["revision_id"]
    assert rev2 != rev1

    defn = await svc.get_definition("a")
    assert defn["current_revision_id"] == rev2
    old_etag = defn["etag"]

    # 用旧 etag 回滚 → CAS 失败,不盲覆盖
    assert await svc.set_revision_current("a", rev1, etag="etag-stale") is None
    defn = await svc.get_definition("a")
    assert defn["current_revision_id"] == rev2
    assert defn["etag"] == old_etag

    # 正确 etag 回滚 → current 切到 rev1,新 etag
    rolled = await svc.set_revision_current("a", rev1, etag=old_etag)
    assert rolled is not None
    assert rolled["current_revision_id"] == rev1
    assert rolled["etag"] != old_etag


@pytest.mark.asyncio
async def test_rollback_rejects_foreign_revision(svc_env):
    svc, workspace = svc_env
    (workspace / "a.json").write_text(json.dumps(_v1("a", "x")), encoding="utf-8")
    await svc.publish_from_v1("a")
    (workspace / "b.json").write_text(json.dumps(_v1("b", "y")), encoding="utf-8")
    await svc.publish_from_v1("b")
    defn_b = await svc.get_definition("b")
    # 不存在的版本 → None（路由映射 404 REVISION_NOT_FOUND）
    assert await svc.set_revision_current("b", "rev_no_such", etag=defn_b["etag"]) is None
    # a 的版本不属于 b → 同样拒绝
    rev_a = (await svc.list_revisions("a"))[0]["revision_id"]
    assert await svc.set_revision_current("b", rev_a, etag=defn_b["etag"]) is None


@pytest.mark.asyncio
async def test_list_revisions_marks_current_and_etag(svc_env):
    svc, workspace = svc_env
    (workspace / "a.json").write_text(json.dumps(_v1("a", "v1")), encoding="utf-8")
    first = await svc.publish_from_v1("a")
    (workspace / "a.json").write_text(json.dumps(_v1("a", "v2")), encoding="utf-8")
    second = await svc.publish_from_v1("a")

    rows = await svc.list_revisions("a")
    assert len(rows) == 2
    by_id = {r["revision_id"]: r for r in rows}
    assert by_id[second["revision"]["revision_id"]]["current"] is True
    assert by_id[first["revision"]["revision_id"]]["current"] is False
    etags = {r["etag"] for r in rows}
    assert len(etags) == 1 and etags.pop()


@pytest.mark.asyncio
async def test_rollback_flow_end_to_end(svc_env):
    svc, workspace = svc_env
    (workspace / "a.json").write_text(json.dumps(_v1("a", "v1")), encoding="utf-8")
    first = await svc.publish_from_v1("a")
    (workspace / "a.json").write_text(json.dumps(_v1("a", "v2")), encoding="utf-8")
    second = await svc.publish_from_v1("a")
    etag = (await svc.get_definition("a"))["etag"]
    # 完整回滚闭环：切回 rev1 → 再切回 rev2（新 etag 来自回滚返回值）
    rolled = await svc.set_revision_current("a", first["revision"]["revision_id"], etag=etag)
    assert rolled["current_revision_id"] == first["revision"]["revision_id"]
    etag2 = rolled["etag"]
    re = await svc.set_revision_current("a", second["revision"]["revision_id"], etag=etag2)
    assert re["current_revision_id"] == second["revision"]["revision_id"]
