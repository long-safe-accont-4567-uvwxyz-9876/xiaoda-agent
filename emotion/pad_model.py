"""PAD 三维情绪模型 — Pleasure-Arousal-Dominance 连续空间

将硬分类(9类关键词)升级为PAD连续空间，提供更细腻的情绪表示。
P: Pleasure -1(不悦) ~ +1(愉悦)
A: Arousal 0(平静) ~ 1(激动)
D: Dominance 0(受控) ~ 1(掌控)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PADEmotion:
    """PAD 三维情绪值"""
    P: float  # Pleasure: -1 ~ +1
    A: float  # Arousal: 0 ~ 1
    D: float  # Dominance: 0 ~ 1

    def clamp(self) -> "PADEmotion":
        """将各维度限制到合法范围"""
        return PADEmotion(
            P=max(-1.0, min(1.0, self.P)),
            A=max(0.0, min(1.0, self.A)),
            D=max(0.0, min(1.0, self.D)),
        )

    def scale(self, factor: float) -> "PADEmotion":
        """按强度因子缩放 PAD 值"""
        return PADEmotion(
            P=self.P * factor,
            A=self.A * factor,
            D=self.D * factor,
        )

    def to_dict(self) -> dict:
        return {"P": round(self.P, 4), "A": round(self.A, 4), "D": round(self.D, 4)}

    @classmethod
    def from_dict(cls, d: dict) -> "PADEmotion":
        return cls(P=float(d.get("P", 0)), A=float(d.get("A", 0)), D=float(d.get("D", 0.5)))

    @classmethod
    def neutral(cls) -> "PADEmotion":
        """中性情绪的 PAD 值"""
        return cls(P=0.0, A=0.0, D=0.5)


# 9类中文标签 → PAD 参考值映射
EMOTION_PAD_REFERENCE: dict[str, PADEmotion] = {
    "喜悦": PADEmotion(0.8, 0.5, 0.6),
    "兴奋": PADEmotion(0.9, 0.9, 0.7),
    "悲伤": PADEmotion(-0.7, 0.3, 0.2),
    "愤怒": PADEmotion(-0.6, 0.8, 0.8),
    "焦虑": PADEmotion(-0.5, 0.7, 0.3),
    "害羞": PADEmotion(0.2, 0.4, 0.2),
    "好奇": PADEmotion(0.3, 0.5, 0.5),
    "思考": PADEmotion(0.0, 0.2, 0.5),
    "恐惧": PADEmotion(-0.8, 0.8, 0.1),
    "平静": PADEmotion(0.0, 0.0, 0.5),
}


def from_emotion(label: str, intensity: float = 1.0) -> PADEmotion:
    """根据情绪标签和强度生成 PAD 值

    Args:
        label: 中文情绪标签（喜悦/悲伤/愤怒等）
        intensity: 强度 0.0-1.0，调制各维度

    Returns:
        PADEmotion: 缩放后的 PAD 值，未知标签返回 neutral
    """
    base = EMOTION_PAD_REFERENCE.get(label, PADEmotion.neutral())
    return base.scale(intensity).clamp()


def blend(pad1: PADEmotion, pad2: PADEmotion, weight: float = 0.5) -> PADEmotion:
    """混合两个 PAD 值

    Args:
        pad1: 第一个 PAD 值
        pad2: 第二个 PAD 值
        weight: pad2 的权重 0-1

    Returns:
        混合后的 PADEmotion
    """
    w = max(0.0, min(1.0, weight))
    return PADEmotion(
        P=pad1.P * (1 - w) + pad2.P * w,
        A=pad1.A * (1 - w) + pad2.A * w,
        D=pad1.D * (1 - w) + pad2.D * w,
    ).clamp()
