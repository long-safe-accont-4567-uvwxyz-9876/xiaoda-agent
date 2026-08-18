"""USB 只读日志崩溃防御回归测试（Task 3）。

背景: 生产 USB 外接盘 (/media/orangepi/KIOXIA) 因文件系统错误 remount 只读时,
loguru logger.add() 创建日志文件抛 OSError: [Errno 30] Read-only file system,
导致全应用崩溃。L6 修复在 utils/logging_config.py:128-168 已加 try/except
保护文件 sink, 并在 CR-FIX 中补全了「部分 sink 注册后失败需回滚清理」逻辑。

本测试聚焦 CR-FIX 行为: 第二个文件 sink 失败时, 第一个已注册的文件 sink
必须被 logger.remove() 清理, 避免残留 sink 指向不可写路径导致后续日志写入再次崩溃。

crash 证据 (均已早于 L6 修复, 现为回归锁定):
  nahida-agent/crash.log: Jun 21  PermissionError
  xiaoda-agent/crash.log: Jul 17  OSError Read-only file system
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_partial_sink_cleanup_when_second_file_sink_fails(monkeypatch):
    """CR-FIX: 第一个文件 sink 注册成功后第二个失败，必须回滚清理第一个。"""
    import utils.logging_config as lc

    removed_sinks: list = []
    added_file_sinks: list = []
    original_add = lc.logger.add
    original_remove = lc.logger.remove

    def _tracking_add(sink, **kwargs):
        # 仅文件 sink (str 路径) 走受控逻辑；stderr/callable 正常注册
        if isinstance(sink, str):
            # 第一个文件 sink (agent_*.json) 成功；第二个 (agent.log) 抛错
            if not added_file_sinks:
                sid = original_add(sink, **kwargs)
                added_file_sinks.append(sid)
                return sid
            raise OSError(30, "Read-only file system")
        return original_add(sink, **kwargs)

    def _tracking_remove(sid=None):
        # logger.remove() 无参 = 移除所有 sink（setup_logging 开头调用）
        if sid is not None:
            removed_sinks.append(sid)
            try:
                original_remove(sid)
            except (ValueError, KeyError):
                pass
        else:
            original_remove()

    monkeypatch.setattr(lc.logger, "add", _tracking_add)
    monkeypatch.setattr(lc.logger, "remove", _tracking_remove)
    monkeypatch.setattr(lc, "LOG_DIR", Path("/tmp/test_usb_ro_partial_9999"))
    monkeypatch.delenv("TEST_MODE", raising=False)

    try:
        lc.setup_logging()  # 不应抛异常
    except (OSError, PermissionError) as e:
        pytest.fail(f"setup_logging 在第二个文件 sink 失败时崩溃: {e}")
    finally:
        # 清理测试注册的 sink
        for sid in added_file_sinks:
            try:
                original_remove(sid)
            except (ValueError, KeyError):
                pass
        # 清理 stderr sink
        try:
            lc.logger.remove()
        except Exception:
            pass
        lc.logger.add(lambda *_: None)

    # CR-FIX 核心断言: 第一个文件 sink 必须被回滚清理
    assert added_file_sinks, "前置失败: 第一个文件 sink 未注册"
    assert added_file_sinks[0] in removed_sinks, (
        "CR-FIX 回归: 第二个文件 sink 失败后, 第一个已注册的文件 sink 未被清理, "
        "残留 sink 将在后续日志写入时再次崩溃"
    )


def test_setup_logging_never_propagates_oserror(monkeypatch):
    """USB 只读: setup_logging 任何文件 sink 失败都不应传播 OSError/PermissionError。"""
    import utils.logging_config as lc

    original_add = lc.logger.add

    def _failing_file_add(sink, **kwargs):
        if isinstance(sink, str):
            raise OSError(30, "Read-only file system")
        return original_add(sink, **kwargs)

    monkeypatch.setattr(lc.logger, "add", _failing_file_add)
    monkeypatch.setattr(lc, "LOG_DIR", Path("/tmp/test_usb_ro_all_9999"))
    monkeypatch.delenv("TEST_MODE", raising=False)

    try:
        lc.setup_logging()
    except (OSError, PermissionError) as e:
        pytest.fail(f"setup_logging 不应在 USB 只读时传播异常: {e}")
    finally:
        try:
            lc.logger.remove()
        except Exception:
            pass
        lc.logger.add(lambda *_: None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
