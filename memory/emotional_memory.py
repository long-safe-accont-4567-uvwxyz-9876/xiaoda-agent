"""情感记忆系统 — Stanislavski 情感记忆理论

四阶段能力：
1. Anchoring：将用户表达的情绪 + 事件 + 上下文存储
2. Recalling：相关话题触发时召回（Jaccard 相似度）
3. Bounding：避免情绪过载（同一会话最多注入 3 条）
4. Enacting：以小妲口吻复述记忆

零质量回退：默认开启，可通过环境变量 EMOTIONAL_MEMORY_ENABLED 关闭
（设为 0/false/off 时，recall_and_enact 不注入任何记忆）。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from utils.atomic_write import atomic_json_write


@dataclass
class EmotionalMemory:
    """情感记忆条目

    参考: Stanislavski 情感记忆理论
    Anchoring：将用户表达的情绪+事件+上下文存储
    Recalling：相关话题触发时召回
    Bounding：避免情绪过载（同一会话最多注入 3 条）
    Enacting：以小妲口吻复述记忆
    """
    id: str                       # 唯一 ID（uuid 或 timestamp-based）
    user_id: str
    event: str                    # 事件描述（"用户提到工作压力大"）
    emotion: str                  # 情绪标签（"难过"/"焦虑"/"开心"）
    context: str                  # 完整上下文（用户原话片段）
    timestamp: float = field(default_factory=time.time)
    keywords: list[str] = field(default_factory=list)  # 关键词用于召回
    recall_count: int = 0         # 被召回次数
    last_recalled_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "event": self.event,
            "emotion": self.emotion,
            "context": self.context,
            "timestamp": self.timestamp,
            "keywords": list(self.keywords),
            "recall_count": self.recall_count,
            "last_recalled_at": self.last_recalled_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EmotionalMemory":
        return cls(
            id=d["id"],
            user_id=d["user_id"],
            event=d["event"],
            emotion=d["emotion"],
            context=d.get("context", ""),
            timestamp=d.get("timestamp", time.time()),
            keywords=list(d.get("keywords", [])),
            recall_count=int(d.get("recall_count", 0)),
            last_recalled_at=float(d.get("last_recalled_at", 0.0)),
        )


class EmotionalMemoryManager:
    """情感记忆管理器

    实现 4 阶段能力：
    1. Anchoring：存储情感事件
    2. Recalling：Jaccard 相似度召回
    3. Bounding：限制每次注入数量（默认 3）
    4. Enacting：以小妲口吻复述
    """

    MAX_INJECT_PER_SESSION = 3  # Bounding 上限
    MAX_MEMORIES_PER_USER = 500  # 每用户上限

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path("data")
        self._memories_path = self._data_dir / "emotional_memories.json"
        self._memories: dict[str, list[EmotionalMemory]] = {}  # user_id -> memories
        self._session_injected: dict[str, set[str]] = {}  # user_id -> injected memory ids (this session)
        self._load()

    # === 持久化 ===
    def _load(self) -> None:
        if not self._memories_path.exists():
            return
        try:
            with self._memories_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            for user_id, mems in data.items():
                self._memories[user_id] = [EmotionalMemory.from_dict(m) for m in mems]
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.warning(f"emotional_memory.load_failed: {e}")
            self._memories = {}

    def _save(self) -> None:
        try:
            data = {
                user_id: [m.to_dict() for m in mems]
                for user_id, mems in self._memories.items()
            }
            atomic_json_write(self._memories_path, data, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"emotional_memory.save_failed: {e}")

    @staticmethod
    def is_enabled() -> bool:
        """是否启用情感记忆（默认开启，可通过 EMOTIONAL_MEMORY_ENABLED 关闭）"""
        val = os.environ.get("EMOTIONAL_MEMORY_ENABLED", "1").strip().lower()
        return val not in ("0", "false", "off", "no", "")

    # === Anchoring ===
    def anchor(self, user_id: str, event: str, emotion: str, context: str,
               keywords: list[str] | None = None) -> EmotionalMemory:
        """存储情感事件

        :param user_id: 用户 ID
        :param event: 事件描述
        :param emotion: 情绪标签
        :param context: 用户原话片段
        :param keywords: 关键词（用于召回，若 None 则自动提取）
        """
        if keywords is None:
            keywords = self._extract_keywords(context)

        memory_id = f"em_{int(time.time() * 1000)}_{len(self._memories.get(user_id, []))}"
        memory = EmotionalMemory(
            id=memory_id,
            user_id=user_id,
            event=event,
            emotion=emotion,
            context=context[:500],  # 截断
            keywords=keywords,
        )

        if user_id not in self._memories:
            self._memories[user_id] = []
        self._memories[user_id].append(memory)

        # 限制每个用户最多 MAX_MEMORIES_PER_USER 条
        if len(self._memories[user_id]) > self.MAX_MEMORIES_PER_USER:
            self._memories[user_id] = self._memories[user_id][-self.MAX_MEMORIES_PER_USER:]

        self._save()
        logger.info(f"emotional_memory.anchored user_id={user_id} "
                    f"emotion={emotion} event={event[:50]}")
        return memory

    def _extract_keywords(self, text: str) -> list[str]:
        """从文本提取关键词（简化版：去停用词 + 取名词性词汇）"""
        # 简化：分词 + 去停用词
        stopwords = {"的", "了", "是", "在", "我", "你", "他", "她", "它", "们",
                     "和", "与", "或", "但", "也", "都", "就", "这", "那", "有",
                     "没", "不", "很", "太", "非常"}
        # 中文分词（简化版：按标点/空格分割）
        words = re.split(r"[，。！？\s,.;:!?\n]+", text)
        keywords = [w for w in words if w and len(w) >= 2 and w not in stopwords]
        return keywords[:10]  # 最多 10 个

    # === Recalling ===
    def recall(self, user_id: str, query: str, top_k: int = 3) -> list[EmotionalMemory]:
        """召回相关情感记忆

        使用 Jaccard 相似度（基于关键词集合）
        """
        if user_id not in self._memories:
            return []

        query_keywords = set(self._extract_keywords(query))
        if not query_keywords:
            return []

        scored = []
        for mem in self._memories[user_id]:
            mem_keywords = set(mem.keywords)
            if not mem_keywords:
                continue
            # Jaccard 相似度
            intersection = len(query_keywords & mem_keywords)
            union = len(query_keywords | mem_keywords)
            similarity = intersection / union if union > 0 else 0
            if similarity > 0:
                scored.append((mem, similarity))

        # 按相似度排序，取 top_k
        scored.sort(key=lambda x: x[1], reverse=True)
        results = [mem for mem, _ in scored[:top_k]]

        # 更新召回计数
        for mem in results:
            mem.recall_count += 1
            mem.last_recalled_at = time.time()

        if results:
            self._save()

        return results

    # === Bounding ===
    def bound(self, user_id: str, memories: list[EmotionalMemory]) -> list[EmotionalMemory]:
        """限制注入：同一会话最多 MAX_INJECT_PER_SESSION 条

        已注入的不重复注入。
        """
        if user_id not in self._session_injected:
            self._session_injected[user_id] = set()

        injected = self._session_injected[user_id]
        result = []
        for mem in memories:
            if mem.id not in injected and len(result) < self.MAX_INJECT_PER_SESSION:
                injected.add(mem.id)
                result.append(mem)

        return result

    def reset_session(self, user_id: str) -> None:
        """重置会话（清空已注入集合）"""
        self._session_injected.pop(user_id, None)

    # === Enacting ===
    def enact(self, memories: list[EmotionalMemory], user_xp_level: int = 1) -> str:
        """以小妲口吻复述记忆

        :param memories: 情感记忆列表
        :param user_xp_level: 用户 XP 等级（影响口吻亲密度）
        :returns: 注入到 prompt 的记忆段落
        """
        if not memories:
            return ""

        # 根据等级调整亲密度
        if user_xp_level >= 3:
            opener = "记得"
        else:
            opener = "我想到"

        lines = ["[情感记忆召回]"]
        for mem in memories:
            time_desc = self._format_time(mem.timestamp)
            line = f"{opener} {time_desc}，{mem.event}，你当时{mem.emotion}。"
            if user_xp_level >= 3:
                line += f'（你说过："{mem.context[:50]}"）'
            lines.append(line)

        lines.append("(请在回复中自然地提及这些记忆，避免生硬)")
        return "\n".join(lines)

    def _format_time(self, ts: float) -> str:
        """格式化时间为相对时间描述"""
        delta = time.time() - ts
        if delta < 3600:
            return "刚才"
        elif delta < 86400:
            return f"{int(delta / 3600)} 小时前"
        elif delta < 604800:
            return f"{int(delta / 86400)} 天前"
        else:
            return f"{int(delta / 604800)} 周前"

    # === 集成入口 ===
    def recall_and_enact(self, user_id: str, query: str, user_xp_level: int = 1) -> str:
        """一步到位：召回 + Bounding + Enacting

        零质量回退：未启用或异常时返回空串，不影响主流程。
        """
        if not self.is_enabled():
            return ""
        try:
            recalled = self.recall(user_id, query)
            bounded = self.bound(user_id, recalled)
            return self.enact(bounded, user_xp_level)
        except Exception as e:
            logger.warning(f"emotional_memory.recall_and_enact_failed: {e}")
            return ""


# 单例
_emotional_memory_manager: EmotionalMemoryManager | None = None


def get_emotional_memory_manager() -> EmotionalMemoryManager:
    """获取全局情绪记忆管理器单例。"""
    global _emotional_memory_manager
    if _emotional_memory_manager is None:
        _emotional_memory_manager = EmotionalMemoryManager()
    return _emotional_memory_manager
