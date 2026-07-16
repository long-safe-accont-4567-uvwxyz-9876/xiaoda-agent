"""永久记忆与跨会话保留 (Permanent Memory)

跨会话保留用户的关键事件、偏好与关系里程碑:
- 关键事件存储 (首次深度对话 / 首次情感支持 / 升级里程碑等)
- 用户偏好提取与持久化 (称呼 / 喜好)
- 重启后主动提及 "好久不见"
- 注入到 system prompt, 让 Agent 拥有跨会话的连续记忆

设计原则 (与 core/xp_system.py / core/mental_state.py 对齐):
- 轻量: 纯 dataclass + JSON 持久化, 不依赖数据库
- 可插拔: 不修改既有模块接口, 由调用方主动调用 store / record_*
- 幂等: 重复持久化安全, 加载失败回退到空状态
- 零质量回退: 默认开启, 可通过 PERMANENT_MEMORY_ENABLED 环境变量关闭
- Windows 兼容: pathlib.Path / json
- 原子写入: temp + os.replace, 避免崩溃损坏
"""
from __future__ import annotations

from typing import ClassVar
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

# 延迟导入 DATA_DIR, 避免 config 模块在测试中导入失败时影响本模块
try:
    from config import DATA_DIR
except Exception:  # pragma: no cover - 配置缺失时退化为项目根目录
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class PermanentMemoryEntry:
    """永久记忆条目 (跨会话保留)

    Attributes:
        id: 条目唯一 ID (user_id:key 形式, 便于追溯)
        user_id: 用户标识
        category: 类别 (milestone / preference / key_event / relationship)
        key: 唯一键 (如 "first_deep_chat" / "preferred_name")
        value: 值
        timestamp: 创建时间戳 (更新 value 时保留原 timestamp)
        source: 来源 (system / user / agent)
        metadata: 附加元数据
    """
    id: str
    user_id: str
    category: str
    key: str
    value: str
    timestamp: float = field(default_factory=time.time)
    source: str = "system"
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """序列化为字典 (用于 JSON 持久化)."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "category": self.category,
            "key": self.key,
            "value": self.value,
            "timestamp": self.timestamp,
            "source": self.source,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict) -> PermanentMemoryEntry:
        """从字典反序列化 (兼容缺失字段)."""
        return cls(
            id=str(d.get("id", "")),
            user_id=str(d.get("user_id", "")),
            category=str(d.get("category", "")),
            key=str(d.get("key", "")),
            value=str(d.get("value", "")),
            timestamp=float(d.get("timestamp", 0.0)),
            source=str(d.get("source", "system")),
            metadata=dict(d.get("metadata", {})),
        )


def _is_enabled() -> bool:
    """检查永久记忆是否启用 (默认开启, 可通过 PERMANENT_MEMORY_ENABLED 关闭)."""
    val = os.getenv("PERMANENT_MEMORY_ENABLED", "true").strip().lower()
    return val not in ("0", "false", "off", "no", "")


class PermanentMemoryManager:
    """永久记忆管理器

    跨会话保留:
    - 关键事件存储 (首次深度对话 / 首次情感支持 / 升级里程碑等)
    - 用户偏好提取与持久化
    - 重启后主动提及 "好久不见"

    用法:
        mgr = PermanentMemoryManager(data_dir=Path("data"))
        mgr.record_key_event("u1", "first_chat", "首次对话")
        mgr.set_preference("u1", "preferred_name", "小纳")
        opener = mgr.get_session_opener("u1")
        prompt_seg = mgr.get_prompt_segment("u1")
    """

    # 关键事件类型
    KEY_EVENTS: ClassVar[dict[str, str]] = {
        "first_chat": "首次对话",
        "first_deep_chat": "首次深度对话",
        "first_emotional_support": "首次情感支持",
        "first_task_collab": "首次共同完成任务",
        "levelup_milestone": "等级升级里程碑",
        "relationship_change": "关系变化",
    }

    def __init__(self, data_dir: Path | None = None) -> None:
        """初始化永久记忆管理器.

        Args:
            data_dir: 持久化目录, 默认为 config.DATA_DIR
        """
        self._data_dir = Path(data_dir) if data_dir else Path(DATA_DIR)
        self._memories_path = self._data_dir / "permanent_memories.json"
        # user_id -> {key: PermanentMemoryEntry}
        self._memories: dict[str, dict[str, PermanentMemoryEntry]] = {}
        self._load()

    @property
    def enabled(self) -> bool:
        """是否启用 (受 PERMANENT_MEMORY_ENABLED 环境变量控制)."""
        return _is_enabled()

    # ── 持久化 ──────────────────────────────────────────────

    def _load(self) -> None:
        """从 JSON 加载所有用户记忆, 文件不存在或损坏时保持空状态."""
        if not self._memories_path.exists():
            return
        try:
            with open(self._memories_path, encoding="utf-8") as f:
                data = json.load(f)
            self._memories = {
                uid: {
                    key: PermanentMemoryEntry.from_dict(entry)
                    for key, entry in entries.items()
                }
                for uid, entries in data.items()
            }
            logger.info(
                f"PermanentMemory.load path={self._memories_path} "
                f"users={len(self._memories)}"
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.error(f"PermanentMemory.load FAILED — file corrupted: {e}. Backing up and starting fresh.")
            # 备份损坏的文件，避免数据被覆盖后无法恢复
            import shutil
            backup = self._memories_path.with_suffix('.json.corrupt')
            try:
                shutil.copy2(self._memories_path, backup)
                logger.warning(f"PermanentMemory backed up corrupt file to {backup}")
            except Exception:
                logger.debug("permanent_memory.corrupt_backup_copy_error: {}", exc_info=True)
            self._memories = {}

    def _save(self) -> None:
        """原子化保存到 JSON 文件 (.tmp + os.replace), offloaded to thread."""
        self._memories_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            uid: {key: entry.to_dict() for key, entry in entries.items()}
            for uid, entries in self._memories.items()
        }

        def _write() -> None:
            tmp = self._memories_path.with_suffix(".json.tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._memories_path)
            except Exception as e:
                logger.warning(f"PermanentMemory.save_failed: {e}")
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _write)
        except RuntimeError:
            # No event loop, run synchronously
            _write()

    # ── 基础 CRUD ──────────────────────────────────────────

    def store(self, user_id: str, category: str, key: str, value: str,
              source: str = "system",
              metadata: dict | None = None) -> PermanentMemoryEntry:
        """存储永久记忆.

        若 key 已存在, 更新 value (保留原 timestamp).

        Args:
            user_id: 用户 ID
            category: 类别 (milestone / preference / key_event / relationship)
            key: 唯一键
            value: 值
            source: 来源 (system / user / agent)
            metadata: 附加元数据

        Returns:
            存储后的 PermanentMemoryEntry
        """
        if not _is_enabled():
            # 关闭时返回临时对象, 不持久化
            return PermanentMemoryEntry(
                id=f"{user_id}:{key}",
                user_id=user_id,
                category=category,
                key=key,
                value=value,
                source=source,
                metadata=metadata or {},
            )

        user_memories = self._memories.setdefault(user_id, {})
        existing = user_memories.get(key)
        if existing is not None:
            # 保留原 timestamp, 仅更新 value / source / metadata
            existing.value = value
            existing.source = source
            if metadata is not None:
                existing.metadata = dict(metadata)
            entry = existing
        else:
            entry = PermanentMemoryEntry(
                id=f"{user_id}:{key}",
                user_id=user_id,
                category=category,
                key=key,
                value=value,
                source=source,
                metadata=metadata or {},
            )
            user_memories[key] = entry
        self._save()
        logger.debug(
            f"PermanentMemory.store user={user_id} cat={category} key={key}"
        )
        return entry

    def retrieve(self, user_id: str, key: str) -> PermanentMemoryEntry | None:
        """获取单条记忆."""
        return self._memories.get(user_id, {}).get(key)

    def retrieve_by_category(self, user_id: str,
                             category: str) -> list[PermanentMemoryEntry]:
        """按类别获取所有记忆."""
        user_memories = self._memories.get(user_id, {})
        return [e for e in user_memories.values() if e.category == category]

    def retrieve_all(self, user_id: str) -> dict[str, PermanentMemoryEntry]:
        """获取用户所有永久记忆 (key -> entry)."""
        return dict(self._memories.get(user_id, {}))

    def delete(self, user_id: str, key: str) -> bool:
        """删除记忆.

        Returns:
            True 若删除成功, False 若条目不存在
        """
        user_memories = self._memories.get(user_id, {})
        if key not in user_memories:
            return False
        del user_memories[key]
        if not user_memories:
            self._memories.pop(user_id, None)
        if _is_enabled():
            self._save()
        return True

    # ── 关键事件 ──────────────────────────────────────────

    def record_key_event(self, user_id: str, event_type: str, value: str,
                         metadata: dict | None = None
                         ) -> PermanentMemoryEntry | None:
        """记录关键事件 (若已存在则不重复记录).

        :param event_type: KEY_EVENTS 中的键
        :param value: 事件描述
        :param metadata: 附加元数据
        :return: 新建或已存在的条目; 未知事件类型返回 None
        """
        if event_type not in self.KEY_EVENTS:
            logger.warning(
                "PermanentMemory.unknown_event_type",
                event_type=event_type,
            )
            return None

        # 检查是否已记录
        existing = self.retrieve(user_id, event_type)
        if existing:
            logger.debug(
                "PermanentMemory.event_already_recorded",
                user_id=user_id,
                event_type=event_type,
            )
            return existing

        return self.store(
            user_id=user_id,
            category="key_event",
            key=event_type,
            value=value,
            source="system",
            metadata=metadata or {},
        )

    # ── 用户偏好 ──────────────────────────────────────────

    def set_preference(self, user_id: str, pref_key: str,
                       pref_value: str) -> PermanentMemoryEntry:
        """设置用户偏好."""
        return self.store(user_id, "preference", pref_key, pref_value,
                          source="agent")

    def get_preference(self, user_id: str, pref_key: str) -> str | None:
        """获取用户偏好."""
        entry = self.retrieve(user_id, pref_key)
        return entry.value if entry else None

    # ── 关系里程碑 ────────────────────────────────────────

    def record_milestone(self, user_id: str, milestone: str,
                         metadata: dict | None = None) -> PermanentMemoryEntry:
        """记录关系里程碑."""
        return self.store(
            user_id=user_id,
            category="relationship",
            key=f"milestone_{int(time.time())}",
            value=milestone,
            source="system",
            metadata=metadata or {},
        )

    # ── 跨会话恢复 ────────────────────────────────────────

    def get_session_opener(self, user_id: str) -> str:
        """获取会话开场白 (基于永久记忆).

        如果用户长时间未互动, 主动提及 "好久不见".

        Returns:
            开场白文本; 新用户或短时间内重启返回空串
        """
        if not _is_enabled():
            return ""

        all_memories = self.retrieve_all(user_id)
        if not all_memories:
            return ""  # 新用户, 无开场白

        # 检查首次对话是否已记录
        first_chat = all_memories.get("first_chat")
        if not first_chat:
            return ""

        # 找到最近的事件时间
        latest_ts = max((m.timestamp for m in all_memories.values()), default=0)
        days_since = (time.time() - latest_ts) / 86400

        if days_since >= 7:
            # 长时间未互动, 主动提及
            opener = f"好久不见呀~距离上次聊天已经 {int(days_since)} 天了呢🌿"

            # 提及最近的关键事件
            milestones = self.retrieve_by_category(user_id, "relationship")
            if milestones:
                latest_milestone = max(milestones, key=lambda m: m.timestamp)
                opener += f"\n记得上次我们聊到{latest_milestone.value}~"

            return opener
        if days_since >= 1:
            return "又见面啦~今天想聊些什么呢？"
        return ""  # 短时间内重启, 不需要特殊开场

    # ── 集成到 prompt ─────────────────────────────────────

    def get_prompt_segment(self, user_id: str) -> str:
        """生成 prompt 段落 (注入到 system prompt).

        包含:
        - 用户偏好 (如称呼 / 喜好)
        - 关键事件提醒 (最近 3 个)
        """
        if not _is_enabled():
            return ""

        all_memories = self.retrieve_all(user_id)
        if not all_memories:
            return ""

        lines = ["[永久记忆]"]

        # 用户偏好
        preferences = {k: v for k, v in all_memories.items()
                      if v.category == "preference"}
        if preferences:
            lines.append("用户偏好：")
            for k, v in preferences.items():
                lines.append(f"  - {k}: {v.value}")

        # 关键事件 (最近 3 个)
        key_events = [m for m in all_memories.values()
                      if m.category == "key_event"]
        if key_events:
            key_events.sort(key=lambda m: m.timestamp, reverse=True)
            lines.append("近期关键事件：")
            for event in key_events[:3]:
                lines.append(f"  - {event.value}")

        if len(lines) == 1:
            return ""  # 只有标题行, 无内容

        return "\n".join(lines)


# ── 单例 (供调用方共享) ─────────────────────────────────────

_permanent_memory_manager: PermanentMemoryManager | None = None


def get_permanent_memory_manager() -> PermanentMemoryManager:
    """获取全局单例 PermanentMemoryManager."""
    global _permanent_memory_manager
    if _permanent_memory_manager is None:
        _permanent_memory_manager = PermanentMemoryManager()
    return _permanent_memory_manager


def reset_permanent_memory_manager() -> None:
    """重置全局单例 (主要用于测试)."""
    global _permanent_memory_manager
    _permanent_memory_manager = None
