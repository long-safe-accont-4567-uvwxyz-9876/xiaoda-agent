"""消除凭证保存与核心重初始化的双 Bot 竞态 — 回归测试

背景（根因）：save_keys 对同一次保存独立创建 _background_reinit() 和
_restart_qq_bot_after_save() 两个后台任务。两者各自碰 app.state.qq_task，
force 重启可能先于 core.init() 完成导致双 Bot 同时存活。

修复：save_keys 把两者串成一个任务——_background_reinit() 完成后再调
_restart_qq_bot_after_save()，只 create_task 一次。若 QQ 凭证未变更则
只跑 _background_reinit()。
"""
import asyncio
import contextlib
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))


@pytest.fixture(autouse=True)
def reset_qq_adapter_module_vars():
    """每个测试前后保存/恢复 qq_bot_adapter 模块级 APP_ID/APP_SECRET。"""
    import qq_bot_adapter
    orig_app_id = qq_bot_adapter.APP_ID
    orig_app_secret = qq_bot_adapter.APP_SECRET
    yield
    qq_bot_adapter.APP_ID = orig_app_id
    qq_bot_adapter.APP_SECRET = orig_app_secret


def _make_app():
    """构造 app，state 用 SimpleNamespace 保证 getattr 默认值正常工作。

    根因：用 MagicMock 做 state 会导致 getattr(app.state, "_qq_applied_fingerprint",
    None) 返回 MagicMock 而非 None，破坏指纹合并判定。
    """
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace(
        core=types.SimpleNamespace(_initialized=False),
        qq_task=None,
        _qq_applied_fingerprint=None,
        _qq_restart_lock=None,
        agent_registry=None,
    )
    return app


class TestReinitAndMaybeRestartQq:
    """验证 _reinit_and_maybe_restart_qq 串行化核心重初始化与 QQ 重启。"""

    async def test_qq_restart_waits_for_core_reinit_and_single_bot_survives(self, monkeypatch):
        """QQ 凭证变更：reinit 先于 qq restart，最终只留一个存活 bot task。

        场景：core._initialized=False，core.init() 用可控 Future 模拟延迟。
        _background_reinit 阻塞在 core.init() 期间，QQ bot 不应被创建。
        core.init() 完成后 reinit 创建 task A，restart 取消 A 创建 B，
        最终只留 B 存活。
        """
        app = _make_app()

        # 可控 Future：core.init() 阻塞直到外部 resolve
        init_future = asyncio.get_running_loop().create_future()

        async def mock_core_init(reinit=False):
            await init_future
            app.state.core._initialized = True
            return True

        app.state.core.init = mock_core_init

        # QQ 凭证
        monkeypatch.setenv("QQBOT_APP_ID", "test_app_id")
        monkeypatch.setenv("QQBOT_APP_SECRET", "test_app_secret")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        # 记录 run_qq_bot 创建的 task（无论是否被 cancel 都记录）
        run_qq_bot_tasks: list[asyncio.Task] = []

        async def mock_run_qq_bot(*args, **kwargs):
            run_qq_bot_tasks.append(asyncio.current_task())
            await asyncio.sleep(100)  # 保持 task 存活，模拟长期运行的 Bot

        # mock web.app_ref：get_app 返回我们的 app，get_start_services 返回
        # 一个只调 ensure_qq_bot_task 的简化 _start_services（绕过真实
        # _start_services 的 tool_engine 等重依赖）
        import web.app_ref as app_ref

        async def mock_start_services(app, core):
            from web.server import ensure_qq_bot_task
            await ensure_qq_bot_task(app)

        monkeypatch.setattr(app_ref, "get_app", lambda: app)
        monkeypatch.setattr(app_ref, "get_start_services", lambda: mock_start_services)

        with patch("qq_bot_adapter.run_qq_bot", side_effect=mock_run_qq_bot), \
             patch("config.AGENT_CONFIG", {"qq_bot": {"is_sandbox": False}}):
            from web.routers.setup import _reinit_and_maybe_restart_qq
            task = asyncio.create_task(_reinit_and_maybe_restart_qq(qq_changed=True))

            # 等待 _background_reinit 开始执行 core.init() 并阻塞
            await asyncio.sleep(0.05)

            # core.init() 未完成，QQ bot 不应被创建
            assert app.state.qq_task is None, \
                "core init 未完成时不应创建 QQ bot task"

            # 完成 core.init()
            init_future.set_result(True)

            # 等待整个流程完成
            await task

            # 验证：run_qq_bot 至少被调用一次（reinit 创建），可能两次（restart 再创建）
            assert len(run_qq_bot_tasks) >= 1, \
                f"run_qq_bot 应至少被调用 1 次，实际 {len(run_qq_bot_tasks)} 次"

            # 验证：最终只有一个存活 bot task（未完成、未取消）
            surviving = [
                t for t in run_qq_bot_tasks
                if not t.done() and not t.cancelled()
            ]
            assert len(surviving) == 1, \
                f"应有 1 个存活 bot task，实际 {len(surviving)}（共创建 {len(run_qq_bot_tasks)} 个）"

            # app.state.qq_task 应指向存活的 task
            assert app.state.qq_task is surviving[0], \
                "app.state.qq_task 应指向存活的 bot task"
            assert app.state.qq_task in run_qq_bot_tasks, \
                "app.state.qq_task 应是 run_qq_bot 创建的 task"

            # 清理
            app.state.qq_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, RuntimeError):
                await app.state.qq_task

    async def test_call_order_reinit_before_restart(self):
        """_reinit_and_maybe_restart_qq 应先调 _background_reinit 再调 _restart_qq_bot_after_save。

        根因：原实现两个后台任务并发，执行顺序不确定。串行化后顺序固定。
        """
        call_order: list[str] = []

        async def mock_reinit():
            call_order.append("reinit")

        async def mock_restart():
            call_order.append("restart")

        with patch("web.routers.setup._background_reinit", new=mock_reinit), \
             patch("web.routers.setup._restart_qq_bot_after_save", new=mock_restart):
            from web.routers.setup import _reinit_and_maybe_restart_qq
            await _reinit_and_maybe_restart_qq(qq_changed=True)

        assert call_order == ["reinit", "restart"], \
            f"调用顺序应为 reinit → restart，实际 {call_order}"

    async def test_no_restart_when_qq_unchanged(self):
        """QQ 凭证未变更时只调 _background_reinit，不调 _restart_qq_bot_after_save。"""
        call_order: list[str] = []

        async def mock_reinit():
            call_order.append("reinit")

        async def mock_restart():
            call_order.append("restart")

        with patch("web.routers.setup._background_reinit", new=mock_reinit), \
             patch("web.routers.setup._restart_qq_bot_after_save", new=mock_restart):
            from web.routers.setup import _reinit_and_maybe_restart_qq
            await _reinit_and_maybe_restart_qq(qq_changed=False)

        assert call_order == ["reinit"], \
            f"QQ 凭证未变更时只应调 reinit，实际 {call_order}"

    async def test_task_added_to_reinit_tasks_for_gc_protection(self):
        """_reinit_and_maybe_restart_qq 的 task 应加入 _reinit_tasks 防止 GC 回收。

        根因：asyncio.create_task 返回的 Task 若无强引用，会被 Python GC 回收，
        导致 task 静默消失。_reinit_tasks 作为强引用集合保留 task，完成后由
        后续任务的 finally 惰性清理。

        注意：task 自身的 finally 执行时 done() 仍为 False（task 尚未标记完成），
        所以无法自我清理。清理发生在下一个 _reinit_and_maybe_restart_qq 的 finally。
        """
        async def mock_reinit():
            pass

        with patch("web.routers.setup._background_reinit", new=mock_reinit):
            from web.routers import setup as setup_module
            # 清空已完成的 task，确保干净的测试环境
            setup_module._reinit_tasks[:] = [
                t for t in setup_module._reinit_tasks if not t.done()
            ]
            # 模拟 save_keys 的写法：create_task 后立即 append 到 _reinit_tasks
            task1 = asyncio.create_task(
                setup_module._reinit_and_maybe_restart_qq(qq_changed=False)
            )
            setup_module._reinit_tasks.append(task1)

            # task 创建后应在 _reinit_tasks 中（强引用防 GC）
            assert task1 in setup_module._reinit_tasks, \
                "task 应在 _reinit_tasks 中以防 GC 回收"

            await task1

            # task1 完成后仍在 _reinit_tasks 中（自身 finally 时 done()=False 无法自我清理）
            assert task1 in setup_module._reinit_tasks, \
                "task1 完成后仍应由 _reinit_tasks 持有，待下次惰性清理"

            # 再跑一个 task，其 finally 会清理已完成的 task1
            task2 = asyncio.create_task(
                setup_module._reinit_and_maybe_restart_qq(qq_changed=False)
            )
            setup_module._reinit_tasks.append(task2)
            await task2

            # task1 应被 task2 的 finally 清理
            assert task1 not in setup_module._reinit_tasks, \
                "task1 应被后续 task 的 finally 惰性清理"


class TestSaveKeysSingleTask:
    """验证 save_keys 只创建一个后台任务且 qq_changed 判定正确。"""

    @staticmethod
    def _mock_setup_wizard(monkeypatch):
        """注入 mock setup_wizard 模块，避免真实 .env 读写。"""
        mock_sw = types.ModuleType("setup_wizard")
        mock_sw.ENV_PATH = "/tmp/test_env_setup_task5"
        mock_sw.ENV_EXAMPLE_PATH = "/tmp/test_env_example_setup_task5"
        mock_sw.REQUIRED_KEYS = []
        mock_sw._parse_env_lines = lambda x: []
        mock_sw._load_env_values = lambda: {}
        mock_sw._write_env = lambda x, y: None
        mock_sw.is_first_run = lambda: False
        monkeypatch.setitem(sys.modules, "setup_wizard", mock_sw)

    @staticmethod
    def _mock_save_helpers(monkeypatch):
        """mock save_keys 的辅助函数，避免副作用。"""
        async def mock_auto_register_providers(updates):
            return []

        async def mock_reload_env_and_cache(updates, env_path):
            # 模拟真实 _reload_env_and_cache 的 os.environ 更新
            for k, v in updates.items():
                vs = v.strip() if isinstance(v, str) else ""
                if vs:
                    os.environ[k] = vs

        monkeypatch.setattr("web.routers.setup._reload_env_and_cache", mock_reload_env_and_cache)
        monkeypatch.setattr("web.routers.setup._reset_credential_pool", lambda updates: None)
        monkeypatch.setattr("web.routers.setup._update_config_and_refresh_clients", lambda updates: None)
        monkeypatch.setattr("web.routers.setup._auto_register_providers", mock_auto_register_providers)

    async def test_save_keys_qq_changed_creates_single_task(self, monkeypatch):
        """保存新 QQ 凭证时 save_keys 应只 create_task 一次，且 qq_changed=True。

        根因：原实现创建两个后台任务（_background_reinit + _restart_qq_bot_after_save），
        修复后只创建一个（_reinit_and_maybe_restart_qq）。
        """
        self._mock_setup_wizard(monkeypatch)
        self._mock_save_helpers(monkeypatch)

        # 保存前 QQ 凭证为空（确保 _qq_changed=True）
        monkeypatch.delenv("QQBOT_APP_ID", raising=False)
        monkeypatch.delenv("QQBOT_APP_SECRET", raising=False)
        monkeypatch.delenv("ENABLE_QQ_BOT", raising=False)

        # 记录 _reinit_and_maybe_restart_qq 调用
        captured_args: list[bool] = []

        async def mock_reinit_and_restart(qq_changed: bool):
            captured_args.append(qq_changed)

        monkeypatch.setattr(
            "web.routers.setup._reinit_and_maybe_restart_qq",
            mock_reinit_and_restart,
        )

        from web.routers.setup import _reinit_tasks, save_keys

        # 清空已完成的 task
        _reinit_tasks[:] = [t for t in _reinit_tasks if not t.done()]
        task_count_before = len(_reinit_tasks)

        body = {
            "keys": {
                "QQBOT_APP_ID": "new_app_id",
                "QQBOT_APP_SECRET": "new_secret",
                "ENABLE_QQ_BOT": "true",
            }
        }
        result = await save_keys(body)

        # 等待后台任务完成
        for t in list(_reinit_tasks):
            if not t.done():
                await t

        assert result.ok is True, f"save_keys 应成功，实际 {result}"
        # 只新增了一个 task
        assert len(_reinit_tasks) <= task_count_before + 1, \
            f"应只新增 1 个 task，_reinit_tasks 变化：{task_count_before} → {len(_reinit_tasks)}"
        # _reinit_and_maybe_restart_qq 被调用一次，qq_changed=True
        assert captured_args == [True], \
            f"_reinit_and_maybe_restart_qq 应被调用一次且 qq_changed=True，实际 {captured_args}"

    async def test_save_keys_qq_unchanged_no_force_restart(self, monkeypatch):
        """QQ 凭证未变更（用户重新提交相同值）时 qq_changed=False，不强制重启。"""
        self._mock_setup_wizard(monkeypatch)
        self._mock_save_helpers(monkeypatch)

        # 保存前后 QQ 凭证相同
        monkeypatch.setenv("QQBOT_APP_ID", "same_app_id")
        monkeypatch.setenv("QQBOT_APP_SECRET", "same_secret")
        monkeypatch.setenv("ENABLE_QQ_BOT", "true")

        captured_args: list[bool] = []

        async def mock_reinit_and_restart(qq_changed: bool):
            captured_args.append(qq_changed)

        monkeypatch.setattr(
            "web.routers.setup._reinit_and_maybe_restart_qq",
            mock_reinit_and_restart,
        )

        from web.routers.setup import _reinit_tasks, save_keys

        _reinit_tasks[:] = [t for t in _reinit_tasks if not t.done()]

        body = {
            "keys": {
                "QQBOT_APP_ID": "same_app_id",
                "QQBOT_APP_SECRET": "same_secret",
                "ENABLE_QQ_BOT": "true",
            }
        }
        result = await save_keys(body)

        for t in list(_reinit_tasks):
            if not t.done():
                await t

        assert result.ok is True, f"save_keys 应成功，实际 {result}"
        assert captured_args == [False], \
            f"QQ 凭证未变更时 qq_changed 应为 False，实际 {captured_args}"

    async def test_save_keys_non_qq_update_qq_unchanged(self, monkeypatch):
        """非 QQ 凭证更新时 qq_changed 应为 False。"""
        self._mock_setup_wizard(monkeypatch)
        self._mock_save_helpers(monkeypatch)

        captured_args: list[bool] = []

        async def mock_reinit_and_restart(qq_changed: bool):
            captured_args.append(qq_changed)

        monkeypatch.setattr(
            "web.routers.setup._reinit_and_maybe_restart_qq",
            mock_reinit_and_restart,
        )

        from web.routers.setup import _reinit_tasks, save_keys

        _reinit_tasks[:] = [t for t in _reinit_tasks if not t.done()]

        body = {
            "keys": {
                "MIMO_API_KEY": "some_mimo_key",
            }
        }
        result = await save_keys(body)

        for t in list(_reinit_tasks):
            if not t.done():
                await t

        assert result.ok is True, f"save_keys 应成功，实际 {result}"
        assert captured_args == [False], \
            f"非 QQ 凭证更新时 qq_changed 应为 False，实际 {captured_args}"

    async def test_save_keys_qq_enable_change_triggers_restart(self, monkeypatch):
        """仅 ENABLE_QQ_BOT 从 false 变 true 也应触发 qq_changed=True。"""
        self._mock_setup_wizard(monkeypatch)
        self._mock_save_helpers(monkeypatch)

        # 旧值：ENABLE_QQ_BOT=false
        monkeypatch.setenv("QQBOT_APP_ID", "app_id")
        monkeypatch.setenv("QQBOT_APP_SECRET", "secret")
        monkeypatch.setenv("ENABLE_QQ_BOT", "false")

        captured_args: list[bool] = []

        async def mock_reinit_and_restart(qq_changed: bool):
            captured_args.append(qq_changed)

        monkeypatch.setattr(
            "web.routers.setup._reinit_and_maybe_restart_qq",
            mock_reinit_and_restart,
        )

        from web.routers.setup import _reinit_tasks, save_keys

        _reinit_tasks[:] = [t for t in _reinit_tasks if not t.done()]

        # 新值：ENABLE_QQ_BOT=true
        body = {
            "keys": {
                "QQBOT_APP_ID": "app_id",
                "QQBOT_APP_SECRET": "secret",
                "ENABLE_QQ_BOT": "true",
            }
        }
        result = await save_keys(body)

        for t in list(_reinit_tasks):
            if not t.done():
                await t

        assert result.ok is True, f"save_keys 应成功，实际 {result}"
        assert captured_args == [True], \
            f"ENABLE_QQ_BOT 变更时 qq_changed 应为 True，实际 {captured_args}"
