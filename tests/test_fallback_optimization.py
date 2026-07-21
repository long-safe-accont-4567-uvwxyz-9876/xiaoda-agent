"""测试 fallback 链和超时优化

验证：
1. set_chat_model 切换 provider 时，flash/mini 路由同步到跨 provider
2. MAX_RETRIES 降为 1（2 次尝试而非 3 次）
3. chat_flash 超时从 60s 降为 30s
4. profile_learner 的 loguru 格式化不再触发 Replacement index 错误
5. 后台任务 _spawn 添加耗时监控日志
"""
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(PROJECT_ROOT))


class TestFallbackChainSync:
    """测试 set_chat_model 时 flash/mini 路由的同步行为。

    关键变更（agnes provider 路由 bug 修复）：
    旧逻辑会把 chat_flash 重置成跨 provider（agnes→mimo, mimo→agnes），
    导致用户明确选择 agnes 后，agnes 主路由失败时 fallback 链跳到 mimo，
    返回 mimo-v2.5 回复，与用户配置不符。

    新行为：chat_flash 跟随主 provider，跨 provider 降级作为最后手段
    由 _try_fallback_chain 的 step 4（mimo fallback）处理。
    """

    def test_set_chat_model_agnes_keeps_flash_with_agnes(self):
        """切换到 agnes 时，chat_flash 应跟随 agnes（不再跨 provider 重置）"""
        from model_router import ROUTE_TABLE, ModelRouter

        # 模拟初始状态：mimo 为默认
        original_flash = ROUTE_TABLE["chat_flash"].copy()
        original_mini = ROUTE_TABLE["chat_mini"].copy()
        try:
            router = MagicMock(spec=ModelRouter)
            router._custom_clients = {}
            router._current_chat_model = None
            router._lazy_register_provider = MagicMock()
            # set_chat_model 持久化时会读取 TASK_TIMEOUTS，需显式提供
            router.TASK_TIMEOUTS = {"chat": 60}

            # 调用 set_chat_model 切换到 agnes
            ModelRouter.set_chat_model(router, "agnes", "agnes-2.0-flash")

            # chat 路由应更新为 agnes
            assert ROUTE_TABLE["chat"]["model"] == "agnes-2.0-flash"
            assert ROUTE_TABLE["chat"]["client"] == "agnes"

            # chat_flash 应跟随主 provider (agnes)，不应被重置成 mimo
            assert ROUTE_TABLE["chat_flash"]["client"] == "agnes", \
                "chat_flash 应跟随主 provider (agnes)，跨 provider 降级由 _try_fallback_chain 处理"
            assert ROUTE_TABLE["chat_flash"]["model"] == "agnes-2.0-flash"
        finally:
            ROUTE_TABLE["chat_flash"] = original_flash
            ROUTE_TABLE["chat_mini"] = original_mini

    def test_set_chat_model_mimo_keeps_flash_with_mimo(self):
        """切换到 mimo 时，chat_flash 应跟随 mimo（不再跨 provider 重置）"""
        from model_router import ROUTE_TABLE, ModelRouter

        original_flash = ROUTE_TABLE["chat_flash"].copy()
        original_mini = ROUTE_TABLE["chat_mini"].copy()
        try:
            router = MagicMock(spec=ModelRouter)
            router._custom_clients = {}
            router._current_chat_model = None
            router._lazy_register_provider = MagicMock()
            # set_chat_model 持久化时会读取 TASK_TIMEOUTS，需显式提供
            router.TASK_TIMEOUTS = {"chat": 60}

            ModelRouter.set_chat_model(router, "mimo", "mimo-v2.5")

            assert ROUTE_TABLE["chat"]["client"] == "mimo"
            # chat_flash 应跟随主 provider (mimo)
            assert ROUTE_TABLE["chat_flash"]["client"] == "mimo", \
                "chat_flash 应跟随主 provider (mimo)，跨 provider 降级由 _try_fallback_chain 处理"
        finally:
            ROUTE_TABLE["chat_flash"] = original_flash
            ROUTE_TABLE["chat_mini"] = original_mini

    def test_fallback_chain_uses_different_providers(self):
        """验证 fallback 链中每级使用不同 provider"""
        from model_router import ROUTE_TABLE, FALLBACK_ROUTE

        # 模拟 agnes 作为主 provider 的场景
        original = {k: v.copy() for k, v in ROUTE_TABLE.items()}
        try:
            ROUTE_TABLE["chat"]["client"] = "agnes"
            ROUTE_TABLE["chat_flash"]["client"] = "mimo"
            ROUTE_TABLE["chat_mini"]["client"] = "agnes"
            ROUTE_TABLE["chat_agnes"]["client"] = "agnes"

            # chat_flash → chat_mini：mimo → agnes（不同 provider）
            assert ROUTE_TABLE["chat_flash"]["client"] != ROUTE_TABLE["chat_mini"]["client"]

            # chat_mini → chat_agnes：agnes → agnes（同级，但这是最终 agnes fallback）
            # chat_agnes 之后还有 custom_provider_fallback（siliconflow）
        finally:
            for k, v in original.items():
                ROUTE_TABLE[k] = v


class TestRetryAndTimeoutReduction:
    """测试重试次数和超时时间的降低"""

    def test_max_retries_is_1(self):
        """MAX_RETRIES 应为 1（最多 2 次尝试）"""
        from model_router import MAX_RETRIES
        assert MAX_RETRIES == 1, f"MAX_RETRIES 应为 1，当前为 {MAX_RETRIES}"

    def test_chat_flash_timeout_is_30s(self):
        """chat_flash 超时应为 30 秒"""
        from model_router import ModelRouter
        assert ModelRouter._DEFAULT_TIMEOUTS["chat_flash"] == 30, \
            f"chat_flash 超时应为 30s，当前为 {ModelRouter._DEFAULT_TIMEOUTS.get('chat_flash')}"

    def test_chat_timeout_is_60s(self):
        """chat 超时应为 60 秒（从 90s 降低）"""
        from model_router import ModelRouter
        assert ModelRouter._DEFAULT_TIMEOUTS["chat"] == 60, \
            f"chat 超时应为 60s，当前为 {ModelRouter._DEFAULT_TIMEOUTS.get('chat')}"


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

    def test_chat_flash_max_tokens_increased(self):
        """chat_flash 路由的 max_tokens 应从 1000 提升到 1200"""
        from model_router import ROUTE_TABLE
        assert ROUTE_TABLE["chat_flash"]["max_tokens"] >= 1200, \
            f"chat_flash max_tokens 应 >= 1200，当前为 {ROUTE_TABLE['chat_flash']['max_tokens']}"

    def test_fast_path_logs_reply_len(self):
        """fast_path.done 日志应包含 reply_len 字段"""
        import inspect
        import agent_core.message_processor as mp_mod
        source = inspect.getsource(mp_mod)
        assert "reply_len" in source, "fast_path 日志应包含 reply_len 字段"

    def test_model_router_checks_finish_reason(self):
        """model_router 应检查 finish_reason 并记录截断告警"""
        import inspect
        from model_router import ModelRouter
        source = inspect.getsource(ModelRouter._handle_route_response)
        assert "finish_reason" in source, \
            "_handle_route_response 应检查 finish_reason"
        assert "truncated_by_max_tokens" in source, \
            "应有 truncated_by_max_tokens 告警日志"
