"""
增强型路由器 — 在 Thompson Sampling 基础上叠加方向偏置。

对齐:
- belief_router.py: BeliefRouter 的 Thompson Sampling (基础)
- ACT/generate_directions_q_wise.py: 按问题类型生成方向 (q-wise direction)
- repe/rep_readers.py: PCARepReader 的方向识别
- reprobe/steerer.py: Steerer 的方向应用

路由公式:
    score(agent) = thompson_sample(agent)
                 + alpha * direction_bias(task_type, agent)
                 + beta * signal_adjustment(agent, recent_signals)
"""
from loguru import logger

from core.behavioral_signal import BehavioralSignalStream
from core.behavioral_direction import DirectionRegistry
from config import AGENT_TASK_MAP


class EnhancedBeliefRouter:
    """增强型路由器 — 对齐 ACT q-wise direction + RepE concept direction。"""

    def __init__(
        self,
        base_router,
        direction_registry: DirectionRegistry,
        signal_stream: BehavioralSignalStream,
        direction_weight: float = 0.3,
        signal_weight: float = 0.2,
    ):
        self._base = base_router
        self._registry = direction_registry
        self._stream = signal_stream
        self._direction_weight = direction_weight
        self._signal_weight = signal_weight

    def select_agent(
        self,
        task_type: str = "",
        exclude: set[str] | None = None,
        direction_hint: str = "",
    ) -> str:
        """增强型 Agent 选择。"""
        candidates = [a for a in self._base.VALID_AGENTS if a not in (exclude or set())]
        if not candidates:
            return "xiaoda"

        # 1. Thompson Sampling 基础分
        thompson_scores = {a: self._base.sample_agent(a) for a in candidates}

        # 2. 方向偏置
        direction_scores = {a: 0.0 for a in candidates}
        if task_type or direction_hint:
            direction_key = direction_hint or f"route_{task_type}"
            direction = self._registry.get(direction_key)
            if direction and "route" in direction.dimensions:
                route_bias = direction.dimensions["route"]
                for agent in candidates:
                    match = 1.0 if AGENT_TASK_MAP.get(agent) == task_type else 0.0
                    direction_scores[agent] = route_bias * match

        # 3. 实时信号调整
        signal_scores = {}
        for agent in candidates:
            recent = self._stream.aggregate(f"agent_{agent}_success", "mean_of_means")
            signal_scores[agent] = recent if recent is not None else 0.0

        # 4. 综合评分
        final_scores = {}
        for agent in candidates:
            t = thompson_scores.get(agent, 0.5)
            d = direction_scores.get(agent, 0.0)
            s = signal_scores.get(agent, 0.0)
            final_scores[agent] = t + self._direction_weight * d + self._signal_weight * s

        selected = max(final_scores, key=final_scores.get)
        logger.debug("enhanced_router.selected",
                     final={k: round(v, 3) for k, v in final_scores.items()},
                     selected=selected)
        return selected

    def update_belief(self, agent_name: str, success: bool) -> None:
        """更新信念 — 委托给基础路由器"""
        self._base.update_belief(agent_name, success)