"""流体记忆系统 — 兼容层（已迁移至 FSRS-DSR 模型）

原 FluidMemory 类保留为兼容层，内部委托给 FSRSModel。
新代码应直接使用 memory.fsrs_model.FSRSModel。
"""
import time
import warnings

from loguru import logger

from memory.fsrs_model import (
    FSRSModel,
    MemoryState,
    MemoryPhase,
    FORGET_THRESHOLD,
    DREAM_THRESHOLD,
    BUFFER_DAYS,
    S_INIT,
)


class FluidMemory:
    """流体记忆 — 兼容层（委托 FSRS-DSR）

    保留原接口签名以兼容旧调用方。
    内部使用 FSRSModel 计算 Retrievability。
    """

    STABILITY_BASE_DAYS = S_INIT
    STABILITY_PER_ACCESS = 14.0
    BOOST_PER_ACCESS = 0.15
    GRACE_DAYS = BUFFER_DAYS
    PERMANENT_ACCESS_THRESHOLD = 5
    WEIGHT_THRESHOLD = 0.1
    FORGET_THRESHOLD = FORGET_THRESHOLD
    DREAM_THRESHOLD = DREAM_THRESHOLD

    def __init__(self) -> None:
        self._fsrs = FSRSModel()

    def score(self, similarity: float, created_at: float,
              access_count: int = 0, peak_weight: float = 1.0,
              fsrs_state: MemoryState | None = None) -> float:
        """计算综合记忆分数（兼容接口，委托 FSRS-DSR）"""
        now = time.time()
        if fsrs_state is not None:
            R = fsrs_state.retrievability(now)
            if peak_weight != 1.0:
                warnings.warn(
                    "peak_weight is ignored when FSRS state is available; "
                    "FSRS R(t) already incorporates decay. "
                    "peak_weight will be removed in a future version.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            return similarity * R

        # ⚠️ 旧路径：无 FSRS 状态时使用线性公式估算 stability。
        # 此公式与 FSRS reinforce 后的 stability 有差异，仅作为 fallback。
        # 新代码应始终传入 fsrs_state 参数。
        stability = S_INIT + access_count * self.STABILITY_PER_ACCESS
        state = MemoryState(
            stability=min(stability, 300.0),
            phase=MemoryPhase.BUFFER,
            last_review=created_at,
            created_at=created_at,
            reinforcement_count=access_count,
        )
        if access_count >= self.PERMANENT_ACCESS_THRESHOLD:
            state = MemoryState(
                stability=stability,
                phase=MemoryPhase.PERMANENT,
                last_review=created_at,
                created_at=created_at,
                reinforcement_count=access_count,
            )
        elif (now - created_at) / 86400.0 > BUFFER_DAYS:
            state = MemoryState(
                stability=stability,
                phase=MemoryPhase.REINFORCED if access_count > 0 else MemoryPhase.DECAY,
                last_review=created_at,
                created_at=created_at,
                reinforcement_count=access_count,
            )
        R = state.retrievability(now)
        return similarity * peak_weight * R

    def is_permanent(self, access_count: int,
                     fsrs_state: MemoryState | None = None) -> bool:
        """判断记忆是否已达永久状态。优先使用 FSRS transition 判定。"""
        if fsrs_state is not None:
            new_phase = fsrs_state.transition(time.time())
            return new_phase == MemoryPhase.PERMANENT
        return access_count >= self.PERMANENT_ACCESS_THRESHOLD

    def should_filter(self, score: float) -> bool:
        return score < self.FORGET_THRESHOLD

    def should_archive(self, score: float) -> bool:
        return score < self.DREAM_THRESHOLD