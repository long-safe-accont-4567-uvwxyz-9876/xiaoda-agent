"""I9: 偏好闭环验证 — 单元测试

覆盖:
- check_l1 / check_l2 / check_l3 各层检查
- check_pipeline_flow 管线联动验证
- run_full_check 完整报告
- PreferenceReport.summary 输出
"""
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def validator():
    from core.preference_validator import PreferenceValidator
    return PreferenceValidator()


# ============================================================
# check_l1
# ============================================================

@pytest.mark.asyncio
async def test_check_l1_has_constraints(validator):
    """L1 有约束时 has_data=True"""
    fake_loop = MagicMock()
    fake_loop.get_active_constraints.return_value = ["不要说好的", "用中文回复"]
    with patch("core.learning_loop.get_learning_loop", return_value=fake_loop):
        result = await validator.check_l1()
    assert result.has_data is True
    assert result.injected is True
    assert "2 条约束" in result.detail


@pytest.mark.asyncio
async def test_check_l1_empty(validator):
    """L1 无约束时 has_data=False"""
    fake_loop = MagicMock()
    fake_loop.get_active_constraints.return_value = []
    with patch("core.learning_loop.get_learning_loop", return_value=fake_loop):
        result = await validator.check_l1()
    assert result.has_data is False


@pytest.mark.asyncio
async def test_check_l1_exception(validator):
    """L1 异常时不崩溃, 返回 has_data=False"""
    with patch("core.learning_loop.get_learning_loop",
               side_effect=RuntimeError("init failed")):
        result = await validator.check_l1()
    assert result.has_data is False
    assert "init failed" in result.detail


# ============================================================
# check_l2
# ============================================================

@pytest.mark.asyncio
async def test_check_l2_has_additions(validator):
    """L2 有晋升经验时 has_data=True"""
    fake_mgr = AsyncMock()
    fake_mgr.get_system_prompt_additions.return_value = "[人家学到的重要经验]\n· 不要打断用户"
    result = await validator.check_l2(fake_mgr)
    assert result.has_data is True
    assert "prompt_additions" in result.detail


@pytest.mark.asyncio
async def test_check_l2_empty(validator):
    """L2 无晋升经验时 has_data=False"""
    fake_mgr = AsyncMock()
    fake_mgr.get_system_prompt_additions.return_value = ""
    result = await validator.check_l2(fake_mgr)
    assert result.has_data is False


@pytest.mark.asyncio
async def test_check_l2_no_manager(validator):
    """无 learning_manager 时返回 has_data=False"""
    result = await validator.check_l2(None)
    assert result.has_data is False
    assert "无 learning_manager" in result.detail


# ============================================================
# check_l3
# ============================================================

@pytest.mark.asyncio
async def test_check_l3_has_lessons(validator):
    """L3 有教训时 has_data=True"""
    fake_lf = MagicMock()
    fake_lf.get_relevant_lessons.return_value = ["lesson1", "lesson2"]
    fake_lf.get_strategy.return_value = "建议策略"
    with patch("core.learning_feedback.get_learning_feedback_loop",
               return_value=fake_lf):
        result = await validator.check_l3("test query")
    assert result.has_data is True
    assert "2 教训" in result.detail


@pytest.mark.asyncio
async def test_check_l3_empty(validator):
    """L3 无教训时 has_data=False"""
    fake_lf = MagicMock()
    fake_lf.get_relevant_lessons.return_value = []
    fake_lf.get_strategy.return_value = ""
    with patch("core.learning_feedback.get_learning_feedback_loop",
               return_value=fake_lf):
        result = await validator.check_l3()
    assert result.has_data is False


# ============================================================
# check_pipeline_flow
# ============================================================

@pytest.mark.asyncio
async def test_pipeline_flow_success(validator):
    """管线联动成功时返回 True"""
    fake_pipeline = AsyncMock()
    fake_pipeline.process_correction.return_value = "不要加表情"

    fake_loop = MagicMock()
    fake_loop.get_active_constraints.return_value = []
    fake_loop._active_constraints = MagicMock()

    with patch("core.preference_pipeline.get_preference_pipeline",
               return_value=fake_pipeline), \
         patch("core.learning_loop.get_learning_loop", return_value=fake_loop), \
         patch("core.learning_feedback.get_learning_feedback_loop",
               return_value=MagicMock()):
        result = await validator.check_pipeline_flow()
    assert result is True


@pytest.mark.asyncio
async def test_pipeline_flow_no_constraint(validator):
    """管线未提取到约束时返回 False"""
    fake_pipeline = AsyncMock()
    fake_pipeline.process_correction.return_value = None

    fake_loop = MagicMock()
    fake_loop.get_active_constraints.return_value = []

    with patch("core.preference_pipeline.get_preference_pipeline",
               return_value=fake_pipeline), \
         patch("core.learning_loop.get_learning_loop", return_value=fake_loop), \
         patch("core.learning_feedback.get_learning_feedback_loop",
               return_value=MagicMock()):
        result = await validator.check_pipeline_flow()
    assert result is False


# ============================================================
# run_full_check + report
# ============================================================

@pytest.mark.asyncio
async def test_run_full_check_returns_report(validator):
    """完整检查应返回包含 3 层 + 管线状态的报告"""
    fake_loop = MagicMock()
    fake_loop.get_active_constraints.return_value = ["约束1"]

    fake_lf = MagicMock()
    fake_lf.get_relevant_lessons.return_value = ["lesson1"]
    fake_lf.get_strategy.return_value = "策略"

    fake_pipeline = AsyncMock()
    fake_pipeline.process_correction.return_value = "约束"

    with patch("core.learning_loop.get_learning_loop", return_value=fake_loop), \
         patch("core.learning_feedback.get_learning_feedback_loop",
               return_value=fake_lf), \
         patch("core.preference_pipeline.get_preference_pipeline",
               return_value=fake_pipeline):
        report = await validator.run_full_check(
            learning_manager=None, test_query="测试")

    assert len(report.layers) == 3
    assert report.layers[0].layer == "L1 LearningLoop"
    assert report.layers[1].layer == "L2 LearningManager"
    assert report.layers[2].layer == "L3 LearningFeedback"
    # L1 和 L3 有数据, L2 无 manager
    assert report.layers[0].has_data is True
    assert report.layers[1].has_data is False
    assert report.layers[2].has_data is True


def test_report_summary():
    """报告摘要应包含所有层和总体状态"""
    from core.preference_validator import LayerCheck, PreferenceReport
    report = PreferenceReport(
        layers=[
            LayerCheck("L1", True, True, "2 条"),
            LayerCheck("L2", False, False, "无"),
            LayerCheck("L3", True, True, "3 教训"),
        ],
        pipeline_flow_ok=True,
    )
    summary = report.summary()
    assert "偏好闭环验证报告" in summary
    assert "L1" in summary
    assert "L2" in summary
    assert "L3" in summary
    assert "管线联动" in summary
    assert "需关注" in summary  # L2 不健康


def test_report_healthy():
    """所有层健康 + 管线联动正常 → healthy=True"""
    from core.preference_validator import LayerCheck, PreferenceReport
    report = PreferenceReport(
        layers=[
            LayerCheck("L1", True, True),
            LayerCheck("L2", True, True),
            LayerCheck("L3", True, True),
        ],
        pipeline_flow_ok=True,
    )
    assert report.healthy is True
    assert "健康" in report.summary()
