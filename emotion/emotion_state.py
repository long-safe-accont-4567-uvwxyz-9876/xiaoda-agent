"""情绪状态机 —— 让 agent 拥有持续的情绪，而非每条消息独立推断。

核心机制：
1. 情绪有强度（intensity），随时间衰减
2. 相同情绪叠加，不同情绪需要超过当前衰减强度才替换
3. 衰减到阈值以下后回到 neutral
4. 被骂了会持续低落，被夸了会持续开心

这样 agent 的情绪有了"惯性"，更像有感情的生命。
"""
from __future__ import annotations

import asyncio
import time
import threading

from loguru import logger


class EmotionState:
    """持续情绪状态（线程安全）。

    情绪强度衰减模型：
    - 1 小时内衰减到原来的 50%
    - 2 小时衰减到 25%
    - 强度 < 0.1 时回到 neutral
    """

    # 衰减率：每小时衰减到原来的 50%
    DECAY_RATE_PER_HOUR = 0.5
    # 中性阈值：低于此强度回到 neutral
    NEUTRAL_THRESHOLD = 0.1

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current = "neutral"
        self._intensity = 0.0
        self._last_update = time.time()
        # 多情绪共存：{emotion: (intensity, timestamp)}，最多保留3个
        self._active_emotions: dict[str, tuple[float, float]] = {}
        # PAD 值（用于 shift_pad 微调）
        self._pad: dict = {"P": 0.0, "A": 0.0, "D": 0.5}
        # 情绪历史：最近 20 条，用于回顾
        self._history: list[tuple[float, str, float]] = []
        # 持久化文件
        import os
        from pathlib import Path
        self._persist_path = Path(
            os.getenv("EMOTION_STATE_PATH",
                      str(Path.home() / ".ai-agent" / "emotion_state.json"))
        )
        self._load()

    def update(self, emotion: str, intensity: float = 0.5,
               context: dict | None = None) -> None:
        """更新情绪状态。

        Args:
            emotion: 情绪名（happy/sad/angry 等）
            intensity: 情绪强度 0.0-1.0
            context: J-Space 方向上下文（可选，用于 Hook #4 方向控制情绪）
        """
        if not emotion or emotion == "neutral":
            # neutral 不替换当前情绪，只让强度衰减更快
            with self._lock:
                self._intensity = self._decayed_intensity() * 0.7
                if self._intensity < self.NEUTRAL_THRESHOLD:
                    self._current = "neutral"
                    self._intensity = 0.0
                self._last_update = time.time()
            return

        intensity = max(0.0, min(1.0, intensity))
        with self._lock:
            now = time.time()
            decayed = self._decayed_intensity()

            if emotion == self._current:
                # 相同情绪叠加（但不超过 1.0）
                self._intensity = min(1.0, decayed + intensity * 0.3)
            else:
                # 新情绪需要超过当前衰减后的强度才能替换
                if intensity > decayed:
                    self._current = emotion
                    self._intensity = intensity
                else:
                    # 保持当前情绪，强度衰减
                    self._intensity = decayed

            # 多情绪共存：将新情绪加入 _active_emotions (存储 intensity + timestamp)
            if emotion != "neutral" and emotion:
                self._active_emotions[emotion] = (intensity, now)
                # 保留强度最高的3个
                if len(self._active_emotions) > 3:
                    sorted_em = sorted(self._active_emotions.items(), key=lambda x: x[1][0], reverse=True)
                    self._active_emotions = dict(sorted_em[:3])

            self._last_update = now
            self._history.append((now, self._current, self._intensity))
            if len(self._history) > 20:
                self._history = self._history[-20:]

            logger.debug("emotion_state.updated",
                         emotion=self._current, intensity=f"{self._intensity:.2f}")

        # J-Space Hook: 方向控制情绪
        try:
            from config import ENABLE_J_SPACE_HOOKS
            if ENABLE_J_SPACE_HOOKS:
                from core.behavioral_direction import DirectionVector
                # 应用 emotion_offset 方向
                emotion_offset = context.get("emotion_offset", 0.0) if context else 0.0
                if emotion_offset != 0.0:
                    # 调整情绪状态
                    # TODO(phase-2): apply emotion_offset direction to emotion state
                    pass
        except Exception as e:
            logger.debug("emotion_state.j_space_hook_failed", error=str(e))

        # 异步持久化（不阻塞主流程）
        try:
            self._save()
        except Exception as e:
            logger.debug("emotion_state.save_failed", error=str(e))

    def get_current(self) -> tuple[str, float]:
        """获取当前情绪和衰减后的强度。

        Returns:
            (emotion, intensity) - 强度 < 0.1 时返回 ("neutral", 0.0)
        """
        with self._lock:
            decayed = self._decayed_intensity()
            if decayed < self.NEUTRAL_THRESHOLD:
                return "neutral", 0.0
            return self._current, decayed

    def shift_pad(self, pad: dict, weight: float = 0.1) -> None:
        """微调当前 PAD 值（不替换，仅偏移）

        用于情绪记忆召回时微调当前情绪状态。

        Args:
            pad: {"P": float, "A": float, "D": float} 目标 PAD 值
            weight: 偏移权重 0-1，默认 0.1（微调10%）
        """
        w = max(0.0, min(1.0, weight))
        with self._lock:
            self._pad = {
                "P": max(-1.0, min(1.0, self._pad["P"] * (1 - w) + pad.get("P", 0) * w)),
                "A": max(0.0, min(1.0, self._pad["A"] * (1 - w) + pad.get("A", 0) * w)),
                "D": max(0.0, min(1.0, self._pad["D"] * (1 - w) + pad.get("D", 0.5) * w)),
            }
        try:
            self._save()
        except Exception as e:
            logger.debug("emotion_state.save_failed", error=str(e))

    def get_description(self) -> str:
        """获取情绪描述文本（用于注入 prompt）。

        返回格式：[当前心情：开心（强度 0.6，已持续 15 分钟）]
        如果是 neutral，返回空字符串（不注入）。
        """
        emotion, intensity = self.get_current()
        if emotion == "neutral" or intensity < self.NEUTRAL_THRESHOLD:
            return ""

        with self._lock:
            elapsed = time.time() - self._last_update
            duration_min = int(elapsed / 60)

        # 情绪中文映射
        cn_map = {
            "happy": "开心", "excited": "兴奋", "love": "喜爱",
            "shy": "害羞", "sad": "难过", "angry": "生气",
            "surprised": "惊讶", "confused": "困惑", "thinking": "思考",
            "playful": "调皮", "moved": "感动", "anxious": "焦虑",
            "fear": "害怕", "pout": "撒娇",
        }
        cn = cn_map.get(emotion, emotion)

        if duration_min > 0:
            return f"[当前心情：{cn}（强度 {intensity:.1f}，已持续 {duration_min} 分钟）]"
        return f"[当前心情：{cn}（强度 {intensity:.1f}）]"

    def get_active_emotions(self) -> list[tuple[str, float]]:
        """获取所有活跃情绪及衰减后强度（按强度降序）

        每个情绪使用各自的更新时间戳独立计算衰减。

        Returns:
            [(emotion, intensity), ...] — 最多3个
        """
        with self._lock:
            now = time.time()
            result = []
            for emo, (raw_intensity, emo_ts) in self._active_emotions.items():
                # 使用每个情绪自己的时间戳计算衰减
                elapsed = now - emo_ts
                decayed = raw_intensity * (self.DECAY_RATE_PER_HOUR ** (elapsed / 3600))
                if decayed >= self.NEUTRAL_THRESHOLD:
                    result.append((emo, decayed))
            result.sort(key=lambda x: x[1], reverse=True)
            return result[:3]

    def get_pad(self) -> dict:
        """获取当前 PAD 值"""
        with self._lock:
            return dict(self._pad)

    def _decayed_intensity(self) -> float:
        """计算衰减后的强度。"""
        elapsed = time.time() - self._last_update
        # 指数衰减：每小时衰减到原来的 50%
        decayed = self._intensity * (self.DECAY_RATE_PER_HOUR ** (elapsed / 3600))
        return max(0.0, decayed)

    def _save(self) -> None:
        """持久化到 JSON 文件（异步 fire-and-forget，避免阻塞事件循环）。"""
        import json
        with self._lock:
            data = {
                "current": self._current,
                "intensity": self._intensity,
                "last_update": self._last_update,
                "history": self._history[-10:],
                "active_emotions": {k: list(v) for k, v in self._active_emotions.items()},
                "pad": self._pad,
            }
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(asyncio.to_thread(
                self._persist_path.write_text, payload, encoding="utf-8"
            ))
        except RuntimeError:
            self._persist_path.write_text(payload, encoding="utf-8")

    def _load(self) -> None:
        """从 JSON 文件加载。"""
        import json
        if not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            with self._lock:
                self._current = data.get("current", "neutral")
                self._intensity = data.get("intensity", 0.0)
                self._last_update = data.get("last_update", time.time())
                self._history = [(t, e, i) for t, e, i in data.get("history", [])]
                # 兼容旧格式（float）和新格式（[intensity, timestamp]）
                raw_active = data.get("active_emotions", {})
                self._active_emotions = {}
                for k, v in raw_active.items():
                    if isinstance(v, (list, tuple)) and len(v) == 2:
                        self._active_emotions[k] = (float(v[0]), float(v[1]))
                    elif isinstance(v, (int, float)):
                        # 旧格式：只有 intensity，用 last_update 作为时间戳
                        self._active_emotions[k] = (float(v), self._last_update)
                self._pad = data.get("pad", {"P": 0.0, "A": 0.0, "D": 0.5})
            logger.info("emotion_state.loaded",
                        emotion=self._current, intensity=f"{self._intensity:.2f}")
        except Exception as e:
            logger.debug("emotion_state.load_failed", error=str(e))


# 全局单例
_instance: EmotionState | None = None
_instance_lock = threading.Lock()


def get_emotion_state() -> EmotionState:
    """获取全局情绪状态单例。"""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = EmotionState()
    return _instance