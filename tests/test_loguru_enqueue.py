"""验证 utils/logging_config.py 中文件 sink 启用 enqueue=True，
stderr sink 保持同步（不启用 enqueue）。

对应 v3 spec P1-4：避免异步上下文里同步文件 I/O 阻塞事件循环。
"""
import pytest
from loguru import logger

from utils.logging_config import setup_logging
import contextlib


@pytest.fixture
def isolated_logger():
    """每个测试前后清理 loguru handlers，避免状态泄漏到其他测试。"""
    saved_handlers = dict(logger._core.handlers)
    yield logger
    # 还原：移除测试期间新增的 handler，保留原有 handler
    current_ids = set(logger._core.handlers.keys())
    saved_ids = set(saved_handlers.keys())
    for hid in list(current_ids - saved_ids):
        with contextlib.suppress(ValueError):
            logger.remove(hid)


def _classify_handlers():
    """遍历当前 logger handlers，按 sink 类型分类返回 enqueue 状态。

    返回 (file_enqueues, stderr_enqueues)：
    - file_enqueues: 文件 sink（FileSink，有 _path 属性）的 _enqueue 列表
    - stderr_enqueues: 非 file sink（stderr / _json_sink 等）的 _enqueue 列表
    """
    file_enqueues = []
    stderr_enqueues = []
    for handler in logger._core.handlers.values():
        sink = handler._sink
        # FileSink 有 _path 属性，StreamSink/FunctionSink 没有
        path = getattr(sink, "_path", None)
        if path is not None:
            file_enqueues.append(handler._enqueue)
        else:
            stderr_enqueues.append(handler._enqueue)
    return file_enqueues, stderr_enqueues


def test_file_sinks_have_enqueue_true(isolated_logger):
    """两个文件 sink（agent_*.json 与 agent.log）应启用 enqueue=True。"""
    setup_logging()
    file_enqueues, stderr_enqueues = _classify_handlers()

    assert len(file_enqueues) == 2, (
        f"应有 2 个文件 sink，实际 {len(file_enqueues)}"
    )
    assert all(file_enqueues), (
        f"所有文件 sink 应启用 enqueue，实际 {file_enqueues}"
    )


def test_stderr_sink_does_not_enqueue(isolated_logger):
    """stderr sink（_json_sink 或 sys.stderr）不应启用 enqueue。"""
    setup_logging()
    file_enqueues, stderr_enqueues = _classify_handlers()

    assert len(stderr_enqueues) >= 1, (
        "应有至少 1 个 stderr sink"
    )
    assert all(not e for e in stderr_enqueues), (
        f"stderr sink 不应启用 enqueue，实际 {stderr_enqueues}"
    )
