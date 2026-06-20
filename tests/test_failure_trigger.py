"""失败触发器单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.failure_trigger import FailureContext, FailureTrigger


# ── FailureContext 默认值 ──


def test_failure_context_defaults():
    ctx = FailureContext(task="test task")
    assert ctx.task == "test task"
    assert ctx.attempted_steps == []
    assert ctx.error == ""
    assert ctx.error_type == ""
    assert ctx.retry_count == 0
    assert ctx.tool_name == ""


# ── on_failure 策略 ──


@pytest.mark.asyncio
async def test_on_failure_retry():
    ft = FailureTrigger()
    ctx = FailureContext(task="test", error_type="generic", retry_count=0)
    result = await ft.on_failure(ctx)
    assert result["action"] == "retry"


@pytest.mark.asyncio
async def test_on_failure_timeout_strategy():
    ft = FailureTrigger()
    ctx = FailureContext(task="test", error_type="timeout", retry_count=0)
    result = await ft.on_failure(ctx)
    assert result["action"] == "retry"
    assert "超时" in result.get("adjustment", "") or "超时" in result.get("root_cause", "")


@pytest.mark.asyncio
async def test_on_failure_auth_strategy():
    ft = FailureTrigger()
    ctx = FailureContext(task="test", error_type="auth_error", retry_count=0)
    result = await ft.on_failure(ctx)
    assert result["action"] == "report"


@pytest.mark.asyncio
async def test_on_failure_not_found_strategy():
    ft = FailureTrigger()
    ctx = FailureContext(task="test", error_type="not found", retry_count=0)
    result = await ft.on_failure(ctx)
    assert result["action"] == "alternative"


@pytest.mark.asyncio
async def test_on_failure_max_retries_report():
    ft = FailureTrigger()
    ctx = FailureContext(task="test", error_type="generic", retry_count=3)
    result = await ft.on_failure(ctx)
    assert result["action"] == "report"


# ── on_success_after_retry ──


@pytest.mark.asyncio
async def test_on_success_after_retry():
    lm = MagicMock()
    lm.log_error = AsyncMock()
    ft = FailureTrigger(learning_manager=lm)
    ctx = FailureContext(task="test", error="err", error_type="generic")
    strategy = {"adjustment": "retry with fix"}
    await ft.on_success_after_retry(ctx, strategy)
    lm.log_error.assert_awaited_once()


# ── 高频失败提升为规则 ──


@pytest.mark.asyncio
async def test_promote_to_rule():
    lm = MagicMock()
    lm.search_similar_errors = AsyncMock(return_value=[])
    lm.log_error = AsyncMock()
    lm.count_by_error_type = AsyncMock(return_value=3)
    lm.promote_error_pattern = AsyncMock()

    ft = FailureTrigger(learning_manager=lm)
    ctx = FailureContext(task="test", error="err", error_type="generic", retry_count=3)
    result = await ft.on_failure(ctx)
    assert result["action"] == "report"
    lm.promote_error_pattern.assert_awaited_once()


# ── 无 learning_manager 降级 ──


@pytest.mark.asyncio
async def test_no_learning_manager():
    ft = FailureTrigger(learning_manager=None)
    ctx = FailureContext(task="test", error_type="generic", retry_count=0)
    result = await ft.on_failure(ctx)
    # 不抛异常，正常返回
    assert result["action"] in ("retry", "alternative", "report")
