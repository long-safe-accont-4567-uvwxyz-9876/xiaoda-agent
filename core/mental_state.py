"""L/M/S 三层心理状态模型

参考: ACL 2026 Dynamic Persona Coherence

三层结构:
- L (Long-term) 长期身份层: 稳定, 几乎不变. 从 SOUL.md + IDENTITY.md 加载
- M (Medium-term) 中期意义压力层: 近 7 天用户互动主题、压力事件、关系进展. 每日 Dream 时整合
- S (Short-term) 短期情感层: 当前会话情绪状态、用户最近一次情绪. 实时更新

特性:
- 零质量回退: 默认开启, 可通过 MENTAL_STATE_ENABLED 环境变量关闭
- Windows 兼容: pathlib.Path / os.path
- 原子写入: 使用 utils.atomic_write 保证持久化安全
- 7 天滚动窗口: Dream 时清理过期 M 层数据
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loguru import logger

# 使用相对导入避免循环依赖
try:
    from utils.atomic_write import atomic_json_write
except ImportError:  # pragma: no cover - 兜底, 测试环境可能路径不同
    atomic_json_write = None  # type: ignore[assignment]


# 7 天滚动窗口 (秒)
_SEVEN_DAYS_SECONDS = 7 * 86400

# 零质量回退开关: 默认开启, 可通过环境变量关闭
def _is_enabled() -> bool:
    """读取 MENTAL_STATE_ENABLED 环境变量, 默认开启 (True).

    设为 "0" / "false" / "off" 时关闭.
    """
    val = os.getenv("MENTAL_STATE_ENABLED", "1").strip().lower()
    return val not in ("0", "false", "off", "no", "")


# ============================================================
# L 层: 长期身份 (稳定)
# ============================================================

@dataclass
class LongTermIdentity:
    """L 层: 长期身份 (稳定)"""
    soul_content: str = ""           # SOUL.md 内容
    identity_content: str = ""       # IDENTITY.md 内容
    core_traits: list[str] = field(default_factory=list)  # 核心人格特质
    last_updated: float = 0.0


# ============================================================
# M 层: 中期意义压力 (7 天滚动)
# ============================================================

@dataclass
class MediumTermState:
    """M 层: 中期意义压力 (7 天滚动)"""
    recent_themes: list[str] = field(default_factory=list)  # 近 7 天主题
    stress_events: list[dict] = field(default_factory=list)  # 压力事件 (含 ts 字段)
    relationship_milestones: list[dict] = field(default_factory=list)  # 关系里程碑 (含 ts 字段)
    last_dream_at: float = 0.0  # 上次 Dream 整合时间


# ============================================================
# S 层: 短期情感 (实时)
# ============================================================

@dataclass
class ShortTermEmotion:
    """S 层: 短期情感 (实时)"""
    current_emotion: str = ""       # 当前会话情绪
    user_last_emotion: str = ""     # 用户最近一次情绪
    session_started_at: float = 0.0
    emotion_history: list[dict] = field(default_factory=list)  # 本次会话情绪历史


# ============================================================
# 三层心理状态容器
# ============================================================

@dataclass
class MentalState:
    """三层心理状态模型

    参考: ACL 2026 Dynamic Persona Coherence
    - L: Long-term identity (stable)
    - M: Medium-term meaning-stress (7-day rolling)
    - S: Short-term emotion (real-time)
    """
    L: LongTermIdentity = field(default_factory=LongTermIdentity)
    M: MediumTermState = field(default_factory=MediumTermState)
    S: ShortTermEmotion = field(default_factory=ShortTermEmotion)

    # ── 持久化 ──────────────────────────────────────────

    def to_dict(self) -> dict:
        """序列化为字典 (可 JSON 持久化)."""
        return {
            "L": {
                "soul_content": self.L.soul_content,
                "identity_content": self.L.identity_content,
                "core_traits": list(self.L.core_traits),
                "last_updated": self.L.last_updated,
            },
            "M": {
                "recent_themes": list(self.M.recent_themes),
                "stress_events": list(self.M.stress_events),
                "relationship_milestones": list(self.M.relationship_milestones),
                "last_dream_at": self.M.last_dream_at,
            },
            "S": {
                "current_emotion": self.S.current_emotion,
                "user_last_emotion": self.S.user_last_emotion,
                "session_started_at": self.S.session_started_at,
                "emotion_history": list(self.S.emotion_history),
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> MentalState:
        """从字典反序列化 (兼容缺失字段)."""
        if not d or not isinstance(d, dict):
            return cls()
        L_data = d.get("L", {}) or {}
        M_data = d.get("M", {}) or {}
        S_data = d.get("S", {}) or {}
        return cls(
            L=LongTermIdentity(
                soul_content=L_data.get("soul_content", ""),
                identity_content=L_data.get("identity_content", ""),
                core_traits=list(L_data.get("core_traits", [])),
                last_updated=float(L_data.get("last_updated", 0.0)),
            ),
            M=MediumTermState(
                recent_themes=list(M_data.get("recent_themes", [])),
                stress_events=list(M_data.get("stress_events", [])),
                relationship_milestones=list(M_data.get("relationship_milestones", [])),
                last_dream_at=float(M_data.get("last_dream_at", 0.0)),
            ),
            S=ShortTermEmotion(
                current_emotion=S_data.get("current_emotion", ""),
                user_last_emotion=S_data.get("user_last_emotion", ""),
                session_started_at=float(S_data.get("session_started_at", 0.0)),
                emotion_history=list(S_data.get("emotion_history", [])),
            ),
        )

    def save(self, path: Path) -> None:
        """保存到 JSON 文件 (原子写入)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.to_dict()
        if atomic_json_write is not None:
            atomic_json_write(path, data, indent=2, ensure_ascii=False)
        else:  # pragma: no cover - 兜底
            with open(path, "w", encoding="utf-8") as f:
                import json
                json.dump(data, f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: Path) -> MentalState:
        """从 JSON 文件加载, 文件不存在或损坏时返回空状态."""
        import json
        path = Path(path)
        if not path.exists():
            return cls()
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except (OSError, json.JSONDecodeError, ValueError) as e:
            logger.warning(f"MentalState.load_failed path={path} error={e}")
            return cls()


# ============================================================
# 心理状态管理器
# ============================================================

class MentalStateManager:
    """心理状态管理器

    用法:
        mgr = MentalStateManager(data_dir=Path("data"))
        mgr.reload_long_term(Path("config/workspace"))
        mgr.update_short_term(emotion="喜悦", user_emotion="焦虑")
        prompt_seg = mgr.get_prompt_segment()
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        """data_dir 默认为 data/"""
        self._data_dir = Path(data_dir) if data_dir else Path("data")
        self._state_path = self._data_dir / "mental_state.json"
        self._state = self._load_or_init()

    def _load_or_init(self) -> MentalState:
        """启动时加载, 若文件不存在则初始化空状态."""
        state = MentalState.load(self._state_path)
        if state.L.last_updated == 0.0:
            logger.debug("MentalState.init_empty")
        return state

    @property
    def state(self) -> MentalState:
        """当前心理状态 (只读视图)."""
        return self._state

    @property
    def enabled(self) -> bool:
        """是否启用 (受 MENTAL_STATE_ENABLED 环境变量控制)."""
        return _is_enabled()

    def _save(self) -> None:
        """持久化当前状态."""
        try:
            self._state.save(self._state_path)
        except Exception as e:
            logger.warning(f"MentalState.save_failed error={e}")

    # ── L 层: 长期身份 ──────────────────────────────────

    def reload_long_term(self, workspace_dir: Path) -> None:
        """从 SOUL.md + IDENTITY.md 重新加载 L 层.

        优先读取 .md 文件, 不存在则回退到 .md.tpl 模板.
        """
        workspace_dir = Path(workspace_dir)
        soul_path = workspace_dir / "SOUL.md"
        if not soul_path.exists():
            soul_path = workspace_dir / "SOUL.md.tpl"
        identity_path = workspace_dir / "IDENTITY.md"
        if not identity_path.exists():
            identity_path = workspace_dir / "IDENTITY.md.tpl"

        soul_content = self._read_text(soul_path)
        identity_content = self._read_text(identity_path)

        self._state.L.soul_content = soul_content
        self._state.L.identity_content = identity_content
        self._state.L.core_traits = self._extract_core_traits(soul_content)
        self._state.L.last_updated = time.time()
        self._save()
        logger.info(f"MentalState.L.reloaded traits={self._state.L.core_traits}")

    @staticmethod
    def _read_text(path: Path) -> str:
        """安全读取文本文件, 不存在返回空串."""
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, FileNotFoundError):
            return ""

    @staticmethod
    def _extract_core_traits(soul_content: str) -> list[str]:
        """从 SOUL.md 提取核心人格特质.

        解析 "## 核心人格" 段落下第一个 "- " 列表项,
        按 "、" 分割得到特质列表 (如 ["温柔", "聪慧", "耐心", "认真"]).
        """
        if not soul_content:
            return []
        lines = soul_content.splitlines()
        in_section = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("##"):
                in_section = "核心人格" in stripped
                continue
            if in_section and stripped.startswith("- "):
                # 取第一个列表项, 按 、 分割
                item = stripped[2:].strip()
                return [t.strip() for t in item.split("、") if t.strip()]
        return []

    # ── S 层: 短期情感 (实时) ──────────────────────────

    def update_short_term(self, emotion: str, user_emotion: str = "") -> None:
        """更新 S 层 (实时).

        Args:
            emotion: 当前会话情绪 (小妲自身情绪)
            user_emotion: 用户最近一次情绪
        """
        if not self.enabled:
            return
        now = time.time()
        if self._state.S.session_started_at == 0.0:
            self._state.S.session_started_at = now
        self._state.S.current_emotion = emotion
        if user_emotion:
            self._state.S.user_last_emotion = user_emotion
        # 记录情绪历史 (最多保留 50 条)
        self._state.S.emotion_history.append({
            "ts": now,
            "emotion": emotion,
            "user_emotion": user_emotion,
        })
        if len(self._state.S.emotion_history) > 50:
            self._state.S.emotion_history = self._state.S.emotion_history[-50:]
        self._save()

    # ── M 层: 中期意义压力 (7 天滚动) ──────────────────

    def add_theme(self, theme: str) -> None:
        """添加主题到 M 层."""
        if not self.enabled or not theme.strip():
            return
        if theme not in self._state.M.recent_themes:
            self._state.M.recent_themes.append(theme)
            # 保留最近 30 个主题
            if len(self._state.M.recent_themes) > 30:
                self._state.M.recent_themes = self._state.M.recent_themes[-30:]
            self._save()

    def add_stress_event(self, event: dict) -> None:
        """添加压力事件到 M 层.

        自动补 ts 字段 (若缺失).
        """
        if not self.enabled or not event:
            return
        event = dict(event)
        if "ts" not in event:
            event["ts"] = time.time()
        self._state.M.stress_events.append(event)
        self._save()

    def add_milestone(self, milestone: dict) -> None:
        """添加关系里程碑到 M 层.

        自动补 ts 字段 (若缺失).
        """
        if not self.enabled or not milestone:
            return
        milestone = dict(milestone)
        if "ts" not in milestone:
            milestone["ts"] = time.time()
        self._state.M.relationship_milestones.append(milestone)
        self._save()

    # ── Dream 整合 ──────────────────────────────────────

    def consolidate_dream(self) -> None:
        """Dream 时整合: 清理 7 天前的 M 层数据.

        - stress_events: 移除 ts 早于 7 天前的条目
        - relationship_milestones: 移除 ts 早于 7 天前的条目
        - recent_themes: 保留最近 30 个 (无时间戳, 仅裁剪数量)
        - 更新 last_dream_at
        """
        if not self.enabled:
            return
        now = time.time()
        cutoff = now - _SEVEN_DAYS_SECONDS

        before_events = len(self._state.M.stress_events)
        before_milestones = len(self._state.M.relationship_milestones)

        self._state.M.stress_events = [
            e for e in self._state.M.stress_events
            if float(e.get("ts", 0.0)) >= cutoff
        ]
        self._state.M.relationship_milestones = [
            m for m in self._state.M.relationship_milestones
            if float(m.get("ts", 0.0)) >= cutoff
        ]
        # 主题裁剪 (保留最近 30 个)
        if len(self._state.M.recent_themes) > 30:
            self._state.M.recent_themes = self._state.M.recent_themes[-30:]

        self._state.M.last_dream_at = now
        self._save()

        decayed = (before_events - len(self._state.M.stress_events)
                   + before_milestones - len(self._state.M.relationship_milestones))
        logger.info(f"MentalState.consolidate_dream decayed={decayed} "
                     f"events={len(self._state.M.stress_events)} "
                     f"milestones={len(self._state.M.relationship_milestones)}")

    # ── Prompt 生成 ─────────────────────────────────────

    def get_prompt_segment(self) -> str:
        """生成 prompt 段落 (注入到 system prompt).

        包含 L 层核心特质 + M 层近期主题 + S 层当前情绪.
        若功能关闭返回空串.
        """
        if not self.enabled:
            return ""

        lines: list[str] = ["[当前心理状态]"]

        # L 层: 核心身份
        if self._state.L.core_traits:
            lines.append(f"长期身份：{'、'.join(self._state.L.core_traits)}")
        elif self._state.L.soul_content:
            lines.append("长期身份：小妲 (智慧伙伴)")

        # M 层: 近期主题
        if self._state.M.recent_themes:
            themes_str = "、".join(self._state.M.recent_themes[:5])
            lines.append(f"近期主题：{themes_str}")
        if self._state.M.stress_events:
            lines.append(f"近期压力事件：{len(self._state.M.stress_events)} 件")

        # S 层: 当前情绪 + 回应建议
        user_emo = self._state.S.user_last_emotion
        if user_emo:
            guidance = self._emotion_guidance(user_emo)
            lines.append(f"当前情绪：用户感到{user_emo}，小妲应以{guidance}语气回应")
        elif self._state.S.current_emotion:
            lines.append(f"当前情绪：{self._state.S.current_emotion}")

        return "\n".join(lines)

    @staticmethod
    def _emotion_guidance(user_emotion: str) -> str:
        """根据用户情绪给出回应语气建议."""
        _soothing = {"焦虑", "悲伤", "愤怒", "恐惧", "孤独", "沮丧", "失落", "不安"}
        _cheering = {"开心", "喜悦", "兴奋", "期待"}
        if user_emotion in _soothing:
            return "安抚"
        if user_emotion in _cheering:
            return "轻快"
        return "温柔"


# ============================================================
# 全局单例
# ============================================================

_manager: MentalStateManager | None = None


def get_mental_state_manager(data_dir: Path | None = None) -> MentalStateManager:
    """获取全局 MentalStateManager 单例, 不存在时创建.

    Args:
        data_dir: 数据目录 (仅首次创建时生效, 已存在单例时忽略)
    """
    global _manager
    if _manager is None:
        _manager = MentalStateManager(data_dir=data_dir)
    return _manager


def get_mental_state_manager_if_exists() -> MentalStateManager | None:
    """返回已初始化的全局单例, 未初始化时返回 None.

    用于 Dream 整合等场景, 避免在未显式初始化时创建副作用 (如测试环境).
    """
    return _manager


def reset_mental_state_manager() -> None:
    """重置全局单例 (主要用于测试)."""
    global _manager
    _manager = None
