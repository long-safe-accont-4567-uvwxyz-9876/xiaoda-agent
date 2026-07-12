# agent_core/structured_blackboard.py
"""
结构化共享黑板 — 在 SharedBlackboard 基础上增加语义索引。

对齐:
- SAELens/sae_lens/training/activations_store.py: ActivationsStore 的结构化存储
- reprobe/store.py: ActivationStore 的 HDF5 持久化模式
- jlens/lens.py: JacobianLens.merge() 的加权合并
"""
from dataclasses import dataclass, field
from typing import Any

from agent_core.shared_blackboard import SharedBlackboard


@dataclass
class StructuredEntry:
    """结构化黑板条目 — 对齐 reprobe/store.py: ActivationStore 的 HDF5 条目。"""
    value: Any
    agent_name: str
    expire_at: float | None
    tags: list[str] = field(default_factory=list)
    direction: str = ""
    quality: float = 1.0
    schema_version: str = "1.0"


class StructuredBlackboard(SharedBlackboard):
    """
    结构化共享黑板 — 在 SharedBlackboard 基础上增加:
    1. 语义标签索引 — 对齐 SAE 的 feature label
    2. 方向关联 — 对齐 Steerer 的干预方向
    3. 质量评分 — 对齐 Probe 的 AUC
    """

    def __init__(self, default_ttl: float = 600.0, persist_path: str = "") -> None:
        super().__init__(default_ttl)
        self._tag_index: dict[str, set[str]] = {}
        self._direction_index: dict[str, set[str]] = {}
        self._persist_path = persist_path

    async def put_structured(
        self,
        key: str,
        value: Any,
        agent_name: str = "",
        ttl: float | None = None,
        tags: list[str] | None = None,
        direction: str = "",
        quality: float = 1.0,
    ) -> None:
        """写入结构化条目"""
        await self.put(key, value, agent_name, ttl)
        if tags:
            for tag in tags:
                if tag not in self._tag_index:
                    self._tag_index[tag] = set()
                self._tag_index[tag].add(key)
        if direction:
            if direction not in self._direction_index:
                self._direction_index[direction] = set()
            self._direction_index[direction].add(key)

    async def query_by_tag(self, tag: str) -> list[dict]:
        """按标签查询 — 对齐 SAE 的 feature lookup。"""
        keys = self._tag_index.get(tag, set())
        results = []
        for key in keys:
            entry = await self.get_with_meta(key)
            if entry:
                results.append({"key": key, **entry})
        return results

    async def query_by_direction(self, direction_name: str) -> list[dict]:
        """按方向查询 — 对齐 Steerer 的方向关联。"""
        keys = self._direction_index.get(direction_name, set())
        results = []
        for key in keys:
            entry = await self.get_with_meta(key)
            if entry:
                results.append({"key": key, **entry})
        return results

    async def cleanup_expired(self) -> int:
        """清理过期条目并同步清理 tag/direction 索引。"""
        cleaned = await super().cleanup_expired()
        if cleaned == 0:
            return 0

        alive_keys = set(await self.keys())

        stale_tags = []
        for tag, keys in self._tag_index.items():
            before = len(keys)
            keys.intersection_update(alive_keys)
            if before > 0 and len(keys) == 0:
                stale_tags.append(tag)
        for tag in stale_tags:
            del self._tag_index[tag]

        stale_dirs = []
        for direction, keys in self._direction_index.items():
            before = len(keys)
            keys.intersection_update(alive_keys)
            if before > 0 and len(keys) == 0:
                stale_dirs.append(direction)
        for direction in stale_dirs:
            del self._direction_index[direction]

        return cleaned

    async def merge_from(self, other: "StructuredBlackboard") -> int:
        """
        合并另一个黑板的条目 — 对齐 jlens/lens.py: JacobianLens.merge()。
        不存在的 key 直接导入，已存在的保留原值。同时合并标签和方向索引。
        """
        merged_count = 0
        other_keys = await other.keys()
        for key in other_keys:
            val = await other.get(key)
            if val is not None:
                existing = await self.get(key)
                if existing is None:
                    await self.put(key, val)
                    merged_count += 1
        if isinstance(other, StructuredBlackboard):
            for tag, keys in other._tag_index.items():
                if tag not in self._tag_index:
                    self._tag_index[tag] = set()
                self._tag_index[tag].update(keys)
            for direction, keys in other._direction_index.items():
                if direction not in self._direction_index:
                    self._direction_index[direction] = set()
                self._direction_index[direction].update(keys)
        return merged_count