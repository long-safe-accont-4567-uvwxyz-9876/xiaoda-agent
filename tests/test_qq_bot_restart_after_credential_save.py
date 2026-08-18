"""QQ Bot 凭证保存后自动重启任务 — 回归测试

背景（用户反馈"QQ ID/Secret 登录成功后仍显示机器人离线"）：
原 _start_services 仅在 WebUI 启动时检查 QQBOT_APP_ID env，用户后填入
凭证后不会自动启动 QQ bot 任务。即使 _reload_env_and_cache 更新了
os.environ，qq_bot_adapter 模块级 APP_ID/APP_SECRET（import 时一次性
读取）仍是旧值（空），导致 run_qq_bot 早期返回 disabled_no_appid。

修复：web/server.py 新增 restart_qq_bot_task()，web/routers/setup.py
在 QQ 凭证保存后异步调用该函数重启 QQ bot 任务。

测试覆盖：
1. restart_qq_bot_task 更新 qq_bot_adapter 模块级 APP_ID/APP_SECRET
2. 取消已存在的旧 qq_task
3. 凭证完整时启动新 qq_task
4. 凭证缺失时不启动（返回 False）
5. ENABLE_QQ_BOT=false 时不启动
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


@pytest.fixture
def mock_app():
    """构造 mock FastAPI app，带 state.qq_task 和 state.core"""
    app = MagicMock()
    app.state = MagicMock()
    app.state.qq_task = None  # 初始无 task
    app.state.core = MagicMock()
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


class TestRestartQqBotTask:
    """验证 restart_qq_bot_task 的重启逻辑。"""

    @pytest.mark.asyncio
    async def test_updates_module_level_app_id_and_secret(self, mock_app, monkeypatch):
        """restart_qq_bot_task 应更新 qq_bot_adapter.APP_ID/APP_SECRET 模块级变量

        根因：原模块级变量在 import 时一次性读取，不感知 os.environ 更新。
        """
        import qq_bot_adapter
        # 初始为空（模拟 WebUI 启动时未配置）
        qq_bot_adapter.APP_ID = ""
        qq_bot_adapter.APP_SECRET = ""
        # 用户填入凭证后
        monkeypatch.setenv("QQBOT_APP_ID", "test_app_id_12345")
        monkeypatch.setenv("QQBOT_APP_SECRET", "test_app_secret_67890")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        with patch("qq_bot_adapter.run_qq_bot", new_callable=AsyncMock) as mock_run, \
             patch("config.AGENT_CONFIG", {"qq_bot": {"is_sandbox": False}}):
            from web.server import restart_qq_bot_task
            await restart_qq_bot_task(mock_app)

        # 验证模块级变量已更新
        assert qq_bot_adapter.APP_ID == "test_app_id_12345"
        assert qq_bot_adapter.APP_SECRET == "test_app_secret_67890"

    @pytest.mark.asyncio
    async def test_cancels_existing_old_task(self, mock_app, monkeypatch):
        """存在旧 qq_task 时应先取消再启动新 task"""
        # 构造可 await 的假 task（模拟用旧凭证运行的 asyncio.Task）
        class FakeOldTask:
            def __init__(self):
                self.cancel_called = False
                self._cancelled = False
            def done(self):
                return False
            def cancel(self):
                self.cancel_called = True
                self._cancelled = True
            def __await__(self):
                # 模拟被取消的 task：直接抛 CancelledError
                if False:
                    yield  # 使其成为 generator
                raise asyncio.CancelledError()

        old_task = FakeOldTask()
        mock_app.state.qq_task = old_task

        monkeypatch.setenv("QQBOT_APP_ID", "new_app_id")
        monkeypatch.setenv("QQBOT_APP_SECRET", "new_secret")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        with patch("qq_bot_adapter.run_qq_bot", new_callable=AsyncMock), \
             patch("config.AGENT_CONFIG", {"qq_bot": {"is_sandbox": False}}):
            from web.server import restart_qq_bot_task
            await restart_qq_bot_task(mock_app)

        # 验证旧 task 已被取消
        assert old_task.cancel_called, "旧 qq_task.cancel() 应被调用"

    @pytest.mark.asyncio
    async def test_starts_new_task_when_credentials_complete(self, mock_app, monkeypatch):
        """凭证完整 + ENABLE_QQ_BOT=true → 启动新 task，返回 True"""
        monkeypatch.setenv("QQBOT_APP_ID", "valid_app_id")
        monkeypatch.setenv("QQBOT_APP_SECRET", "valid_secret")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        async def mock_run_qq_bot(*args, **kwargs):
            await asyncio.sleep(100)  # 模拟长期运行的 bot

        with patch("qq_bot_adapter.run_qq_bot", side_effect=mock_run_qq_bot), \
             patch("config.AGENT_CONFIG", {"qq_bot": {"is_sandbox": False}}):
            from web.server import restart_qq_bot_task
            result = await restart_qq_bot_task(mock_app)

        assert result is True
        # 验证新 task 已创建并保存到 app.state
        assert mock_app.state.qq_task is not None
        assert isinstance(mock_app.state.qq_task, asyncio.Task)
        # 清理
        mock_app.state.qq_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await mock_app.state.qq_task

    @pytest.mark.asyncio
    async def test_returns_false_when_app_id_missing(self, mock_app, monkeypatch):
        """QQBOT_APP_ID 缺失 → 不启动，返回 False"""
        monkeypatch.setenv("QQBOT_APP_ID", "")
        monkeypatch.setenv("QQBOT_APP_SECRET", "some_secret")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        from web.server import restart_qq_bot_task
        result = await restart_qq_bot_task(mock_app)

        assert result is False
        assert mock_app.state.qq_task is None

    @pytest.mark.asyncio
    async def test_returns_false_when_app_secret_missing(self, mock_app, monkeypatch):
        """QQBOT_APP_SECRET 缺失 → 不启动，返回 False"""
        monkeypatch.setenv("QQBOT_APP_ID", "some_id")
        monkeypatch.setenv("QQBOT_APP_SECRET", "")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        from web.server import restart_qq_bot_task
        result = await restart_qq_bot_task(mock_app)

        assert result is False
        assert mock_app.state.qq_task is None

    @pytest.mark.asyncio
    async def test_returns_false_when_qq_bot_disabled(self, mock_app, monkeypatch):
        """ENABLE_QQ_BOT=false → 不启动，返回 False"""
        monkeypatch.setenv("QQBOT_APP_ID", "some_id")
        monkeypatch.setenv("QQBOT_APP_SECRET", "some_secret")
        monkeypatch.setenv("ENABLE_QQ_BOT", "false")

        from web.server import restart_qq_bot_task
        result = await restart_qq_bot_task(mock_app)

        assert result is False
        assert mock_app.state.qq_task is None

    @pytest.mark.asyncio
    async def test_preserves_sandbox_config(self, mock_app, monkeypatch):
        """sandbox 配置应从 AGENT_CONFIG 正确传递给 run_qq_bot"""
        monkeypatch.setenv("QQBOT_APP_ID", "app_id")
        monkeypatch.setenv("QQBOT_APP_SECRET", "secret")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        captured_kwargs = {}

        async def mock_run_qq_bot(*args, **kwargs):
            captured_kwargs.update(kwargs)
            await asyncio.sleep(100)

        with patch("qq_bot_adapter.run_qq_bot", side_effect=mock_run_qq_bot), \
             patch("config.AGENT_CONFIG", {"qq_bot": {"is_sandbox": True}}):
            from web.server import restart_qq_bot_task
            await restart_qq_bot_task(mock_app)
            # yield 控制权让 create_task 调度的协程开始执行
            await asyncio.sleep(0.1)

        # 验证 sandbox=True 已传递
        assert captured_kwargs.get("sandbox") is True

        # 清理
        mock_app.state.qq_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await mock_app.state.qq_task
