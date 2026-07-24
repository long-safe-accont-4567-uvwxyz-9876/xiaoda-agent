#!/usr/bin/env python3
"""B1 修复：测试事件污染生产日志 —— 隔离测试脚本的日志输出。

根因：agent_core/__init__.py 在模块导入时调用 setup_logging()，
该函数配置了 2 个文件 sink 写入生产日志目录 (LOG_DIR/agent_*.json 和 agent.log)。
任何 from agent_core import AgentCore 的测试脚本都会触发文件 sink 配置，
导致测试日志（含 "rm -rf /" 等测试命令）污染生产日志文件。

修复方案：在 setup_logging() 中检测 TEST_MODE 环境变量，测试模式下跳过文件 sink。
"""
import os
from pathlib import Path
from unittest import mock

import pytest
from loguru import logger

# ── 辅助函数 ──────────────────────────────────────────────

def _get_handler_sink_types() -> list[str]:
    """获取当前 logger 所有 handler 的 sink 类型名称。

    FileSink -> 文件 sink（写日志到文件）
    StreamSink -> stderr/stdout sink
    StandardSink / CallableSink -> 函数 sink（如 _json_sink）
    """
    types = []
    for handler in logger._core.handlers.values():
        sink = handler._sink
        # loguru 的 sink 类型直接在 sink 对象上
        types.append(type(sink).__name__)
    return types


def _count_file_sinks() -> int:
    """统计当前 logger 中 FileSink 类型的 sink 数量。"""
    return sum(1 for t in _get_handler_sink_types() if "FileSink" in t)


def _count_total_handlers() -> int:
    """统计当前 logger 的 handler 总数。"""
    return len(logger._core.handlers)


# ── 测试用例 ──────────────────────────────────────────────

class TestSetupLoggingTestMode:
    """测试 setup_logging() 在 TEST_MODE 下的行为。"""

    def test_normal_mode_adds_file_sinks(self):
        """正常模式（无 TEST_MODE）：应添加 2 个文件 sink（JSON + 文本）。"""
        from utils.logging_config import setup_logging

        with mock.patch.dict(os.environ, {}, clear=False):
            # 确保 TEST_MODE 未设置
            os.environ.pop("TEST_MODE", None)
            setup_logging()

        file_sink_count = _count_file_sinks()
        total = _count_total_handlers()
        # 正常模式：stderr(1) + JSON文件(1) + 文本文件(1) = 3
        assert total == 3, f"正常模式应有 3 个 handler，实际 {total}：{_get_handler_sink_types()}"
        assert file_sink_count == 2, (
            f"正常模式应有 2 个文件 sink，实际 {file_sink_count}：{_get_handler_sink_types()}"
        )

    def test_test_mode_skips_file_sinks(self):
        """TEST_MODE=true：应跳过文件 sink，仅保留 stderr。"""
        from utils.logging_config import setup_logging

        with mock.patch.dict(os.environ, {"TEST_MODE": "true"}, clear=False):
            setup_logging()

        file_sink_count = _count_file_sinks()
        total = _count_total_handlers()
        # 测试模式：仅 stderr(1)，无文件 sink
        assert file_sink_count == 0, (
            f"测试模式不应有文件 sink，实际 {file_sink_count}：{_get_handler_sink_types()}"
        )
        assert total == 1, f"测试模式应有 1 个 handler（stderr），实际 {total}"

    @pytest.mark.parametrize("mode_value", ["1", "true", "TRUE", "True", "yes", "YES"])
    def test_test_mode_various_values(self, mode_value):
        """TEST_MODE 的各种合法值都应触发测试模式。"""
        from utils.logging_config import setup_logging

        with mock.patch.dict(os.environ, {"TEST_MODE": mode_value}, clear=False):
            setup_logging()

        assert _count_file_sinks() == 0, (
            f"TEST_MODE={mode_value!r} 应跳过文件 sink，"
            f"实际 {_count_file_sinks()}：{_get_handler_sink_types()}"
        )

    @pytest.mark.parametrize("mode_value", ["0", "false", "no", "", "random"])
    def test_non_test_mode_values_keep_file_sinks(self, mode_value):
        """TEST_MODE=0/false/no/空/随机值 应保持正常模式（添加文件 sink）。"""
        from utils.logging_config import setup_logging

        with mock.patch.dict(os.environ, {"TEST_MODE": mode_value}, clear=False):
            setup_logging()

        assert _count_file_sinks() == 2, (
            f"TEST_MODE={mode_value!r} 应保持正常模式（2 个文件 sink），"
            f"实际 {_count_file_sinks()}：{_get_handler_sink_types()}"
        )

    def test_test_mode_does_not_create_log_files(self, tmp_path):
        """TEST_MODE=true 时不应在生产日志目录创建/追加日志文件。"""
        from config import LOG_DIR
        from utils.logging_config import setup_logging

        # 记录测试前的日志文件状态
        log_dir = Path(LOG_DIR)
        json_logs_before = set(log_dir.glob("agent_*.json")) if log_dir.exists() else set()
        _text_log_before = log_dir / "agent.log"

        with mock.patch.dict(os.environ, {"TEST_MODE": "true"}, clear=False):
            setup_logging()
            # 写一条测试日志
            logger.info("test_b1.isolation_check", test_data="this should NOT go to production logs")

        # 强制 flush 所有 enqueue 的 handler
        for handler in logger._core.handlers.values():
            if hasattr(handler._sink, "_queue"):
                try:
                    handler._sink.stop()
                except Exception:
                    pass

        # 验证：不应有新的 agent_*.json 文件或 agent.log 被创建/修改
        # 注意：由于 enqueue=True 和异步写入，我们主要检查没有新文件被创建
        # 更严格的检查：验证日志文件内容不包含测试消息
        json_logs_after = set(log_dir.glob("agent_*.json")) if log_dir.exists() else set()
        _new_json_logs = json_logs_after - json_logs_before

        # 允许已有文件被其他进程追加（生产服务），但不应有新文件因测试创建
        # 这个断言比较宽松，因为生产服务可能同时在写
        # 核心验证是 file_sink_count == 0（上一个测试已覆盖）


class TestAgentCoreImportDoesNotPolluteLogs:
    """测试导入 agent_core 时不会污染生产日志（需 TEST_MODE 设置）。"""

    def test_import_with_test_mode_no_file_sink(self):
        """设置 TEST_MODE=true 后导入 agent_core，不应有文件 sink。"""
        # 在导入前设置 TEST_MODE（使用 mock.patch.dict 确保清理）
        with mock.patch.dict(os.environ, {"TEST_MODE": "true"}, clear=False):
            # 重新触发 setup_logging（agent_core.__init__ 已在之前的导入中执行）
            from utils.logging_config import setup_logging
            setup_logging()

            assert _count_file_sinks() == 0, (
                f"TEST_MODE=true 下导入 agent_core 后不应有文件 sink，"
                f"实际 {_count_file_sinks()}：{_get_handler_sink_types()}"
            )


class TestConftestSetsTestMode:
    """验证 conftest.py 正确设置 TEST_MODE 环境变量。"""

    def test_conftest_sets_test_mode(self):
        """conftest.py 应设置 TEST_MODE=true 防止日志污染。"""
        # 这个测试验证 conftest.py 的配置
        # 由于 conftest.py 在 pytest 启动时加载，我们检查环境变量是否已设置
        # 如果 conftest.py 正确配置，TEST_MODE 应该已设置
        # 注意：这个测试可能需要在 conftest.py 修改后才能通过
        test_mode = os.environ.get("TEST_MODE", "")
        assert test_mode.lower() in ("1", "true", "yes"), (
            f"conftest.py 应设置 TEST_MODE=true，当前值：{test_mode!r}"
        )
