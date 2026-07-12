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


def init_j_space() -> None:
    """初始化 J-Space 组件"""
    global _signal_stream, _direction_registry, _intervention_loop

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

        logger.info("j_space.initialized")
    except Exception as e:
        logger.warning(f"j_space.init_failed (non-blocking): {e}")
        _signal_stream = None
        _direction_registry = None
        _intervention_loop = None


def get_signal_stream() -> BehavioralSignalStream | None:
    return _signal_stream


def get_direction_registry() -> DirectionRegistry | None:
    return _direction_registry


def get_intervention_loop() -> InterventionLoop | None:
    return _intervention_loop
