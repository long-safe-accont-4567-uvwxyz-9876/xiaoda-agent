"""N9 修复回归测试：recall 工具超时降级与 tool_executor 不再误重试。

背景（2026-07-25 17:48-17:51 生产事故）：
- 用户问"礼物是什么？"，LLM 调用 recall 工具查记忆
- recall 调用 retrieve_memories 缺乏自身超时，依赖 tool_executor 60s 兜底
- 60s 超时后错误字符串含 "[timeout]"，被 _is_retryable_error 中的 "timeout"
  关键词匹配，触发自动重试（MAX_RETRIES=2）
- 单次调用最坏阻塞 60s × 3 = 180s 才进入降级
- 降级路径 skip_memory_degraded_reply 让 LLM 失去记忆上下文，加上截断重试
  机制，雪崩成 4 段人格切换的混乱回复（事故 ID 2112）

修复：
1. recall 函数内 asyncio.wait_for(timeout=10) 包裹 retrieve_memories
2. 超时返回 ToolResult.ok(...) 而非 fail，让 LLM 告诉用户"暂时想不起来"
3. tool_executor TimeoutError 错误改中文，不含 "timeout" 关键词，不触发重试
4. TOOL_TIMEOUTS 给 recall 单独配置 15s 短超时作为兜底
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.memory_tool import recall
from tool_engine.tool_executor import ToolExecutor


class TestRecallTimeoutDegradation:
    """验证 recall 工具超时降级行为。"""

    @pytest.mark.asyncio
    async def test_recall_timeout_returns_ok_not_fail(self):
        """recall 内部超时应返回 ToolResult.ok 而非 fail。

        关键属性：LLM 收到的是"记忆暂时不可用"提示，而非错误。
        这样 LLM 会告诉用户"暂时想不起来"，而非进入 is_degraded_reply 路径。
        """
        # 模拟 retrieve_memories 被 asyncio.wait_for 超时取消后的行为：
        # 直接抛出 asyncio.TimeoutError（与 wait_for 超时后抛出的异常一致）。
        # 这样测试无需真实等待 10s 超时（<0.1s 完成），专注验证降级逻辑。
        async def _raises_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        mock_mm = MagicMock()
        mock_mm.retrieve_memories = _raises_timeout

        with patch("tools.memory_tool._memory_manager", mock_mm):
            result = await recall(query="礼物是什么？", top_k=8)

        # 关键断言：超时返回 ok 而非 fail
        assert result.success is True, "recall 超时应返回 ok（让 LLM 知道是记忆系统问题）"
        # 提示内容应让 LLM 知道告诉用户"暂时想不起来"
        data = result.data or ""
        assert "暂时" in data or "想不起" in data, \
            f"超时提示应让 LLM 告诉用户暂时想不起来，实际：{data}"

    @pytest.mark.asyncio
    async def test_recall_timeout_does_not_leak_timeout_keyword(self):
        """recall 超时返回的字符串不应含 'timeout' 关键词。

        避免 LLM 把英文错误信息泄漏给用户。
        """
        async def _raises_timeout(*args, **kwargs):
            raise asyncio.TimeoutError()

        mock_mm = MagicMock()
        mock_mm.retrieve_memories = _raises_timeout

        with patch("tools.memory_tool._memory_manager", mock_mm):
            result = await recall(query="测试", top_k=5)

        # 英文 "timeout" 关键词不应泄漏到 LLM 可见的内容
        data = (result.data or "").lower()
        assert "timeout" not in data, \
            f"超时提示不应含英文 'timeout' 关键词，实际：{data}"

    @pytest.mark.asyncio
    async def test_recall_normal_path_unaffected(self):
        """recall 正常路径（未超时）应原样返回检索结果。"""
        mock_results = [
            {"id": 1, "summary": "测试记忆", "score": 0.9, "importance": 0.8,
             "timestamp": 1780000000, "is_raw": 1}
        ]
        mock_mm = MagicMock()
        mock_mm.retrieve_memories = AsyncMock(return_value=mock_results)

        with patch("tools.memory_tool._memory_manager", mock_mm):
            result = await recall(query="测试", top_k=5)

        assert result.success is True
        assert "测试记忆" in (result.data or "")

    @pytest.mark.asyncio
    async def test_recall_empty_results_unaffected(self):
        """recall 检索返回空时仍返回 '没有找到相关记忆'。"""
        mock_mm = MagicMock()
        mock_mm.retrieve_memories = AsyncMock(return_value=[])

        with patch("tools.memory_tool._memory_manager", mock_mm):
            result = await recall(query="不存在的记忆", top_k=5)

        assert result.success is True
        assert "没有找到相关记忆" in (result.data or "")


class TestToolExecutorTimeoutNoRetry:
    """验证 tool_executor 超时不再触发自动重试。"""

    def test_tool_execution_timeout_error_does_not_match_retryable(self):
        """工具执行超时错误字符串不应匹配 _is_retryable_error。

        生产事故根因：原错误含 "[timeout]" 被 "timeout" 关键词匹配，
        触发 MAX_RETRIES=2 自动重试，导致 60s × 3 = 180s 阻塞。
        """
        executor = ToolExecutor()
        # 模拟 N9 修复后的超时错误字符串
        timeout_error = "工具「recall」执行超时（15s），请稍后再试"
        assert not executor._is_retryable_error(timeout_error), \
            "工具执行超时不应触发自动重试（避免 60s × 3 雪崩）"

    def test_connection_timeout_still_retryable(self):
        """网络连接超时仍应可重试（保留 'connection' 关键词匹配）。

        修复必须精确：只让工具执行超时不重试，不影响网络瞬时错误重试。
        """
        executor = ToolExecutor()
        # httpx.ConnectTimeout 等错误字符串
        connection_errors = [
            "ConnectionError: timed out",
            "ConnectTimeout: Failed to establish connection",
            "connection reset by peer",
            "RemoteProtocolError: connection closed",
        ]
        for err in connection_errors:
            assert executor._is_retryable_error(err), \
                f"网络连接错误应可重试：{err}"

    def test_rate_limit_still_retryable(self):
        """限流错误仍应可重试（保留 503/429/ratelimit 关键词）。"""
        executor = ToolExecutor()
        rate_limit_errors = [
            "RateLimitError: 429 Too Many Requests",
            "Service Unavailable: 503",
            "Bad Gateway: 502",
        ]
        for err in rate_limit_errors:
            assert executor._is_retryable_error(err), \
                f"限流错误应可重试：{err}"

    def test_old_timeout_keyword_still_matches_as_reminder(self):
        """旧错误字符串 '[timeout]' 仍会匹配 _is_retryable_error。

        回归提醒：ToolExecutor 已不再产生含 '[timeout]' 的错误字符串（改为
        中文表述），但 _is_retryable_error 仍保留 "timeout" 关键词匹配以
        处理其他来源的超时错误。此测试记录此事实：如果有人改回 '[timeout]'
        字样，会重新触发自动重试导致事故复发。
        """
        executor = ToolExecutor()
        # 旧错误字符串（生产事故根因）
        old_error = "那边有点慢呢……等会儿再试试好不好？ [timeout]"
        # 旧字符串仍会匹配 "timeout" 关键词，但 ToolExecutor 不再产生此字符串
        # 此测试记录此事实：如果重新引入 [timeout] 字样，会重新触发事故
        assert executor._is_retryable_error(old_error), \
            "旧 [timeout] 字符串仍匹配（提醒：不要重新使用此错误字符串）"

    def test_recall_timeout_config_is_short(self):
        """TOOL_TIMEOUTS['recall'] 应配置为 15s（而非默认 60s）。

        短超时确保即使 recall 内部 10s 超时未触发（事件循环阻塞），
        tool_executor 也能在 15s 后超时（而非 60s），减少用户等待。
        """
        assert ToolExecutor.TOOL_TIMEOUTS["recall"] == 15, \
            "recall 应配置 15s 短超时作为兜底"

    def test_other_tools_timeout_unchanged(self):
        """其他工具超时配置不受影响。"""
        expected = {
            "agnes_video_generate": 240,
            "document_reader": 120,
            "web_browse": 30,
            "multi_search": 25,
            "web_search": 15,
            "wolfram_query": 20,
            "python_executor": 30,
            "shell_command": 20,
            "delegate_task": 60,
            "default": 60.0,
        }
        for tool, expected_timeout in expected.items():
            assert ToolExecutor.TOOL_TIMEOUTS[tool] == expected_timeout, \
                f"{tool} 超时配置不应改变：期望 {expected_timeout}"


class TestProductionRegressionScenario:
    """生产事故（2026-07-25 17:48-17:51）回归测试。"""

    def test_no_snowball_chain(self):
        """验证修复后不再产生 180s 雪崩链。

        事故时间线：
        - 17:48:01 调用 recall
        - 17:49:01 60s 超时 → 触发重试
        - 17:50:01 60s 超时 → 触发重试
        - 17:51:02 降级模式触发（共 180s+）

        修复后预期：
        - recall 内部 10s 超时 → 立即返回 ok 降级提示（10s 内）
        - 或 tool_executor 15s 兜底超时 → 中文错误不触发重试（15s 内）
        - 总耗时 <= 15s（原 180s+）
        """
        # 验证超时配置
        assert ToolExecutor.TOOL_TIMEOUTS["recall"] == 15

        # 验证超时错误不触发重试
        executor = ToolExecutor()
        timeout_error = "工具「recall」执行超时（15s），请稍后再试"
        assert not executor._is_retryable_error(timeout_error)

        # 验证最大重试次数不变（影响其他可重试错误）
        assert ToolExecutor.MAX_RETRIES == 2

        # 关键结论：即使 recall 内部 10s 超时未触发，
        # tool_executor 15s 兜底也只触发一次（不重试），总耗时 <= 15s
