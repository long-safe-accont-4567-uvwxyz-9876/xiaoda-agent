"""Workflow V2 application service: business operations over WorkflowRepository.

The router talks only to this service via ``request.app.state.workflow_v2``.
All reads/writes go through ``WorkflowRepository`` (plus a couple of direct
``wf_definition``/``wf_revision`` queries — those tables ship in the Task 3
DDL, see db/db_workflow.py).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from loguru import logger

from workflow_v2.models import RunStatus, WorkflowRun, WorkflowRunEvent
from workflow_v2.repository import WorkflowRepository

# statuses that can no longer be cancelled
_TERMINAL_STATUSES = {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}


def _run_dict(run: WorkflowRun) -> dict:
    return {
        "run_id": run.run_id,
        "workflow_id": run.workflow_id,
        "revision_id": run.revision_id,
        "status": run.status.value,
        "lock_version": run.lock_version,
        "parent_run_id": run.parent_run_id,
        "idempotency_key": run.idempotency_key,
        "input": run.input,
        "output": run.output,
        "cancel_requested_at": run.cancel_requested_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


class WorkflowV2Service:
    def __init__(self, repo: WorkflowRepository):
        self.repo = repo
        # M4 观测：build_runtime 装配时注入 WorkflowMetrics（空/缺省场景可 None）
        self.metrics: Any = None

    # --- definitions -------------------------------------------------------

    async def _definition_row(self, wf_id: str):
        cur = await self.repo.conn.execute(
            "SELECT * FROM wf_definition WHERE workflow_id=?", (wf_id,)
        )
        return await cur.fetchone()

    async def get_definition(self, wf_id: str) -> dict | None:
        row = await self._definition_row(wf_id)
        if row is None:
            return None
        return {
            "workflow_id": row["workflow_id"],
            "name": row["name"],
            "description": row["description"],
            "enabled": bool(row["enabled"]),
            "current_revision_id": row["current_revision_id"],
            "etag": row["etag"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def patch_definition(self, wf_id: str, body: dict, etag: str) -> dict | None:
        """Update identity fields (name/description) guarded by an atomic etag CAS.

        The etag comparison happens inside the SAME UPDATE statement (``WHERE
        workflow_id=? AND etag=?``), so two concurrent PATCHes with the same
        If-Match can never both pass: exactly one sees ``rowcount == 1`` and the
        loser gets 0 rows. Returns the updated definition (fresh etag) on
        success, or ``None`` when the etag moved / the definition is gone —
        the route maps that to 409 ETAG_CONFLICT.
        """
        sets: list[str] = []
        params: list = []
        if "name" in body:
            sets.append("name=?")
            params.append(body["name"])
        if "description" in body:
            sets.append("description=?")
            params.append(body["description"])
        sets.append("etag=?")
        params.append(f"etag-{uuid.uuid4().hex[:12]}")
        sets.append("updated_at=?")
        params.append(time.time())
        params.append(wf_id)
        params.append(etag)
        cur = await self.repo.conn.execute(
            f"UPDATE wf_definition SET {', '.join(sets)} WHERE workflow_id=? AND etag=?",
            params,
        )
        if cur.rowcount != 1:
            return None  # etag moved (or definition deleted) — no blind overwrite
        await self.repo.conn.commit()
        return await self.get_definition(wf_id)

    # --- runs ----------------------------------------------------------------

    async def _find_run_by_idempotency(self, wf_id: str, idempotency_key: str) -> WorkflowRun | None:
        cur = await self.repo.conn.execute(
            "SELECT run_id FROM wf_run WHERE workflow_id=? AND idempotency_key=?",
            (wf_id, idempotency_key),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return await self.repo.get_run(row["run_id"])

    async def create_or_get_run(self, wf_id: str, input_: dict, idempotency_key: str) -> dict | None:
        """Idempotent: same (workflow_id, idempotency_key) always returns the same run.

        Returns None when the workflow definition does not exist.
        """
        definition = await self._definition_row(wf_id)
        if definition is None:
            return None
        existing = await self._find_run_by_idempotency(wf_id, idempotency_key)
        if existing is not None:
            return _run_dict(existing)

        now = time.time()
        run = WorkflowRun(
            run_id=f"wfr_{uuid.uuid4().hex[:12]}",
            workflow_id=wf_id,
            revision_id=definition["current_revision_id"] or "",
            status=RunStatus.QUEUED,
            idempotency_key=idempotency_key,
            input=input_,
            created_at=now,
            updated_at=now,
        )
        event = WorkflowRunEvent(
            run_id=run.run_id, seq=1, event_type="run_queued",
            run_status=RunStatus.QUEUED, timestamp=now,
        )
        try:
            await self.repo.create_run(run, [], event)
        except sqlite3.IntegrityError:
            # lost a concurrent-create race on ux_wf_run_idem: return the winner
            existing = await self._find_run_by_idempotency(wf_id, idempotency_key)
            if existing is not None:
                return _run_dict(existing)
            raise
        if self.metrics is not None:
            self.metrics.run_started(wf_id)
        return _run_dict(run)

    async def snapshot(self, run_id: str) -> dict | None:
        run = await self.repo.get_run(run_id)
        if run is None:
            return None
        steps = await self.repo.list_steps(run_id)
        last_seq = (await self.repo.next_seq(run_id)) - 1
        return {
            "run": _run_dict(run),
            "steps": [s.model_dump(mode="json") for s in steps],
            "last_seq": last_seq,
        }

    async def events_after(self, run_id: str, after_seq: int) -> list[dict]:
        events = await self.repo.events_after(run_id, after_seq)
        return [e.model_dump(mode="json") for e in events]

    async def request_cancel(self, run_id: str) -> dict:
        """Idempotent cancel request. Raises KeyError when the run does not exist."""
        run = await self.repo.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.status in _TERMINAL_STATUSES:
            return {"cancel_requested": False, "status": run.status.value}
        now = time.time()
        if run.cancel_requested_at is None:
            await self.repo.conn.execute(
                "UPDATE wf_run SET cancel_requested_at=?, updated_at=? WHERE run_id=?",
                (now, now, run_id),
            )
            await self.repo.conn.commit()
        return {"cancel_requested": True, "status": run.status.value}

    # ── 转正：v1 JSON 桥接 + WebUI 视图（2026-08-22 决策"转正"） ────────────

    def _v1_path(self, wf_id: str):
        from pathlib import Path

        from config import WORKSPACE_DIR
        p = Path(WORKSPACE_DIR) / "workflows" / f"{wf_id}.json"
        return p if p.exists() else None

    async def snapshot_revision_from_v1(self, wf_id: str) -> dict | None:
        """把当前 v1 JSON 固化为新的不可变 revision（不提升 current）。

        M2 显式版本创建的语义：只存档、不动当前运行版本；
        publish（_publish_v1_revision）= 本方法 + 设置 current。
        v1 文件不存在返回 None，定义行缺失时自动创建。
        """
        fp = self._v1_path(wf_id)
        if fp is None:
            return None
        try:
            v1 = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("workflow.v1_read_failed wf_id={}", wf_id)
            return None
        from workflow_v2.migrate import migrate_v1
        rev, warnings = migrate_v1(v1)
        if warnings:
            logger.info("workflow.v1_migrate_warnings wf_id={} count={}",
                        wf_id, len(warnings))
        # 编译期拒绝（同 migrate 路径）：未实现节点类型/坏图不入库，
        # 否则要跑到该节点才报 UNSUPPORTED_NODE（2026-08-23 复审项）
        from workflow_v2.graph import GraphError, validate_graph
        try:
            validate_graph(rev.nodes, rev.edges)
        except GraphError as e:
            logger.warning("workflow.snapshot_invalid_graph wf_id={} error={}",
                           wf_id, e)
            return None
        definition = await self._definition_row(wf_id)
        if definition is None:
            await self.repo.upsert_definition(
                workflow_id=wf_id,
                name=str(v1.get("name") or wf_id),
                description=str(v1.get("description") or ""),
                enabled=bool(v1.get("enabled", True)),
            )
        await self.repo.insert_revision(rev)
        return {"revision": rev.model_dump(mode="json"), "warnings": warnings}

    async def _publish_v1_revision(self, wf_id: str) -> dict | None:
        """发布：快照（见上）+ 提升当前版本。返回 revision dict 或 None。"""
        snap = await self.snapshot_revision_from_v1(wf_id)
        if snap is None:
            return None
        await self.repo.set_current_revision(wf_id, snap["revision"]["revision_id"])
        return snap

    async def ensure_published(self, wf_id: str) -> dict | None:
        """确保 wf_definition 存在且 current_revision_id 非空（首次运行自动接入）。

        三种情形：定义不存在 → 从 v1 完整迁移并发布；定义在但无当前版本 →
        从 v1 发布一个版本；两者都就绪 → 直接返回现状。v1 文件也不存在 → None。
        """
        definition = await self.get_definition(wf_id)
        if definition is not None and definition.get("current_revision_id"):
            return definition
        published = await self._publish_v1_revision(wf_id)
        if published is None:
            return None
        return await self.get_definition(wf_id)

    async def publish_from_v1(self, wf_id: str) -> dict | None:
        """发布新版本：把当前 v1 JSON 固化为新的不可变 revision（WebUI 发布按钮）。"""
        return await self._publish_v1_revision(wf_id)

    async def _revision_exists(self, wf_id: str, revision_id: str) -> bool:
        cur = await self.repo.conn.execute(
            "SELECT 1 FROM wf_revision WHERE revision_id=? AND workflow_id=?",
            (revision_id, wf_id),
        )
        return (await cur.fetchone()) is not None

    async def revision_exists(self, wf_id: str, revision_id: str) -> bool:
        """revision 是否属于该 workflow（回滚路由做 404/409 区分）。"""
        return await self._revision_exists(wf_id, revision_id)

    async def set_revision_current(self, wf_id: str, revision_id: str,
                                    etag: str) -> dict | None:
        """回滚：把 current_revision_id 切到指定 revision（etag CAS 同定义 PATCH）。

        版本不存在或 etag 移动（并发修改）→ None，不盲覆盖。
        """
        if not await self._revision_exists(wf_id, revision_id):
            return None
        cur = await self.repo.conn.execute(
            "UPDATE wf_definition SET current_revision_id=?, etag=?, updated_at=? "
            "WHERE workflow_id=? AND etag=?",
            (revision_id, f"etag-{uuid.uuid4().hex[:12]}", time.time(), wf_id, etag),
        )
        if cur.rowcount != 1:
            return None  # 定义被他人改过（etag 移动）——回滚不盲覆盖
        await self.repo.conn.commit()
        return await self.get_definition(wf_id)

    async def list_runs(self, workflow_id: str) -> list[dict]:
        runs = await self.repo.list_runs_by_wf(workflow_id)
        return [_run_dict(r) for r in runs]

    async def list_revisions(self, workflow_id: str) -> list[dict]:
        """版本列表富化：逐条标注是否 current + 定义 etag（回滚 PUT 需要）。"""
        definition = await self._definition_row(workflow_id)
        current = definition["current_revision_id"] if definition else None
        etag = definition["etag"] if definition else ""
        rows = await self.repo.list_revisions(workflow_id)
        for r in rows:
            r["current"] = r["revision_id"] == current
            r["etag"] = etag
        return rows

    # ── M3：灰度开关（DB config 键，决策"试点白名单"；不新增环境变量） ─────────
    #
    # 两个键（立项书 §6）：
    #   workflow_v2.enabled      —— 全局开关，默认 false
    #   workflow_v2.pilot_wf_ids —— 试点白名单，JSON 字符串数组
    # 生效规则：全局开 或 wf_id 在白名单内 → 该工作流可用。
    # 表由 legacy_migration v28 创建（idempotent CREATE TABLE IF NOT EXISTS）。

    async def get_config(self, key: str, default: Any = None) -> Any:
        """读取 DB config 值（JSON 解码）；键缺失或表未建 → default。"""
        try:
            cur = await self.repo.conn.execute(
                "SELECT value FROM wf_config WHERE key=?", (key,)
            )
            row = await cur.fetchone()
        except sqlite3.OperationalError:
            return default
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    async def set_config(self, key: str, value: Any) -> None:
        """写 DB config 键（upsert，JSON 编码持久化）。"""
        await self.repo.conn.execute(
            "INSERT INTO wf_config(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await self.repo.conn.commit()

    async def v2_global_enabled(self) -> bool:
        return bool(await self.get_config("workflow_v2.enabled", False))

    async def v2_pilot_ids(self) -> list[str]:
        """白名单列表（容忍字符串直存的旧值形态）。"""
        v = await self.get_config("workflow_v2.pilot_wf_ids", [])
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                return []
        return v if isinstance(v, list) else []

    async def is_wf_enabled(self, wf_id: str) -> bool:
        """灰度生效规则（立项书 §6）：全局开 **或** 白名单内 → 该流可用。"""
        return await self.v2_global_enabled() or wf_id in await self.v2_pilot_ids()

    # ── M3：v1 → v2 批量迁移（CLI scripts/migrate_v1_workflows.py 核心） ──────

    async def _find_revision_by_hash(self, wf_id: str, content_hash: str) -> dict | None:
        cur = await self.repo.conn.execute(
            "SELECT revision_id, content_hash FROM wf_revision "
            "WHERE workflow_id=? AND content_hash=? ORDER BY created_at DESC LIMIT 1",
            (wf_id, content_hash),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def migrate_workflow(self, wf_id: str, *, set_current: bool = True,
                               dry_run: bool = False) -> dict | None:
        """幂等迁移一个 v1 工作流为 v2 revision（CLI 的默认/--dry-run 共用）。

        报告字段：action = "invalid" | "unchanged" | "migrated"；
        v1 文件缺失 → 整体返回 None。语义（立项书 §6 CLI 要求）：
        - 同 content_hash 的 revision 已存在 → unchanged，**不**覆盖 current
          （尊重 WebUI 人工回滚——"回滚"就是把 current 指向旧版本）；
        - 否则固化新 revision（dry_run 只预演不写库）→ migrated；
        - set_current=True 时达成"置 current"，但只有 current 为空才写指针
          （已有当前版本时该工作即 publish 职责，不属于迁移）。
        """
        fp = self._v1_path(wf_id)
        if fp is None:
            return None
        try:
            v1 = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("workflow.v1_read_failed wf_id={}", wf_id)
            return None
        from workflow_v2.migrate import migrate_v1
        rev, warnings = migrate_v1(v1)
        from workflow_v2.graph import GraphError, validate_graph
        try:
            validate_graph(rev.nodes, rev.edges)
        except GraphError as e:
            return {"wf_id": wf_id, "action": "invalid", "error": str(e),
                    "details": e.details, "warnings": warnings}

        existing = await self._find_revision_by_hash(wf_id, rev.content_hash)
        if existing is not None:
            # 同内容已入库：不重复插入、不覆盖人工回滚；current 为空才补指
            definition = await self._definition_row(wf_id)
            if (not dry_run and set_current and definition is not None
                    and not definition["current_revision_id"]):
                await self.repo.set_current_revision(wf_id, existing["revision_id"])
            return {"wf_id": wf_id, "action": "unchanged",
                    "revision_id": existing["revision_id"],
                    "content_hash": rev.content_hash, "warnings": warnings}

        definition = await self._definition_row(wf_id)
        if dry_run:
            return {"wf_id": wf_id, "action": "migrated", "dry_run": True,
                    "definition_exists": definition is not None,
                    "content_hash": rev.content_hash, "warnings": warnings}
        if definition is None:
            await self.repo.upsert_definition(
                workflow_id=wf_id,
                name=str(v1.get("name") or wf_id),
                description=str(v1.get("description") or ""),
                enabled=bool(v1.get("enabled", True)),
            )
        await self.repo.insert_revision(rev)
        if set_current and definition is None:  # 新定义：首版即当前
            await self.repo.set_current_revision(wf_id, rev.revision_id)
        return {"wf_id": wf_id, "action": "migrated",
                "revision_id": rev.revision_id, "content_hash": rev.content_hash,
                "warnings": warnings}

    # --- M4 REVIEW 审批 / 负载节流 / 观测 ------------------------------------

    async def max_concurrent_runs(self) -> int:
        """并发运行上限（负载节流）：0/负值 = 不限制。

        DB config 键 ``workflow_v2.max_concurrent_runs``（不新增 env var），
        默认 4——同时处于 RUNNING 的 run 超过上限时 driver 不再启动新的
        QUEUED run（已运行中的不受影响，等空出名额自动续跑）。
        """
        v = await self.get_config("workflow_v2.max_concurrent_runs", 4)
        try:
            n = int(v)
        except (TypeError, ValueError):
            return 4
        return n if n > 0 else 0

    async def list_reviews(self, run_id: str) -> list[dict]:
        """某次运行的审批单（含已决）；WebUI 审批卡片 / 审计用。"""
        return await self.repo.list_reviews(run_id)

    async def decide_review(self, run_id: str, review_id: str, decision: str,
                            decided_by: str, note: str = "") -> tuple[str, dict]:
        """审批决策：返回 (outcome, data)。outcome ∈ ok / not_found / conflict。

        approve → REVIEW 步骤 SUCCEEDED + run 继续（DAG 自动推进）；
        reject → 步骤 FAILED + run FAILED（REVIEW_REJECTED，审批否决即停）。
        单事务 CAS：重复决策 / run 已终态 → conflict，不重复计分。
        """
        row = await self.repo.get_review(review_id)
        if row is None or row["run_id"] != run_id:
            return "not_found", {}
        committed = await self.repo.resolve_review(review_id, decision, decided_by, note)
        if committed is None:
            return "conflict", {}
        run = await self.repo.get_run(run_id)
        if run is not None and self.metrics is not None:
            self.metrics.review_decided(run.workflow_id)
        return "ok", {"run_id": run_id, "status": run.status.value if run else "unknown",
                      "review_id": review_id, "decision": decision}

    def metrics_snapshot(self) -> dict:
        """workflow 级指标 + debug 计数（无 metrics 注入时返回空结构）。"""
        if self.metrics is None:
            return {"workflows": {}, "totals": {}}
        return self.metrics.snapshot()
