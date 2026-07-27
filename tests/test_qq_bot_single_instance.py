"""QQ Bot 单实例合并 — 凭证指纹去重测试

背景（根因）：restart_qq_bot_task 中 asyncio.Lock 仅保证重启流程互斥，每个
等待者获得锁后仍会执行"取消当前 task → 新建 task"。没有凭证版本比较、
in-flight 合并或防抖——20 次相同凭证并发请求会导致 19 次 Bot 启动、
18 次刚启动即被取消。

修复：ensure_qq_bot_task 用凭证指纹（APP_ID + APP_SECRET + ENABLE_QQ_BOT 的
sha256 前 16 位）合并并发请求——相同凭证 + 存活 task 时直接复用，不取消不重建。
"""
import asyncio
import contextlib
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


def _make_app():
    """构造 app，state 用 SimpleNamespace 保证 getattr 默认值正常工作。

    根因：用 MagicMock 做 state 会导致 getattr(app.state, "_qq_applied_fingerprint",
    None) 返回一个 MagicMock 而非 None，破坏指纹合并判定（applied_fp == target_fp
    永远为 False）。
    """
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace(
        qq_task=None,
        core=types.SimpleNamespace(),
        _qq_applied_fingerprint=None,
        _qq_restart_lock=None,
    )
    return app


@pytest.fixture(autouse=True)
def reset_qq_adapter_module_vars():
    """每个测试前后保存/恢复 qq_bot_adapter 模块级 APP_ID/APP_SECRET"""
    import qq_bot_adapter
    orig_app_id = qq_bot_adapter.APP_ID
    orig_app_secret = qq_bot_adapter.APP_SECRET
    yield
    qq_bot_adapter.APP_ID = orig_app_id
    qq_bot_adapter.APP_SECRET = orig_app_secret


class TestEnsureQqBotTaskSingleInstance:
    """验证 ensure_qq_bot_task 的凭证指纹合并逻辑。"""

    async def test_concurrent_same_credential_starts_once(self, monkeypatch):
        """20 次并发相同凭证请求：run_qq_bot 只被调用 1 次，task 未被 cancel。

        这是本修复的核心场景：原实现下 20 次并发会产生 19 次冗余启动。
        """
        app = _make_app()
        monkeypatch.setenv("QQBOT_APP_ID", "app_id_123")
        monkeypatch.setenv("QQBOT_APP_SECRET", "secret_456")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        async def mock_run_qq_bot(*args, **kwargs):
            await asyncio.sleep(100)  # 保持 task 存活，模拟长期运行的 Bot

        with patch("qq_bot_adapter.run_qq_bot", side_effect=mock_run_qq_bot) as mock_run, \
             patch("config.AGENT_CONFIG", {"qq_bot": {"is_sandbox": False}}):
            from web.server import ensure_qq_bot_task
            results = await asyncio.gather(*[ensure_qq_bot_task(app) for _ in range(20)])

            assert all(r is True for r in results), "并发请求应全部返回 True（启动或复用）"
            # mock_run.call_count 在 run_qq_bot(...) 被调用（创建协程）时递增，
            # 不依赖协程是否实际执行——只有第一个请求会走到 _restart_qq_bot_task_inner
            assert mock_run.call_count == 1, \
                f"run_qq_bot 应只调用 1 次，实际 {mock_run.call_count} 次"
            assert app.state.qq_task is not None, "应存在存活 task"
            assert not app.state.qq_task.done(), "task 应存活未完成"
            assert not app.state.qq_task.cancelled(), "task 不应被 cancel"

            # 清理
            app.state.qq_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await app.state.qq_task

    async def test_credential_change_restarts_task(self, monkeypatch):
        """凭证变更后调 restart_qq_bot_task：旧 task 被 cancel，新 task 存活。

        restart_qq_bot_task 是 ensure_qq_bot_task(force=True) 的薄封装，
        force=True 绕过指纹合并，用于凭证保存路径。
        """
        app = _make_app()
        monkeypatch.setenv("QQBOT_APP_ID", "old_app_id")
        monkeypatch.setenv("QQBOT_APP_SECRET", "old_secret")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        async def mock_run_qq_bot(*args, **kwargs):
            await asyncio.sleep(100)

        with patch("qq_bot_adapter.run_qq_bot", side_effect=mock_run_qq_bot), \
             patch("config.AGENT_CONFIG", {"qq_bot": {"is_sandbox": False}}):
            from web.server import ensure_qq_bot_task, restart_qq_bot_task

            # 第一次：启动（走非 force 路径，建立指纹基线）
            await ensure_qq_bot_task(app)
            old_task = app.state.qq_task
            assert old_task is not None
            await asyncio.sleep(0)  # 让旧 task 进入 sleep 状态，确保 cancel 能干净传播

            # 变更凭证（指纹改变）
            monkeypatch.setenv("QQBOT_APP_ID", "new_app_id")
            monkeypatch.setenv("QQBOT_APP_SECRET", "new_secret")

            # 强制重启（凭证保存路径）
            await restart_qq_bot_task(app)
            new_task = app.state.qq_task

            assert new_task is not None, "应创建新 task"
            assert new_task is not old_task, "新 task 应不同于旧 task"
            assert old_task.cancelled(), "旧 task 应被 cancel"
            assert not new_task.done(), "新 task 应存活"

            # 清理
            new_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await new_task

    async def test_force_true_restarts_even_same_fingerprint(self, monkeypatch):
        """force=True 时即使指纹相同也重启。

        场景：凭证保存路径即使凭证内容未变（用户重新提交相同凭证）也要重启，
        确保 qq_bot_adapter 模块级变量被刷新。
        """
        app = _make_app()
        monkeypatch.setenv("QQBOT_APP_ID", "same_app_id")
        monkeypatch.setenv("QQBOT_APP_SECRET", "same_secret")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        async def mock_run_qq_bot(*args, **kwargs):
            await asyncio.sleep(100)

        with patch("qq_bot_adapter.run_qq_bot", side_effect=mock_run_qq_bot) as mock_run, \
             patch("config.AGENT_CONFIG", {"qq_bot": {"is_sandbox": False}}):
            from web.server import ensure_qq_bot_task

            # 第一次启动
            await ensure_qq_bot_task(app)
            first_task = app.state.qq_task
            assert first_task is not None
            await asyncio.sleep(0)  # 让旧 task 进入 sleep 状态

            # force=True，相同凭证也重启
            await ensure_qq_bot_task(app, force=True)
            second_task = app.state.qq_task

            assert second_task is not None, "应创建新 task"
            assert second_task is not first_task, "force=True 应创建新 task（不复用）"
            assert first_task.cancelled(), "旧 task 应被 cancel"
            assert mock_run.call_count == 2, \
                f"run_qq_bot 应调用 2 次，实际 {mock_run.call_count} 次"

            # 清理
            second_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await second_task
