"""BeliefRouter bug 修复测试 — decide() 应调用 select_agent()。"""
import os
from unittest.mock import MagicMock, patch

from core.router_engine import RouterEngine


def test_decide_calls_select_agent_not_decide():
    """RouterEngine.decide() 应调用 BeliefRouter.select_agent()，而非不存在的 decide()。"""
    belief_router = MagicMock()
    belief_router.select_agent = MagicMock(return_value="xiaolang")

    engine = RouterEngine(belief_router=belief_router)
    with patch.dict(os.environ, {"ROUTER_ENGINE": "new"}):
        engine._use_belief = True
        # 传入不会匹配 @mention/否定/自指/语音/关键词的输入
        decision = engine.decide("帮我写个Python函数", user_id="test")

    belief_router.select_agent.assert_called_once()
    assert "xiaolang" in decision.agent_names


def test_decide_belief_fallback_on_exception():
    """BeliefRouter.select_agent() 异常时降级到关键词匹配。"""
    belief_router = MagicMock()
    belief_router.select_agent = MagicMock(side_effect=RuntimeError("broken"))

    engine = RouterEngine(belief_router=belief_router)
    with patch.dict(os.environ, {"ROUTER_ENGINE": "new"}):
        engine._use_belief = True
        # 输入包含"小狼"关键词，确保降级后匹配关键词模式
        decision = engine.decide("让小狼帮我写代码", user_id="test")

    assert "xiaolang" in decision.agent_names
