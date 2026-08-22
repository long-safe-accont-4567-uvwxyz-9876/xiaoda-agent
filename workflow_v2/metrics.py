# workflow_v2/metrics.py
"""M4 workflow 级指标（内存计数）：按工作流聚合运行/步骤/事件/审批统计。

设计：进程内单实例（由 ``build_runtime`` 创建后注入 service/driver/scheduler），
零 IO 零锁 —— 事件表已落库（审计权威），此处的计数只服务「现在跑得怎么样」
的观测与 debug 计数（events / reviews 增量），不参与任何状态判定。

快照由 ``GET /api/v1/workflow-metrics`` 暴露（prometheus /metrics 保持不动，
那是进程级——workflow 粒度放专属端点，避免污染全局 exposition）。
"""
from __future__ import annotations

import time
from typing import Any


class WorkflowMetrics:
    def __init__(self) -> None:
        self._wf: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 计数
    def _row(self, wf_id: str) -> dict[str, Any]:
        row = self._wf.get(wf_id)
        if row is None:
            row = {
                "runs_started": 0,
                "runs_succeeded": 0,
                "runs_failed": 0,
                "runs_cancelled": 0,
                "steps_succeeded": 0,
                "steps_failed": 0,
                "events": 0,
                "reviews_created": 0,
                "reviews_decided": 0,
                "first_run_at": 0.0,
                "last_run_at": 0.0,
                "last_finished_at": 0.0,
            }
            self._wf[wf_id] = row
        return row

    def run_started(self, wf_id: str) -> None:
        row = self._row(wf_id)
        row["runs_started"] += 1
        row["last_run_at"] = time.time()
        if not row["first_run_at"]:
            row["first_run_at"] = row["last_run_at"]

    def run_finished(self, wf_id: str, status: str) -> None:
        row = self._row(wf_id)
        key = f"runs_{status}"
        if key in row:
            row[key] += 1
        row["last_finished_at"] = time.time()

    def step_finished(self, wf_id: str, ok: bool) -> None:
        row = self._row(wf_id)
        row["steps_succeeded" if ok else "steps_failed"] += 1

    def event_appended(self, wf_id: str) -> None:
        self._row(wf_id)["events"] += 1

    def review_created(self, wf_id: str) -> None:
        self._row(wf_id)["reviews_created"] += 1

    def review_decided(self, wf_id: str) -> None:
        self._row(wf_id)["reviews_decided"] += 1

    # ------------------------------------------------------------------ 快照
    def snapshot(self) -> dict[str, Any]:
        """工作流维度计数 + 全库合计；pending 审批数 = 创建 - 已决策。"""
        rows: dict[str, Any] = {}
        totals: dict[str, int] = {}
        for wf_id, r in sorted(self._wf.items()):
            item = dict(r)
            item["reviews_pending"] = item["reviews_created"] - item["reviews_decided"]
            rows[wf_id] = item
            for k, v in item.items():
                if isinstance(v, (int, float)):
                    totals[k] = totals.get(k, 0) + v
        return {"workflows": rows, "totals": totals}
