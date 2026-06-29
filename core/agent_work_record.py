"""子 Agent 工作履历 — 记录每次委派的成败/耗时, 供路由器智能调度

I7: 给子 Agent 建立工作履历, 让路由器能基于历史表现做决策。
- record(): 委派完成后记录 (agent, task_type, success, duration)
- get_stats(): 查询某 agent 的历史统计
- get_best_agent(): 从候选中选成功率最高的 (含冷启动保护)

持久化到 DATA_DIR/agent_work_records.json, 格式:
{"records": [{"agent": "nike", "task_type": "frontend", "success": true,
              "duration": 1.2, "timestamp": 1234567890}, ...]}
仅保留最近 500 条 (FIFO)。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from loguru import logger

try:
    from config import DATA_DIR
except Exception:  # pragma: no cover
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

_MAX_RECORDS = 500


class AgentWorkRecord:
    """子 Agent 工作履历记录器"""

    def __init__(self, persist_path: Path | None = None) -> None:
        self._path = persist_path or Path(DATA_DIR) / "agent_work_records.json"
        self._records: list[dict] = []
        self._load()

    def record(self, agent: str, task_type: str, success: bool,
               duration: float = 0.0) -> None:
        """记录一次委派结果"""
        entry = {
            "agent": agent,
            "task_type": task_type,
            "success": success,
            "duration": round(duration, 2),
            "timestamp": time.time(),
        }
        self._records.append(entry)
        if len(self._records) > _MAX_RECORDS:
            self._records = self._records[-_MAX_RECORDS:]
        self._persist()

    def get_stats(self, agent: str, task_type: str | None = None) -> dict:
        """查询 agent 的历史统计 (可按 task_type 过滤)"""
        relevant = [r for r in self._records
                    if r["agent"] == agent
                    and (task_type is None or r["task_type"] == task_type)]
        total = len(relevant)
        if total == 0:
            return {"total": 0, "success_rate": 0.0, "avg_duration": 0.0}
        successes = sum(1 for r in relevant if r["success"])
        durations = [r["duration"] for r in relevant if r["duration"] > 0]
        return {
            "total": total,
            "success_rate": successes / total,
            "avg_duration": sum(durations) / len(durations) if durations else 0.0,
        }

    def get_best_agent(self, candidates: list[str],
                        task_type: str | None = None) -> str | None:
        """从候选 agent 中选成功率最高的 (含冷启动保护)。

        冷启动: 样本数 < 3 的 agent 给予 0.5 基础分, 避免新 agent 被饿死。
        """
        if not candidates:
            return None
        scored: list[tuple[str, float]] = []
        for agent in candidates:
            stats = self.get_stats(agent, task_type)
            if stats["total"] < 3:
                # 冷启动保护: 新 agent 给 0.5 基础分
                scored.append((agent, 0.5))
            else:
                scored.append((agent, stats["success_rate"]))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[0][0] if scored else candidates[0]

    def _persist(self) -> None:
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"records": self._records}, ensure_ascii=False),
                encoding="utf-8")
            import os
            os.replace(tmp, self._path)
        except Exception as e:
            logger.warning("work_record.persist_failed", error=str(e))

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._records = data.get("records", [])[-_MAX_RECORDS:]
        except Exception as e:
            logger.warning("work_record.load_failed", error=str(e))
            self._records = []


_recorder: AgentWorkRecord | None = None


def get_work_recorder() -> AgentWorkRecord:
    """获取全局 AgentWorkRecord 单例."""
    global _recorder
    if _recorder is None:
        _recorder = AgentWorkRecord()
    return _recorder
