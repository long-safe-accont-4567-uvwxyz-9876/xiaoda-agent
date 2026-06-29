"""学习反馈闭环 — 纠正记录 → 模式提取 → 约束注入 → 后续行为改变

约束持久化到 DATA_DIR/active_constraints.json, 重启不丢失。
"""
from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path

from loguru import logger

try:
    from config import DATA_DIR
except Exception:  # pragma: no cover - 配置缺失时退化为项目根目录
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)


class LearningLoop:
    """学习反馈闭环"""

    def __init__(self, persist_path: Path | None = None) -> None:
        self._active_constraints: deque = deque(maxlen=20)
        self._correction_count: int = 0
        if persist_path is not None:
            self._persist_path = Path(persist_path)
        else:
            self._persist_path = Path(DATA_DIR) / "active_constraints.json"
        self._load()

    async def process_correction(self, user_msg: str, bot_reply: str) -> str | None:
        """处理用户纠正, 提取约束"""
        constraint = self._extract_constraint(user_msg, bot_reply)
        if constraint:
            self._active_constraints.append(constraint)
            self._correction_count += 1
            self._persist()
            logger.info(f"学习闭环: 新约束 → {constraint}")
        return constraint

    def get_active_constraints(self) -> list[str]:
        """获取活跃约束 (最近10条)"""
        return list(self._active_constraints)[-10:]

    def _extract_constraint(self, user_msg: str, bot_reply: str) -> str | None:
        """从用户消息中提取行为约束"""
        msg = user_msg.lower()
        if any(kw in msg for kw in ["不要", "别", "不准", "不能", "禁止"]):
            return f"用户偏好: {user_msg.strip()[:80]}"
        if any(kw in msg for kw in ["应该是", "其实", "不对", "错了"]):
            return f"纠正: {user_msg.strip()[:80]}"
        if "记住" in msg or "记一下" in msg:
            return f"记忆: {user_msg.strip()[:80]}"
        return None

    def get_stats(self) -> dict:
        """返回学习闭环统计 (纠正总数与活跃约束数)."""
        return {
            "total_corrections": self._correction_count,
            "active_constraints": len(self._active_constraints),
        }

    def _persist(self) -> None:
        """持久化约束到 JSON (原子写入)"""
        try:
            data = {
                "constraints": list(self._active_constraints),
                "correction_count": self._correction_count,
            }
            tmp = self._PERSIST_PATH.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._PERSIST_PATH)
        except Exception as e:
            logger.warning(f"LearningLoop.persist_failed: {e}")

    def _load(self) -> None:
        """启动时从 JSON 加载约束"""
        try:
            if self._PERSIST_PATH.exists():
                data = json.loads(self._PERSIST_PATH.read_text(encoding="utf-8"))
                for c in data.get("constraints", []):
                    self._active_constraints.append(c)
                self._correction_count = data.get("correction_count", 0)
        except Exception as e:
            logger.warning(f"LearningLoop.load_failed: {e}")


_learning_loop = LearningLoop()


def get_learning_loop() -> LearningLoop:
    """获取全局 LearningLoop 单例."""
    return _learning_loop
