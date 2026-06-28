"""行为健康评分 BHS(a) — MARIA VITAL

5维加权: 目标完成率 + 重复失败率 + 循环信号 + 角色偏离 + 低质量输出率
检测 zombie 状态: 端口在监听但响应全 error
"""
import time
from dataclasses import dataclass, field
from collections import deque
from loguru import logger


@dataclass
class BehavioralMetrics:
    """行为指标 — 从实际运行轨迹采集"""

    goal_completion_rate: float = 1.0
    failure_repeat_rate: float = 0.0
    loop_signal: float = 0.0
    role_deviation: float = 0.0
    low_quality_rate: float = 0.0

    W_GOAL = 0.30
    W_NO_REPEAT = 0.25
    W_NO_LOOP = 0.20
    W_NO_DEVIATION = 0.15
    W_NO_LOW_Q = 0.10

    @property
    def behavioral_health_score(self) -> float:
        return max(0, min(1, (
            self.W_GOAL * self.goal_completion_rate
            + self.W_NO_REPEAT * (1 - self.failure_repeat_rate)
            + self.W_NO_LOOP * (1 - self.loop_signal)
            + self.W_NO_DEVIATION * (1 - self.role_deviation)
            + self.W_NO_LOW_Q * (1 - self.low_quality_rate)
        )))

    @property
    def health_status(self) -> str:
        bhs = self.behavioral_health_score
        if bhs >= 0.9: return "Optimal"
        if bhs >= 0.7: return "Healthy"
        if bhs >= 0.5: return "Degraded"
        if bhs >= 0.3: return "Critical"
        return "Failed (zombie?)"


class ZombieDetector:
    """Zombie 状态检测 — 端口在监听但行为异常"""

    def detect(self, metrics: BehavioralMetrics) -> list[str]:
        alerts = []
        if metrics.goal_completion_rate < 0.1:
            alerts.append(f"Zombie检测: 目标完成率<10%, Agent可能在空转")
        if metrics.loop_signal > 0.5:
            alerts.append(f"循环检测: 循环信号={metrics.loop_signal:.2f}, 可能陷入死循环")
        if metrics.role_deviation > 0.3:
            alerts.append(f"角色偏离: 偏离度={metrics.role_deviation:.2f}, 可能被prompt injection劫持")
        for a in alerts:
            logger.critical(a)
        return alerts


class BehavioralHealthMonitor:
    """行为健康监控器"""

    def __init__(self):
        self._metrics = BehavioralMetrics()
        self._tool_history: deque = deque(maxlen=100)
        self._zombie_detector = ZombieDetector()

    def record_tool_call(self, tool_name: str, success: bool):
        """记录工具调用"""
        self._tool_history.append((tool_name, success, time.time()))
        self._update_metrics()

    def record_user_correction(self):
        """记录用户纠正"""
        self._metrics.low_quality_rate = min(1.0, self._metrics.low_quality_rate + 0.05)

    def _update_metrics(self):
        """更新指标"""
        if not self._tool_history:
            return
        recent = list(self._tool_history)[-20:]
        successes = sum(1 for _, s, _ in recent if s)
        self._metrics.goal_completion_rate = successes / len(recent)

        # 检测循环: A→B→A→B 模式
        if len(recent) >= 4:
            tools = [t for t, _, _ in recent]
            loop_count = sum(1 for i in range(len(tools) - 2) if tools[i] == tools[i + 2])
            self._metrics.loop_signal = loop_count / max(1, len(tools) - 2)

    def get_health_report(self) -> dict:
        """获取健康报告"""
        alerts = self._zombie_detector.detect(self._metrics)
        return {
            "behavioral_health_score": round(self._metrics.behavioral_health_score, 3),
            "health_status": self._metrics.health_status,
            "metrics": {
                "goal_completion_rate": round(self._metrics.goal_completion_rate, 3),
                "loop_signal": round(self._metrics.loop_signal, 3),
                "role_deviation": round(self._metrics.role_deviation, 3),
                "low_quality_rate": round(self._metrics.low_quality_rate, 3),
            },
            "alerts": alerts,
        }


_bh_monitor = BehavioralHealthMonitor()


def get_behavioral_health_monitor() -> BehavioralHealthMonitor:
    return _bh_monitor
