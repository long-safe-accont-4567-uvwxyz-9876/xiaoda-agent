"""L6 测试: setup_logging 在日志目录不可写时不应崩溃.

Bug: 当 USB 外接盘 (/media/orangepi/KIOXIA) 因 FAT/ext4 文件系统错误
remount 为只读时，loguru 的 logger.add() 创建日志文件抛出
OSError: [Errno 30] Read-only file system，导致整个应用崩溃。

crash 日志证据:
  nahida-agent: PermissionError: [Errno 13] Permission denied
  xiaoda-agent: OSError: [Errno 30] Read-only file system

修复目标: 文件 sink 创建失败时降级到本地目录，stderr sink 不受影响。
"""
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def test_setup_logging_no_crash_on_readonly_dir(monkeypatch):
    """L6: LOG_DIR 不可写时 setup_logging 不应抛出异常。"""
    import utils.logging_config as lc

    # 模拟 LOG_DIR 指向一个不存在的只读路径
    fake_log_dir = Path("/tmp/test_l6_readonly_dir_9999")
    monkeypatch.setattr(lc, "LOG_DIR", fake_log_dir)

    # 模拟 mkdir 或 logger.add 抛出 OSError（只读文件系统）
    original_add = lc.logger.add

    def _mocked_add(sink, **kwargs):
        # 对文件 sink 抛错，对 stderr 正常工作
        if isinstance(sink, str) or (hasattr(sink, '__str__') and '.json' in str(sink)):
            raise OSError(30, "Read-only file system")
        return original_add(sink, **kwargs)

    monkeypatch.setattr(lc.logger, "add", _mocked_add)
    monkeypatch.setenv("TEST_MODE", "1")  # 先跳过文件 sink
    monkeypatch.delenv("TEST_MODE", raising=False)

    # 现在不用 TEST_MODE，模拟真实场景
    # setup_logging 应捕获异常而不崩溃
    try:
        lc.setup_logging()
        # 如果到达这里，说明没有崩溃
        assert True
    except (OSError, PermissionError) as e:
        pytest.fail(f"setup_logging 在日志目录不可写时崩溃了: {e}")
    finally:
        # 恢复 logger
        lc.logger.remove()
        lc.logger.add(lambda _: None)


def test_setup_logging_stderr_always_works(monkeypatch):
    """L6: 即使文件 sink 失败，stderr sink 必须正常工作。"""
    import utils.logging_config as lc

    # 模拟文件 sink 失败
    original_add = lc.logger.add
    call_count = {"stderr": 0, "file": 0}

    def _counting_add(sink, **kwargs):
        if isinstance(sink, str) or '.json' in str(sink):
            call_count["file"] += 1
            raise OSError(30, "Read-only file system")
        else:
            call_count["stderr"] += 1
            return original_add(sink, **kwargs)

    monkeypatch.setattr(lc.logger, "add", _counting_add)
    monkeypatch.delenv("TEST_MODE", raising=False)

    try:
        lc.setup_logging()
    except Exception:
        pass
    finally:
        lc.logger.remove()
        lc.logger.add(lambda _: None)

    # stderr sink 应至少被调用一次
    assert call_count["stderr"] >= 1, "stderr sink 应在文件 sink 失败时仍正常添加"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
