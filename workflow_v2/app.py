# workflow_v2/app.py
"""Workflow V2 转正运行时：节点执行器 + 后台驱动循环 + 装配助手。

2026-08-22 决策"转正"（此前后端挂路由未注入 app.state、无执行器、无驱动）。
本模块补齐三件事：

- ``WorkflowExecutor`` —— Scheduler 的回调执行器
  * legacy_prompt（v1 迁移的 custom 节点）经 core.router 走 LLM 通道执行；
  * start/end/transform 为结构性节点，直接成功（不消费 LLM）；
  * 其余节点类型（tool/mcp/agent/condition/...）转正第 1 版不实现，
    返回带 UNSUPPORTED_NODE 错误的失败结果，随 run 的 failure_policy
    落盘为 run_failed —— 可追踪、不静默；
- ``WorkflowDriver``: 后台 asyncio 循环，轮询非终态 run 驱动 scheduler.tick，
  处理 cancel 请求与重启后的保守恢复；
- ``build_runtime``: web/server.py::_start_services 的装配入口（幂等）。

带降级语义：core.router 缺失或 LLM 调用失败 → 节点失败落盘（不吞异常）；
降级模式下 web 侧不装配，路由对 app.state.workflow_v2 缺失返回 503。
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from loguru import logger

from workflow_v2.models import (
    NodeSpec, NodeType, RunStatus, StepStatus, WorkflowStepRun,
)
from workflow_v2.repository import WorkflowRepository
from workflow_v2.scheduler import NodeResult, Scheduler
from workflow_v2.service import WorkflowV2Service

_UNSUPPORTED_HINT = (
    "该节点类型在转正第 1 轮未实现，请在编排中改用 step/legacy_prompt 节点"
)


class WorkflowExecutor:
    """Scheduler 的回调执行器：把节点交给真实通道执行（最小闭环）。

    目前只执行 legacy_prompt（v1 迁移来的自定义节点，以节点 note/name 作为
    prompt 交给当前主 LLM），其余 DAG 高级节点类型（tool/mcp/agent/condition…）
    显式失败并记录原因 —— 宁可失败落库也不静默跳过。
    """

    def __init__(self, core: Any) -> None:
        self._core = core

    async def __call__(self, node: NodeSpec, step: WorkflowStepRun,
                       ctx: dict[str, Any]) -> NodeResult:
        try:
            t = node.type
            if t in (NodeType.START, NodeType.END, NodeType.TRANSFORM):
                # 结构性节点：开始/结束/纯标记 step，直接成功不消费 LLM
                return NodeResult(status=StepStatus.SUCCEEDED, output={"node": node.id})
            if t == NodeType.LEGACY_PROMPT:
                return await self._run_legacy_prompt(node)
            return NodeResult(
                status=StepStatus.FAILED,
                error_code="UNSUPPORTED_NODE",
                error_message=f"node type '{t.value}' {_UNSUPPORTED_HINT}",
            )
        except Exception as e:  # noqa: BLE001 —— 执行器绝不允许把异常抛给驱动
            logger.warning("workflow.executor_failed node={} error={}", node.id, str(e))
            return NodeResult(
                status=StepStatus.FAILED,
                error_code="EXECUTOR_ERROR",
                error_message=str(e)[:500],
            )

    async def _run_legacy_prompt(self, node: NodeSpec) -> NodeResult:
        router = getattr(self._core, "router", None)
        if router is None or not hasattr(router, "route"):
            return NodeResult(
                status=StepStatus.FAILED,
                error_code="ROUTER_UNAVAILABLE",
                error_message="LLM 路由不可用（降级模式或未装配）",
            )
        raw = (node.config or {}).get("raw") or {}
        note = raw.get("note") or ""
        name = node.name or node.id or "未命名节点"
        prompt = (
            f"执行工作流节点「{name}」：{note}"
            if note
            else f"请执行工作流节点「{name}」"
        )
        try:
            answer = await router.route(
                task_type="chat",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                timeout=60,
            )
        except Exception as e:  # noqa: BLE001 —— LLM 调用失败落死因，不静默
            logger.warning("workflow.legacy_prompt_llm_failed node={} error={}",
                           node.id, str(e))
            return NodeResult(
                status=StepStatus.FAILED,
                error_code="LLM_CALL_FAILED",
                error_message=str(e)[:500],
            )
        text = str(answer or "").strip()
        return NodeResult(
            status=StepStatus.SUCCEEDED if text else StepStatus.FAILED,
            output={"text": text, "prompt": prompt},
        )


class WorkflowDriver:
    """后台驱动：轮询非终态 run → scheduler.tick 推进；处理 cancel 与恢复。"""

    def __init__(self, repo: WorkflowRepository, scheduler: Scheduler,
                 poll_seconds: float = 1.0, conn: Any = None) -> None:
        self._repo = repo
        self._scheduler = scheduler
        self._poll = poll_seconds
        self._conn = conn  # 装配时传入的独立连接，stop 时一并关闭
        self._task: asyncio.Task | None = None
        self._closed = False

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:  # noqa: BLE001 —— 关闭失败不阻塞 shutdown 链
                logger.debug("workflow.driver_conn_close_error", exc_info=True)
            finally:
                self._conn = None

    async def recover_running(self) -> None:
        """重启恢复：对遗留 running 步骤按保守策略标 FAILED（幂等步骤除外）。"""
        for run in await self._repo.list_active_runs():
            if run.status == RunStatus.RUNNING:
                with suppress(Exception):  # noqa: BLE001
                    await self._scheduler.recover(run.run_id)

    async def _loop(self) -> None:
        while not self._closed:
            try:
                await self._poll_once()
            except Exception as e:  # noqa: BLE001 —— 驱动不因单轮失败退出
                logger.warning("workflow.driver_poll_error error={}", str(e)[:300])
            await asyncio.sleep(self._poll)

    async def _poll_once(self) -> None:
        for run in await self._repo.list_active_runs():
            if run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
                continue
            # 取消请求优先：置终态后不再调度
            if run.cancel_requested_at is not None:
                if await self._repo.cancel_run(run.run_id):
                    logger.info("workflow.run_cancelled run_id={}", run.run_id)
                continue
            try:
                await self._scheduler.tick(run.run_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("workflow.tick_failed run_id={} error={}", run.run_id, str(e)[:300])


async def build_runtime(core: Any, db_path: str) -> tuple[WorkflowV2Service, WorkflowDriver]:
    """装配 v2 运行时：独立 aiosqlite 连接 + repository + service + executor + driver。

    注意生产关系：与 VectorStore 一致，v2 使用自己的一条 aiosqlite 连接（主库
    独立连接串行访问，事务由 repository 内 BEGIN/COMMIT 管理），不共享 Core DB
    线程池连接以避免交叉事务。schema 由 legacy_migration v27 幂等创建。
    """
    import aiosqlite

    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    repo = WorkflowRepository(conn)
    svc = WorkflowV2Service(repo)
    executor = WorkflowExecutor(core)
    scheduler = Scheduler(repo, executor, repo.get_revision)
    driver = WorkflowDriver(repo, scheduler, conn=conn)
    await driver.recover_running()
    driver.start()
    return svc, driver