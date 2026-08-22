# workflow_v2/app.py
"""Workflow V2 转正运行时：节点执行器 + 后台驱动循环 + 装配助手。

2026-08-22 决策"转正"（本前后端挂路由未注入 app.state、无执行器、无驱动）。
本模块补齐三件事：

- ``UnifiedExecutor``(见 workflow_v2/executor.py)—— Scheduler 的回调执行器：
  * TOOL/MCP 走 core.tool_executor（复用注册表 + 沙箱 + 审批 + 审计）；
  * MODEL/SKILL/LEGACY_PROMPT 走 core.router（ModelRouter）；
  * START/END/TRANSFORM 为结构性节点直接成功；AGENT 显式 NO_IMPL；
    安全横切：secret 占位符运行时解析、input 校验清洗、超时/重试归一；
- ``WorkflowDriver``: 后台 asyncio 循环，轮询非终态 run 驱动 scheduler.tick，
  处理 cancel 请求与重启后的保守恢复；
- ``build_runtime``: web/server.py::_start_services 的装配入口（幂等）。

带降级语义：core.router / tool_executor 缺失或调用失败 → 节点失败落盘
（不吞异常）；降级模式下 web 侧不装配，路由对 app.state.workflow_v2 缺失
返回 503。
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from loguru import logger

from workflow_v2.executor import ExecutorServices, UnifiedExecutor
from workflow_v2.models import RunStatus
from workflow_v2.repository import WorkflowRepository
from workflow_v2.scheduler import Scheduler
from workflow_v2.service import WorkflowV2Service


def _skill_resolver_from_workspace():
    """默认技能解析器：WORKSPACE_DIR/skills/{name}.md（含 v1 自动生成的 wf_*.md）。"""
    from pathlib import Path

    from config import WORKSPACE_DIR
    base = Path(WORKSPACE_DIR) / "skills"

    def resolve(name: str) -> dict | None:
        fp = base / f"{name}.md"
        if not fp.is_file():
            return None
        return {"name": name, "instructions": fp.read_text(encoding="utf-8", errors="replace")}

    return resolve


def _secret_resolver_from_core(core: Any):
    """把 core 的 SecretsBroker 适配成 secret 占位符解析器；缺省返回 None(不解析)。"""
    sb = getattr(core, "secrets_broker", None)
    if sb is not None and hasattr(sb, "get"):
        return lambda name: sb.get(name)  # type: ignore[no-any-return]
    return None


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
    services = ExecutorServices(
        tool_executor=getattr(core, "tool_executor", None),
        router=getattr(core, "router", None),
        security=getattr(core, "security", None),
        skill_resolver=_skill_resolver_from_workspace(),
        secret_resolver=_secret_resolver_from_core(core),
        user_id="workflow",
    )
    executor = UnifiedExecutor(services)
    scheduler = Scheduler(repo, executor, repo.get_revision)
    driver = WorkflowDriver(repo, scheduler, conn=conn)
    await driver.recover_running()
    driver.start()
    return svc, driver