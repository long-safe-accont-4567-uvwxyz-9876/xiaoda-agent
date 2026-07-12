# tests/test_enhanced_router.py
import pytest
from unittest.mock import MagicMock, patch
from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry
from core.enhanced_router import EnhancedBeliefRouter


@pytest.fixture
def mock_base_router():
    router = MagicMock()
    router.VALID_AGENTS = ["xiaoda", "xiaolang", "xiaoke", "xiaolian"]
    router._beliefs = {
        "xiaoda": MagicMock(sample=MagicMock(return_value=0.5)),
        "xiaolang": MagicMock(sample=MagicMock(return_value=0.4)),
        "xiaoke": MagicMock(sample=MagicMock(return_value=0.6)),
        "xiaolian": MagicMock(sample=MagicMock(return_value=0.3)),
    }
    # sample_agent 是公共接口，返回 float
    _sample_map = {"xiaoda": 0.5, "xiaolang": 0.4, "xiaoke": 0.6, "xiaolian": 0.3}
    router.sample_agent = MagicMock(side_effect=lambda name: _sample_map.get(name, 0.5))
    router.update_belief = MagicMock()
    return router


@pytest.mark.asyncio
async def test_select_agent_basic(mock_base_router):
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream)
    selected = enhanced.select_agent()
    assert selected in mock_base_router.VALID_AGENTS


@pytest.mark.asyncio
async def test_select_agent_with_exclude(mock_base_router):
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream)
    selected = enhanced.select_agent(exclude={"xiaoda", "xiaoke", "xiaolian"})
    assert selected == "xiaolang"


@pytest.mark.asyncio
async def test_select_agent_empty_candidates(mock_base_router):
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream)
    selected = enhanced.select_agent(exclude=set(mock_base_router.VALID_AGENTS))
    assert selected == "xiaoda"  # fallback


@pytest.mark.asyncio
async def test_select_agent_with_direction_hint(mock_base_router):
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    registry.register(DirectionVector("route_security", {"route": 0.5}, "manual"))
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream)
    selected = enhanced.select_agent(task_type="security", direction_hint="route_security")
    assert selected in mock_base_router.VALID_AGENTS


@pytest.mark.asyncio
async def test_select_agent_signal_adjustment(mock_base_router):
    stream = BehavioralSignalStream()
    # 给 xiaoke 高成功率信号
    await stream.emit("agent_xiaoke_success", 0.9, "test")
    await stream.emit("agent_xiaoke_success", 0.95, "test")
    registry = DirectionRegistry()
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream,
                                    direction_weight=0.0, signal_weight=0.5)
    selected = enhanced.select_agent()
    # xiaoke 的 thompson=0.6 + signal_weight*0.925 应该最高
    assert selected == "xiaoke"


@pytest.mark.asyncio
async def test_update_belief_delegates(mock_base_router):
    stream = BehavioralSignalStream()
    registry = DirectionRegistry()
    enhanced = EnhancedBeliefRouter(mock_base_router, registry, stream)
    enhanced.update_belief("xiaoda", True)
    mock_base_router.update_belief.assert_called_once_with("xiaoda", True)
