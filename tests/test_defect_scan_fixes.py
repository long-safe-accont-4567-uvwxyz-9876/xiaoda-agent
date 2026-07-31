"""缺陷扫描修复验证 — 覆盖本轮发现的关键 Bug。

Bug 1 (P0 Security): /workspace 路由缺少认证，未授权用户可浏览文件系统、修改白名单
Bug 2 (P1 Concurrency): _should_run check-then-act 竞态条件，并发消息触发周期任务重复执行
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock


# ── Bug 1: workspace 路由认证 ──────────────────────────────────


class TestWorkspaceAuth:
    """验证 /workspace 路由要求认证。"""

    def test_workspace_router_has_auth_dependency(self):
        """workspace router 必须携带 get_current_user 依赖。"""
        from web.routers.workspace import router
        dep_names = []
        for dep in router.dependencies:
            if hasattr(dep, "dependency"):
                dep_names.append(getattr(dep.dependency, "__name__", str(dep.dependency)))
        assert any("get_current_user" in name for name in dep_names), \
            f"/workspace 路由缺少认证依赖，当前依赖: {dep_names}"


# ── Bug 2: _should_run 竞态条件 ────────────────────────────────


class TestShouldRunRaceCondition:
    """验证 _should_run 的并发安全性。"""

    def test_concurrent_should_run_no_duplicate(self):
        """两条并发消息同时调用 _should_run，同名任务只能被一个调用返回 True。"""
        from core.background_tasks import BackgroundTaskManager

        mock_db = AsyncMock()
        mock_db.get_cron_last_run = AsyncMock(return_value=None)

        mock_context = MagicMock()
        mock_context.history = []

        manager = BackgroundTaskManager(
            db=mock_db,
            context=mock_context,
        )

        async def _run():
            results = await asyncio.gather(
                manager._should_run("dream_archive", interval_hours=24),
                manager._should_run("dream_archive", interval_hours=24),
            )
            true_count = sum(1 for r in results if r is True)
            return true_count

        true_count = asyncio.get_event_loop().run_until_complete(_run())
        assert true_count <= 1, \
            f"并发 _should_run 返回 {true_count} 个 True，应最多 1 个（防止任务重复执行）"

        manager._running_scheduled.discard("dream_archive")

    def test_should_run_releases_placeholder_on_db_error(self):
        """_should_run DB 报错时应释放占位，允许后续重试。"""
        from core.background_tasks import BackgroundTaskManager

        mock_db = AsyncMock()
        mock_db.get_cron_last_run = AsyncMock(side_effect=OSError("db locked"))

        mock_context = MagicMock()
        mock_context.history = []

        manager = BackgroundTaskManager(
            db=mock_db,
            context=mock_context,
        )

        async def _run():
            return await manager._should_run("dream_archive", interval_hours=24)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False
        assert "dream_archive" not in manager._running_scheduled, \
            "DB 错误后 _running_scheduled 占位未释放，会导致任务永久拒绝"

    def test_should_run_releases_placeholder_when_not_due(self):
        """_should_run 判定未到运行时间时应释放占位。"""
        from core.background_tasks import BackgroundTaskManager

        mock_db = AsyncMock()
        mock_db.get_cron_last_run = AsyncMock(return_value=time.time())

        mock_context = MagicMock()
        mock_context.history = []

        manager = BackgroundTaskManager(
            db=mock_db,
            context=mock_context,
        )

        async def _run():
            return await manager._should_run("dream_archive", interval_hours=24)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False
        assert "dream_archive" not in manager._running_scheduled, \
            "未到运行时间时 _running_scheduled 占位未释放"
