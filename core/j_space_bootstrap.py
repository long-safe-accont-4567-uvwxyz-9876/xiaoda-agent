# core/j_space_bootstrap.py
"""
J-Space 架构优化启动初始化。

在 Agent 启动时初始化:
1. BehavioralSignalStream 全局实例
2. DirectionRegistry 加载/初始化预注册方向
3. InterventionLoop 注册默认规则
"""
from loguru import logger
from config import ENABLE_J_SPACE_HOOKS, DIRECTION_REGISTRY_PATH, SIGNAL_STREAM_MAX_HISTORY
from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionVector, DirectionRegistry
from core.intervention_loop import InterventionRule, InterventionLoop
from agent_core.structured_blackboard import StructuredBlackboard
from core.enhanced_router import EnhancedBeliefRouter


def _create_default_directions() -> list[DirectionVector]:
    """预注册方向"""
    return [
        DirectionVector("helpfulness", {"prompt": 0.3, "route": 0.2}, "manual"),
        DirectionVector("safety", {"prompt": 0.5, "tool": -0.3}, "manual"),
        DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual"),
        DirectionVector("focused", {"prompt": 0.4, "route": 0.3}, "manual"),
    ]


def _create_default_rules() -> list[InterventionRule]:
    """默认干预规则"""
    return [
        InterventionRule("cognitive_load", threshold=0.8, direction_name="calm",
                         alpha=0.4, mode="projected", cooldown=30.0),
        InterventionRule("health", threshold=0.3, direction_name="focused",
                         alpha=0.5, mode="uniform", cooldown=60.0),
    ]


_signal_stream: BehavioralSignalStream | None = None
_direction_registry: DirectionRegistry | None = None
_intervention_loop: InterventionLoop | None = None
_structured_blackboard: StructuredBlackboard | None = None
_enhanced_router: EnhancedBeliefRouter | None = None


def _wire_hooks() -> None:
    """将 J-Space 组件注入到各 Hook 模块的全局变量。"""
    try:
        import core.agent_introspection as _ai
        _ai._signal_stream = _signal_stream
    except Exception as e:
        logger.warning(f"j_space.wire_failed agent_introspection: {e}")
    try:
        import core.behavioral_health as _bh
        _bh._signal_stream = _signal_stream
    except Exception as e:
        logger.warning(f"j_space.wire_failed behavioral_health: {e}")
    try:
        import agent_dispatcher as _ad
        _ad._signal_stream = _signal_stream
        _ad._intervention_loop = _intervention_loop
    except Exception as e:
        logger.warning(f"j_space.wire_failed agent_dispatcher: {e}")
    try:
        import core.degradation_strategy as _ds
        _ds._signal_stream = _signal_stream
    except Exception as e:
        logger.warning(f"j_space.wire_failed degradation_strategy: {e}")
    try:
        import memory.cognitive_memory as _cm
        _cm._structured_blackboard = _structured_blackboard
    except Exception as e:
        logger.warning(f"j_space.wire_failed cognitive_memory: {e}")
    try:
        import belief_router as _br
        _br._enhanced_router = _enhanced_router
    except Exception as e:
        logger.warning(f"j_space.wire_failed belief_router: {e}")


def init_j_space() -> None:
    """初始化 J-Space 组件"""
    global _signal_stream, _direction_registry, _intervention_loop
    global _structured_blackboard, _enhanced_router

    if not ENABLE_J_SPACE_HOOKS:
        logger.info("j_space.disabled by config")
        return

    try:
        _signal_stream = BehavioralSignalStream(max_history=SIGNAL_STREAM_MAX_HISTORY)
        _direction_registry = DirectionRegistry(storage_path=DIRECTION_REGISTRY_PATH)

        # 如果注册表为空，初始化预注册方向
        if not _direction_registry.list_directions():
            for direction in _create_default_directions():
                _direction_registry.register(direction)
            logger.info(f"j_space.directions_registered count={len(_create_default_directions())}")
        else:
            logger.info(f"j_space.directions_loaded count={len(_direction_registry.list_directions())}")

        _intervention_loop = InterventionLoop(_signal_stream, _direction_registry)
        for rule in _create_default_rules():
            _intervention_loop.register_rule(rule)
        logger.info(f"j_space.rules_registered count={len(_create_default_rules())}")

        _structured_blackboard = StructuredBlackboard()
        # EnhancedBeliefRouter wraps the base BeliefRouter
        try:
            from belief_router import BeliefRouter
            _base_router = BeliefRouter()
            _enhanced_router = EnhancedBeliefRouter(
                base_router=_base_router,
                direction_registry=_direction_registry,
                signal_stream=_signal_stream,
            )
        except Exception as e:
            logger.warning(f"j_space.enhanced_router_init_failed: {e}")
            _enhanced_router = None

        _wire_hooks()

        logger.info("j_space.initialized")
    except Exception as e:
        logger.warning(f"j_space.init_failed (non-blocking): {e}")
        _signal_stream = None
        _direction_registry = None
        _intervention_loop = None
        _structured_blackboard = None
        _enhanced_router = None


def get_signal_stream() -> BehavioralSignalStream | None:
    return _signal_stream


def get_direction_registry() -> DirectionRegistry | None:
    return _direction_registry


def get_intervention_loop() -> InterventionLoop | None:
    return _intervention_loop


def get_structured_blackboard() -> StructuredBlackboard | None:
    return _structured_blackboard


def get_enhanced_router() -> EnhancedBeliefRouter | None:
    return _enhanced_router
