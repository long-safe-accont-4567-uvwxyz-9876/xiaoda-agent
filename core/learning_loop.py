"""学习反馈闭环 — 纠正记录 → 模式提取 → 约束注入 → 后续行为改变

约束持久化到 DATA_DIR/active_constraints.json, 重启不丢失。
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from pathlib import Path

from loguru import logger

try:
    from config import DATA_DIR
except (ImportError, AttributeError):
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

except Exception:
    logger.exception(".core.learning_loop.unexpected")
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

try:
    from utils.atomic_write import atomic_json_write
except (ImportError, AttributeError):
    atomic_json_write = None  # type: ignore[assignment]


except Exception:
    logger.exception(".core.learning_loop.unexpected")
    atomic_json_write = None  # type: ignore[assignment]


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
            # 根因修复：_persist 同步 json.dump 写文件，在 async 路径会阻塞事件循环。
            # 用 asyncio.to_thread 隔离到线程池。
            await asyncio.to_thread(self._persist)
            logger.info("学习闭环: 新约束 → {}", constraint)
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
            if atomic_json_write is not None:
                atomic_json_write(self._persist_path, data,
                                  encoding="utf-8", indent=2, ensure_ascii=False)
            else:
                # fallback: 固定 tmp 方式（atomic_write 不可用时）
                tmp = self._persist_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp, self._persist_path)
        except Exception as e:
            logger.warning("LearningLoop.persist_failed: {}", e)

    def _load(self) -> None:
        """启动时从 JSON 加载约束"""
        try:
            if self._persist_path.exists():
                data = json.loads(self._persist_path.read_text(encoding="utf-8"))
                for c in data.get("constraints", []):
                    self._active_constraints.append(c)
                self._correction_count = data.get("correction_count", 0)
        except Exception as e:
            logger.warning("LearningLoop.load_failed: {}", e)


_learning_loop = LearningLoop()


def get_learning_loop() -> LearningLoop:
    """获取全局 LearningLoop 单例."""
    return _learning_loop