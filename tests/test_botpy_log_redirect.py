"""botpy 文件日志重定向测试（技术债批 1.4）。

背景：botpy.Client 构造时默认 ext_handlers=True，把 TimedRotatingFileHandler
挂到 botpy logger，文件固定写 os.getcwd()/botpy.log。项目侧在首个 Client
实例化前调用 qq_bot_adapter._redirect_botpy_file_log(log_dir=...) 预配置
自定义 handler，利用 SDK configure_logging 的判空幂等（_ext_handlers 非空
即不再追加默认），把文件日志定向到项目 LOG_DIR。
"""
import logging
import logging.handlers
import os
import time
from pathlib import Path

import botpy
import botpy.logging as bl
import pytest


def _reset_botpy_state():
    """重置 SDK 日志模块状态，模拟新进程首次配置。"""
    bl._ext_handlers = []
    bl.logs.clear()
    lg = logging.getLogger("botpy")
    for h in list(lg.handlers):
        lg.removeHandler(h)


@pytest.fixture(autouse=True)
def _sdk_clean_state():
    _reset_botpy_state()
    # 重置项目侧"已重定向"标志：每个测试模拟新进程首次配置
    import qq_bot_adapter as _qb

    _qb._BOTPY_LOG_REDIRECTED = False
    yield
    _reset_botpy_state()
    _qb._BOTPY_LOG_REDIRECTED = False


def _trigger_construct() -> None:
    """模拟 AIQQBot 实例化（触发 SDK configure_logging 与 handler 挂载，不触网）。

    SDK 某些版本/进程状态下 Client 构造会因已注册的 asyncio loop 抛
    RuntimeError；文件 handler 的挂载与构造行为解耦（configure_logging +
    get_logger），构造失败时直接显式 get_logger() 完成同样的挂载验证。
    """
    try:
        from botpy.client import Client

        Client(intents=botpy.Intents(public_messages=True))
    except (RuntimeError, NotImplementedError) as exc:  # 构造环境不兼容仅告警
        logging.getLogger("botpy").getChild("construct_skip").debug(str(exc)[:120])
        botpy.logging.get_logger()


def test_redirect_target_and_no_cwd_file(tmp_path):
    from qq_bot_adapter import _redirect_botpy_file_log

    log_dir = tmp_path / "logs"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    old_cwd = Path.cwd()
    os.chdir(cwd)
    try:
        _redirect_botpy_file_log(log_dir=log_dir)
        _trigger_construct()
        logging.getLogger("botpy").warning("probe-after-redirect")
        time.sleep(0.2)  # 等待 TimedRotatingFileHandler 刷盘
        target = log_dir / "botpy.log"
        assert target.exists(), f"{target} 未创建"
        content = target.read_text(encoding="utf-8")
        assert "probe-after-redirect" in content
        assert not (cwd / "botpy.log").exists(), "cwd 不应再产生 botpy.log"
    finally:
        os.chdir(old_cwd)


def test_redirect_idempotent(tmp_path):
    from qq_bot_adapter import _redirect_botpy_file_log

    _redirect_botpy_file_log(log_dir=tmp_path / "logs")
    _redirect_botpy_file_log(log_dir=tmp_path / "logs")
    _trigger_construct()
    lg = logging.getLogger("botpy")
    file_handlers = [
        h for h in lg.handlers
        if isinstance(h, logging.handlers.TimedRotatingFileHandler)
    ]
    assert len(file_handlers) == 1, f"文件 handler 应唯一，实际 {len(file_handlers)}"


def test_redirect_failure_does_not_raise(tmp_path):
    """目标路径不可用（落在一已存在文件之下）时静默回退，不抛异常。"""
    from qq_bot_adapter import _redirect_botpy_file_log

    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    bad_log_dir = blocker / "logs"  # mkdir 在文件路径下必然抛 FileNotFoundError
    _redirect_botpy_file_log(log_dir=bad_log_dir)  # 不抛即视为回退成功