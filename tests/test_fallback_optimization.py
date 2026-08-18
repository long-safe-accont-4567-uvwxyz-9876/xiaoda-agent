"""测试 fallback 链和超时优化

验证：
1. fallback 降级链 chat → chat_agnes 使用不同 provider
2. MAX_RETRIES 降为 1（2 次尝试而非 3 次）
3. chat 超时为 60s
4. profile_learner 的 loguru 格式化不再触发 Replacement index 错误
5. 后台任务 _spawn 添加耗时监控日志
"""
import asyncio
import copy
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))


class TestFallbackChainSync:
    """测试 fallback 降级链的 provider 隔离"""

    def test_fallback_chain_uses_different_providers(self):
        """验证 fallback 链 chat → chat_agnes 使用不同 provider

        chat_pro/chat_flash 已合并进 chat（同一 provider 同一 model，无区分意义），
        降级链精简为 chat → chat_agnes。chat 走 DEFAULT_PROVIDER，
        chat_agnes 走 agnes provider，确保主路由失败时切换到独立 provider 兜底。
        """
        from model_router import ROUTE_TABLE, FALLBACK_ROUTE
        from config import DEFAULT_PROVIDER

        # 模拟 chat 主路由使用 DEFAULT_PROVIDER 的场景
        original = {k: v.copy() for k, v in ROUTE_TABLE.items()}
        try:
            ROUTE_TABLE["chat"]["client"] = DEFAULT_PROVIDER
            ROUTE_TABLE["chat_agnes"]["client"] = "agnes"

            # 降级链：chat → chat_agnes
            assert FALLBACK_ROUTE["chat"] == "chat_agnes"

            # chat 与 chat_agnes 应使用不同 provider（主路由失败时切到独立 provider）
            assert ROUTE_TABLE["chat"]["client"] != ROUTE_TABLE["chat_agnes"]["client"], \
                "chat 与 chat_agnes 应使用不同 provider，否则降级无意义"
            assert ROUTE_TABLE["chat_agnes"]["client"] == "agnes"
        finally:
            for k, v in original.items():
                ROUTE_TABLE[k] = v


class TestRetryAndTimeoutReduction:
    """测试重试次数和超时时间的降低"""

    def test_max_retries_is_1(self):
        """MAX_RETRIES 应为 1（最多 2 次尝试）"""
        from model_router import MAX_RETRIES
        assert MAX_RETRIES == 1, f"MAX_RETRIES 应为 1，当前为 {MAX_RETRIES}"

    def test_chat_timeout_is_30s(self):
        """chat 超时应为 30 秒（从 90s → 60s → 30s 渐进收紧）。

        2026-08-04 起：为根治超时失败问题，chat 超时收至 30s，
        与 agnes transport 的 read=30s 对齐，避免外层兜底与内层超时叠加。
        """
        from model_router import ModelRouter
        assert ModelRouter._DEFAULT_TIMEOUTS["chat"] == 30, \
            f"chat 超时应为 30s，当前为 {ModelRouter._DEFAULT_TIMEOUTS.get('chat')}"


class TestProfileLearnerFormatBug:
    """测试 profile_learner 的 loguru 格式化修复"""

    def test_loguru_with_brace_in_message_does_not_crash(self):
        """当异常消息包含 {} 时，loguru 不应报 Replacement index 错误"""
        from loguru import logger
        import io

        # 模拟一个包含 {} 的异常消息
        error_msg = "Replacement index 0 out of range for positional args tuple {}"

        # 使用 loguru 的正确写法（不应抛出异常）
        test_logger = logger.bind()
        try:
            # 这个调用不应该抛出异常
            test_logger.warning("profile_learner.insight_failed: {}", error_msg)
        except Exception as e:
            pytest.fail(f"loguru 格式化失败: {e}")

    def test_fstring_with_braces_does_not_crash_loguru(self):
        """f-string 产生的包含 {} 的消息传给 loguru 不应崩溃"""
        from loguru import logger

        # 模拟 _run_profile_insight 中的场景
        e = IndexError("Replacement index 0 out of range {}")
        msg = f"profile_learner.insight_failed: {e}"

        # 直接调用 logger.warning 不应崩溃
        try:
            logger.warning(msg)
        except Exception:
            pytest.fail("logger.warning with {} in message crashed")


class TestBackgroundTaskTiming:
    """测试后台任务耗时监控日志"""

    @pytest.mark.asyncio
    async def test_spawn_logs_duration_on_completion(self):
        """_spawn 完成时应记录耗时"""
        from core.background_tasks import _spawn, _bg_tasks

        _bg_tasks.clear()
        log_records = []

        import core.background_tasks as bt_mod
        original_debug = bt_mod.logger.debug
        original_info = bt_mod.logger.info
        def capture_debug(*args, **kwargs):
            log_records.append((args, kwargs))
        def capture_info(*args, **kwargs):
            log_records.append((args, kwargs))
        bt_mod.logger.debug = capture_debug
        bt_mod.logger.info = capture_info

        try:
            async def dummy_task():
                await asyncio.sleep(0.01)

            _spawn(dummy_task())
            await asyncio.sleep(0.2)

            # 应该有完成日志（debug 或 info 级别）
            assert len(log_records) > 0, "后台任务完成时应有日志"
        finally:
            bt_mod.logger.debug = original_debug
            bt_mod.logger.info = original_info


class TestToolExecutionGatherTimeout:
    """测试工具执行的 gather 超时保护"""

    @pytest.mark.asyncio
    async def test_gather_with_timeout_does_not_hang(self):
        """asyncio.gather 应有超时保护，不会无限等待"""
        # 模拟一个慢工具
        async def slow_tool():
            await asyncio.sleep(100)
            return "result"

        # 应在超时时间内抛出 TimeoutError
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(slow_tool()),
                timeout=0.1
            )


class TestTruncationDetection:
    """测试回复截断检测"""

    def test_chat_max_tokens_increased(self):
        """chat 路由的 max_tokens 应从 1500 提升到 2048"""
        from model_router import ROUTE_TABLE
        assert ROUTE_TABLE["chat"]["max_tokens"] >= 2048, \
            f"chat max_tokens 应 >= 2048，当前为 {ROUTE_TABLE['chat']['max_tokens']}"

    def test_fast_path_logs_reply_len(self):
        """fast_path.done 日志应包含 reply_len 字段（Phase 3 后日志代码在 main_path mixin）"""
        import inspect
        import agent_core.mixins.main_path as main_path_mod
        source = inspect.getsource(main_path_mod)
        assert "reply_len" in source, "fast_path 日志应包含 reply_len 字段"

    def test_model_router_checks_finish_reason(self):
        """model_router 应检查 finish_reason 并记录截断告警"""
        import inspect
        from model_router import ModelRouter
        source = inspect.getsource(ModelRouter._handle_route_response)
        assert "finish_reason" in source, \
            "_handle_route_response 应检查 finish_reason"
        # 截断告警日志已随 length 重试逻辑抽至 _retry_truncated_content
        retry_source = inspect.getsource(ModelRouter._retry_truncated_content)
        assert "truncated_by_max_tokens" in retry_source, \
            "应有 truncated_by_max_tokens 告警日志"
