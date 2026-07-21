"""P1-3 测试: 邮件 token 刷新超时不应标记完成。

Bug: timeout 分支调用 set_cron_last_run("mail_token_refresh") 会更新最后运行时间戳，
而 _should_run() 基于 cron_last_run 判断是否到下次运行时间，导致超时后 2 小时内
不再重试，瞬态网络故障可让 OAuth token 失效长达 2 小时。

修复目标: timeout 分支不应调用 set_cron_last_run，保持 stale 让下次 cron 检查重试。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_timeout_does_not_mark_cron_complete():
    """超时分支不应调用 set_cron_last_run。"""
    from core.background_tasks import BackgroundTaskManager

    # 构造一个最小可测的 BackgroundTaskManager 实例
    mgr = BackgroundTaskManager.__new__(BackgroundTaskManager)
    mgr.db = MagicMock()
    mgr.db.set_cron_last_run = AsyncMock()
    mgr.db.get_cron_last_run = AsyncMock(return_value=0)

    # mock _resolve_agently_cli 返回 True（进入 timeout 路径）
    # mock _run_agently 触发 asyncio.TimeoutError
    async def _slow_agently(*args, **kwargs):
        await asyncio.sleep(100)  # 不会被真的等到，wait_for 会先超时

    with patch("tools.mail_tools._resolve_agently_cli", return_value="/fake/agently"), \
         patch("tools.mail_tools._run_agently", side_effect=_slow_agently):
        # 用很短的 timeout 加速测试
        with patch("core.background_tasks.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            await mgr._refresh_mail_token_task()

    # 关键断言: 超时分支不应调用 set_cron_last_run
    # 允许其他 cron 标记（如果有），但 mail_token_refresh 不应被标记
    calls = [call.args[0] if call.args else call.kwargs.get("key") for call in mgr.db.set_cron_last_run.call_args_list]
    assert "mail_token_refresh" not in calls, (
        f"timeout 分支不应调用 set_cron_last_run('mail_token_refresh')，但调用了。calls={calls}"
    )


@pytest.mark.asyncio
async def test_success_marks_cron_complete():
    """成功分支应正常调用 set_cron_last_run。"""
    from core.background_tasks import BackgroundTaskManager

    mgr = BackgroundTaskManager.__new__(BackgroundTaskManager)
    mgr.db = MagicMock()
    mgr.db.set_cron_last_run = AsyncMock()
    mgr.db.get_cron_last_run = AsyncMock(return_value=0)

    # mock _run_agently 返回成功
    async def _ok_agently(*args, **kwargs):
        return (0, "ok", "")

    with patch("tools.mail_tools._resolve_agently_cli", return_value="/fake/agently"), \
         patch("tools.mail_tools._run_agently", side_effect=_ok_agently):
        await mgr._refresh_mail_token_task()

    # 成功后应标记 cron
    calls = [call.args[0] if call.args else call.kwargs.get("key") for call in mgr.db.set_cron_last_run.call_args_list]
    assert "mail_token_refresh" in calls, "成功后应调用 set_cron_last_run('mail_token_refresh')"


@pytest.mark.asyncio
async def test_no_agently_cli_still_marks_cron():
    """agently-cli 不存在时（早期 return）仍应标记 cron（与 timeout 不同）。"""
    from core.background_tasks import BackgroundTaskManager

    mgr = BackgroundTaskManager.__new__(BackgroundTaskManager)
    mgr.db = MagicMock()
    mgr.db.set_cron_last_run = AsyncMock()
    mgr.db.get_cron_last_run = AsyncMock(return_value=0)

    # _resolve_agently_cli 返回空（未安装）
    with patch("tools.mail_tools._resolve_agently_cli", return_value=""):
        await mgr._refresh_mail_token_task()

    # 这种情况下任务"主动决定不运行"，标记 cron 是合理的
    calls = [call.args[0] if call.args else call.kwargs.get("key") for call in mgr.db.set_cron_last_run.call_args_list]
    assert "mail_token_refresh" in calls, "agently-cli 缺失时应标记 cron"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
