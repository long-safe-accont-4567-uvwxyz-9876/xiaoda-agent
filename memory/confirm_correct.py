"""Confirm/Correct 机制 — 记忆强化与纠正

confirm: 确认强化（FSRS-DSR reinforce + access_count+1, edges+0.25）
correct: 纠正超驰（创建新节点，关闭旧节点，迁移边，建立 supersedes 链）
"""
import hashlib
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from loguru import logger
from memory.fsrs_model import FSRSModel, MemoryState, MemoryPhase, ReinforcementSignal

# 命名空间前缀避免跨模块 hash 冲突；使用 SHA-256 前 16 字符（64 位空间）
# 修复 P0：MD5[:12] 仅 48 位空间，约 16M 条目内必然碰撞，导致 UPSERT 覆盖原节点。
_NODE_ID_PREFIX = "cc"
_NODE_HASH_SALT = b"xiaoda-confirm-correct-v2"

_SH_TZ = ZoneInfo("Asia/Shanghai")


class ConfirmCorrect:
    """confirm: 确认强化 / correct: 纠正超驰"""

    EDGE_BOOST = 0.25

    def __init__(self, concept_db, spreading_engine, memory_db,
                 key_extractor):
        self.db = concept_db
        self.engine = spreading_engine
        self.memory = memory_db
        self.ke = key_extractor
        self._fsrs = FSRSModel()

    def _now_iso(self) -> str:
        return datetime.now(_SH_TZ).isoformat()

    def _clean_text(self, text: str) -> str:
        return text.strip()

    def _make_node_id(self, text: str) -> str:
        """生成节点 ID。

        使用 SHA-256 + 模块级 salt，取前 16 字符（64 位空间）。
        相比原 MD5[:12]（48 位），碰撞概率从 ~16M 降到 ~10^19。
        """
        cleaned = self._clean_text(text)
        h = hashlib.sha256(_NODE_HASH_SALT + cleaned.encode("utf-8")).hexdigest()[:16]
        return f"{_NODE_ID_PREFIX}_{h}"

    async def confirm(self, node_ids: list[str]) -> dict:
        """确认强化

        1. FSRS-DSR reinforce (STRONG_CONFIRM → S增长, D降低)
        2. access_count += 1
        3. peak_weight = max(peak_weight, weight)
        4. last_accessed = now
        5. 所有关联边 weight += 0.25 (双向同步)
        6. 同步 episodic_memories FSRS 状态
        """
        now_iso = self._now_iso()
        now_ts = time.time()
        reinforced = 0
        unknown = 0

        for nid in node_ids:
            node = await self.db.get_node(nid)
            if node is None:
                unknown += 1
                continue

            new_access = node["access_count"] + 1

            # FSRS-DSR reinforce
            _created_at = node.get("created_at", 0.0)
            if _created_at == 0.0:
                _created_iso = node.get("created", "")
                if _created_iso:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(_created_iso)
                        _created_at = dt.timestamp()
                    except (ValueError, TypeError):
                        _created_at = 0.0
            state = MemoryState(
                difficulty=node.get("difficulty", 5.0),
                stability=node.get("stability", 3.0),
                # 使用 safe() 防止非法 phase 值导致 ValueError 中断整个循环
                phase=MemoryPhase.safe(node.get("phase", "buffer")),
                last_review=node.get("last_review", 0.0) or now_ts,
                created_at=_created_at if _created_at > 0.0 else now_ts,
                reinforcement_count=node.get("reinforcement_count", 0),
            )
            new_state = self._fsrs.reinforce(
                state, ReinforcementSignal.STRONG_CONFIRM, now=now_ts)

            # weight 由 FSRS R 驱动：R=1 → weight=1, R→0 → weight→0
            R = new_state.retrievability(now_ts)
            new_weight = min(1.0, R)
            new_peak = max(node.get("peak_weight", 1.0), new_weight)

            await self.db.update_node(
                nid, access_count=new_access, weight=new_weight,
                peak_weight=new_peak, last_accessed=now_iso,
                difficulty=new_state.difficulty,
                stability=new_state.stability,
                phase=new_state.phase.value,
                last_review=now_ts,
                reinforcement_count=new_state.reinforcement_count)

            # 强化所有关联边（双向同步）
            edges = await self.db.get_edges(nid)
            for target_id, edge in edges.items():
                new_edge_w = min(1.0, edge["weight"] + self.EDGE_BOOST)
                await self.db.update_edge(nid, target_id, weight=new_edge_w)
                await self.db.update_edge(target_id, nid, weight=new_edge_w)

            # 同步 episodic_memories FSRS 状态
            if node.get("source_mem_id"):
                try:
                    await self.memory.update_fsrs_state(
                        node["source_mem_id"],
                        difficulty=new_state.difficulty,
                        stability=new_state.stability,
                        phase=new_state.phase.value,
                        last_review=now_ts,
                        reinforcement_count=new_state.reinforcement_count)
                except Exception as e:
                    logger.debug("confirm.sync_episodic_failed",
                                 error=str(e))

            reinforced += 1

        # G13: 失效扩散激活 recall 缓存（节点权重/边已变更）
        if getattr(self, 'engine', None) and self.engine:
            self.engine.clear_cache()

        return {"reinforced": reinforced, "unknown": unknown}

    async def correct(self, old_hint: str, new_text: str) -> dict:
        """纠正超驰（融合而非擦除）

        1. recall 找到最匹配旧记忆
        2. 验证匹配质量（共享 ≥2 token 或覆盖 ≥50%）
        3. 创建新节点（继承权重, confidence×0.7）
        4. 迁移旧节点的知识边到新节点（不迁移 supersedes 边）
        5. 建立双向 supersedes/superseded-by 边 (weight=0.5)
        6. 关闭旧节点 (valid_to = now, superseded_by = new_id)
        7. 保留 history 溯源链
        """
        # 1. 找到旧记忆
        results = await self.engine.recall(old_hint, top_k=1)
        if not results:
            return {"error": "no match"}

        old_id = results[0]["id"]
        old_node = await self.db.get_node(old_id)
        if not old_node:
            return {"error": "node not found"}
        old_text = old_node["text"]

        # 2. 验证匹配质量
        hint_tokens = set(self.ke.extract(old_hint))
        node_tokens = set(self.ke.extract(old_text))
        shared = hint_tokens & node_tokens
        if not (len(shared) >= 2 or
                (node_tokens and len(shared) / len(node_tokens) >= 0.5)):
            return {"error": "insufficient match quality"}

        # 3. 创建新节点
        now = self._now_iso()
        new_id = self._make_node_id(new_text)
        lowered_conf = round(old_node.get("confidence", 1.0) * 0.7, 3)

        history = json.loads(old_node.get("history", "[]"))
        history.append({"text": old_text, "replaced": now})

        new_keys = self.ke.extract(new_text, is_query=False)

        now_ts = time.time()
        await self.db.insert_node(
            id=new_id, text=self._clean_text(new_text),
            weight=old_node.get("weight", 1.0),
            peak_weight=old_node.get("peak_weight", 1.0),
            confidence=lowered_conf, access_count=0,
            keys=json.dumps(new_keys, ensure_ascii=False),
            layer="hippocampus",
            created=now, last_accessed=now,
            valid_from=now, valid_to=None,
            superseded_by=None, history=json.dumps(history, ensure_ascii=False),
            origin=json.dumps({"via": "correct"}),
            source_mem_id=old_node.get("source_mem_id"),
            difficulty=old_node.get("difficulty", 5.0),
            stability=old_node.get("stability", 3.0),
            phase="reinforced",
            last_review=now_ts,
            reinforcement_count=0,
        )

        # 4. 迁移旧节点的知识边（不迁移 supersedes 边）
        old_edges = await self.db.get_edges(old_id)
        for target_id, edge in old_edges.items():
            if edge["relation"] in ("supersedes", "superseded-by"):
                continue
            if target_id == new_id:
                continue
            await self.db.create_edge(new_id, target_id,
                                       edge["relation"], edge["weight"], now)
            await self.db.create_edge(target_id, new_id,
                                       edge["relation"], edge["weight"], now)

        # 5. supersedes 双向边
        await self.db.create_edge(new_id, old_id, "supersedes", 0.5, now)
        await self.db.create_edge(old_id, new_id, "superseded-by", 0.5, now)

        # 6. 关闭旧节点
        await self.db.update_node(old_id, valid_to=now,
                                   superseded_by=new_id)

        # G13: 失效扩散激活 recall 缓存（节点新增/关闭/边迁移）
        if getattr(self, 'engine', None) and self.engine:
            self.engine.clear_cache()

        logger.info("correct.applied", old_id=old_id, new_id=new_id)
        return {
            "old_text": old_text, "new_text": new_text,
            "old_id": old_id, "new_id": new_id,
        }