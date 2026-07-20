"""KnowledgeGraphV2 — 基于 graphiti 核心机制的时序知识图谱。

功能:
- Episode 摄入 + LLM 提取
- 事实超驰 (矛盾检测 + 时间窗口冲突解析)
- 实体演化 (替换式 summary 重写)
- 社区发现 (Task 6 扩展)
"""
import asyncio
import json
import time
import uuid
from typing import Any

from loguru import logger

from memory.knowledge_graph import KnowledgeGraph, _clean_json_response, _repair_json, _normalize_json_keys


ENTITY_EXTRACT_PROMPT_V2 = """从以下对话摘要中提取关键实体和关系，只提取最显著的3-5个。

严格输出JSON，不要添加任何其他文字。格式如下：
{{"entities": [{{"name": "实体名", "kind": "人物/游戏/地点/概念/物品", "observations": ["观察1"]}}], "relations": [{{"from_entity": "实体A", "relation_type": "关系类型", "to_entity": "实体B", "fact": "自然语言事实陈述"}}]}}

规则：
1. 只提取明确提及的实体，不要推测
2. observations 是关于实体的具体描述
3. relation_type 使用简洁的动词短语，如"喜欢"、"属于"、"住在"
4. fact 是对关系的自然语言完整陈述，如"用户喜欢打篮球"
5. 如果没有明确的实体和关系，返回 {{"entities": [], "relations": []}}

对话摘要：
{summary}"""


CONTRADICTION_PROMPT = """判断新事实是否与已有事实矛盾。

新事实: {new_fact}
已有事实: {existing_facts_list}

规则:
1. 如果新事实与已有事实表达相同含义，不算矛盾
2. 如果新事实使已有事实不再成立，算矛盾
3. 输出JSON: {{"contradicted_indices": [索引列表]}}

输出JSON:"""


SUMMARY_REWRITE_PROMPT = """你是知识压缩助手。将旧摘要和新信息融合为一条精简摘要。

旧摘要: {old_summary}
新信息: {new_observations}
实体名: {entity_name}

要求:
1. 保留所有关键事实
2. 去除冗余和重复
3. 不超过200字
4. 直接输出摘要文本，不要加任何标记"""


class KnowledgeGraphV2(KnowledgeGraph):
    """KG v2: 时序事实、实体演化、Episode溯源、社区发现。

    继承 KnowledgeGraph 以复用 _call_free_model / set_free_model_client。
    """

    def __init__(
        self,
        db_v2: Any,
        vector_store: Any = None,
        router: Any = None,
    ) -> None:
        super().__init__(db=None, knowledge_db=None, router=router)
        self._db_v2 = db_v2
        self._vector_store = vector_store
        self._conn = db_v2._conn if db_v2 else None

    async def extract_from_summary(self, summary: str) -> dict:
        """使用 V2 prompt 提取实体和关系（含 fact 字段）。"""
        if not summary:
            return {"entities": [], "relations": []}
        try:
            prompt = ENTITY_EXTRACT_PROMPT_V2.format(summary=summary[:500])
            messages = [
                {"role": "system", "content": "你是一个知识提取助手，只输出纯JSON，不要输出任何其他内容，不要用markdown代码块包裹。"},
                {"role": "user", "content": prompt},
            ]
            result = await self._call_free_model(messages, temperature=0.1, max_tokens=1024)
            if result is None and self._router:
                # 修复 P0-2（同 knowledge_graph.py）：降级路由加 8s 超时保护
                try:
                    result = await asyncio.wait_for(
                        self._router.route(
                            "memory_encoding", messages, temperature=0.1,
                            user_openid="system", session_id="kg_v2_extract",
                        ),
                        timeout=8.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("kg_v2.extract_router_timeout, fallback to empty entities")
                    return {"entities": [], "relations": []}
            if isinstance(result, str):
                cleaned = _clean_json_response(result)
                try:
                    parsed = json.loads(cleaned)
                except json.JSONDecodeError:
                    repaired = _repair_json(cleaned)
                    parsed = json.loads(repaired)
                if isinstance(parsed, list) and len(parsed) > 0:
                    parsed = parsed[0] if isinstance(parsed[0], dict) else {}
                if not isinstance(parsed, dict):
                    return {"entities": [], "relations": []}
                parsed = _normalize_json_keys(parsed)
                entities = parsed.get("entities", [])
                relations = parsed.get("relations", [])
                if not isinstance(entities, list):
                    entities = []
                if not isinstance(relations, list):
                    relations = []
                return {"entities": entities[:5], "relations": relations[:5]}
        except Exception as e:
            logger.warning("kg_v2.extract_failed", error=str(e))
        return {"entities": [], "relations": []}

    async def add_facts_from_episode(
        self,
        episode_content: str,
        episode_time: float,
        source_type: str = "summary",
    ) -> dict:
        """从 Episode 提取并合并事实。"""
        if self._db_v2 is None:
            logger.warning("kg_v2.add_facts_no_db")
            return {"episode_id": "", "new_facts": 0, "invalidated": 0}
        episode_id = f"EP-{uuid.uuid4().hex[:12]}"
        now = time.time()
        await self._db_v2.insert_episode(
            episode_id, episode_content, source_type, episode_time, now
        )

        extracted = await self.extract_from_summary(episode_content)
        if not extracted.get("entities") and not extracted.get("relations"):
            return {"episode_id": episode_id, "new_facts": 0, "invalidated": 0}

        await self.merge_entities_v2(extracted["entities"], episode_content, episode_time)

        invalidated_count = 0
        new_facts_count = 0
        for rel in extracted.get("relations", []):
            is_new, invalidated = await self.merge_relation_v2(rel, episode_id, episode_time)
            new_facts_count += int(is_new)
            invalidated_count += len(invalidated)

        return {
            "episode_id": episode_id,
            "new_facts": new_facts_count,
            "invalidated": invalidated_count,
        }

    async def merge_entities_v2(
        self,
        entities: list[dict],
        episode_content: str,
        episode_time: float,
    ) -> None:
        """实体演化: summary 替换式重写, version 递增。"""
        if self._db_v2 is None:
            logger.warning("kg_v2.merge_entities_no_db")
            return
        for ent in entities[:5]:
            try:
                name = ent.get("name", "")
                if not name:
                    continue
                kind = ent.get("kind", "")
                new_obs = ent.get("observations", [])
                existing = await self._db_v2.get_entity_v2(name)

                if existing:
                    old_summary = existing.get("summary", "")
                    if old_summary and new_obs:
                        new_summary = await self._rewrite_summary(old_summary, new_obs, name)
                    elif old_summary:
                        new_summary = old_summary
                    else:
                        new_summary = "; ".join(new_obs) if new_obs else ""

                    rowid = await self._db_v2.update_entity_summary_v2(
                        name, new_summary,
                        summary_version=existing.get("summary_version", 0) + 1,
                    )
                    # 同步向量
                    if self._vector_store and rowid:
                        await self._vector_store.upsert_kg_entity(
                            rowid, f"{name}: {new_summary}"
                        )
                else:
                    entity_id = f"ENT-{uuid.uuid4().hex[:12]}"
                    summary = "; ".join(new_obs) if new_obs else ""
                    rowid = await self._db_v2.insert_entity_v2(
                        entity_id, name, kind, new_obs, summary
                    )
                    if self._vector_store and rowid:
                        await self._vector_store.upsert_kg_entity(
                            rowid, f"{name}: {summary}"
                        )
            except Exception as e:
                logger.warning("kg_v2.merge_entity_failed", name=ent.get("name", ""), error=str(e))

    async def _rewrite_summary(
        self, old_summary: str, new_observations: list, entity_name: str
    ) -> str:
        """LLM 重写 summary。"""
        prompt = SUMMARY_REWRITE_PROMPT.format(
            old_summary=old_summary,
            new_observations=", ".join(new_observations),
            entity_name=entity_name,
        )
        messages = [{"role": "user", "content": prompt}]
        result = await self._call_free_model(messages, temperature=0.3, max_tokens=512)
        if result and isinstance(result, str):
            return result.strip()
        return f"{old_summary}; {'; '.join(new_observations)}"

    async def _ensure_episode_exists(self, episode_id: str, episode_time: float) -> None:
        """确保 episode 在 kg_episodes 中存在（ref JOIN 需要）。"""
        if self._db_v2 is None:
            return
        existing = await self._db_v2.get_episode(episode_id)
        if not existing:
            await self._db_v2.insert_episode(
                episode_id, "", "summary", episode_time, time.time()
            )

    async def merge_relation_v2(
        self,
        relation: dict,
        episode_id: str,
        episode_time: float,
    ) -> tuple[bool, list[dict]]:
        """合并新关系，自动处理超驰。Returns: (is_new, invalidated_relations)。

        事务边界：BEGIN IMMEDIATE ... COMMIT 包住「查冲突→LLM 检测→invalidate→insert」
        全流程，防止并发产生重复关系。所有内部 DB 写入使用 auto_commit=False，
        统一由外层 COMMIT 提交。失败时 ROLLBACK 保持原状。
        """
        if self._db_v2 is None:
            logger.warning("kg_v2.merge_relation_no_db")
            return False, []
        from_entity = relation.get("from_entity", "")
        relation_type = relation.get("relation_type", "")
        to_entity = relation.get("to_entity", "")
        fact = relation.get("fact", f"{from_entity} {relation_type} {to_entity}")

        if not from_entity or not relation_type or not to_entity:
            return False, []

        conn = self._conn
        # 立即获取写锁：BEGIN IMMEDIATE 在事务开始时即获取 RESERVED→EXCLUSIVE 锁，
        # 阻止其他写入者并发执行 merge，避免重复关系。
        try:
            await conn.execute("BEGIN IMMEDIATE")
        except Exception as e:
            # aiosqlite 在已处于事务中时再 BEGIN 会报错；这里降级为隐式事务
            logger.debug("kg_v2.merge_begin_failed_using_implicit", error=str(e))

        try:
            # 搜索潜在冲突：同一 from_entity + relation_type 的当前有效关系
            # （to_entity 可能不同，如"用户喜欢篮球" vs "用户喜欢网球"）
            conflict_candidates = await self._db_v2.get_active_relations_by_subject_and_type(
                from_entity, relation_type
            )

            invalidated: list[dict] = []
            is_duplicate = False

            if conflict_candidates:
                # 精确匹配检查（去重）
                for candidate in conflict_candidates:
                    if candidate.get("fact", "") == fact:
                        is_duplicate = True
                        await self._ensure_episode_exists(episode_id, episode_time)
                        await self._db_v2.append_episode_ref(
                            candidate["id"], episode_id, auto_commit=False,
                        )
                        break

                # LLM 矛盾检测
                if not is_duplicate:
                    contradictions = await self._detect_contradictions(
                        new_fact=fact,
                        existing_facts=[r.get("fact", "") for r in conflict_candidates],
                    )
                    for idx in contradictions:
                        if idx < len(conflict_candidates):
                            candidate = conflict_candidates[idx]
                            if self._resolve_contradiction(candidate, episode_time):
                                await self._db_v2.invalidate_relation(
                                    candidate["id"],
                                    invalid_at=candidate["invalid_at"],
                                    expired_at=candidate.get("expired_at", time.time()),
                                    auto_commit=False,
                                )
                                invalidated.append(candidate)

            # 插入新关系
            if not is_duplicate:
                rel_id = f"REL-{uuid.uuid4().hex[:12]}"
                rowid = await self._db_v2.insert_relation_v2(
                    rel_id, from_entity, relation_type, to_entity, fact,
                    episode_id, episode_time, auto_commit=False,
                )
                # 同步事实向量（向量库独立于 SQLite 事务，无法回滚；
                # 即使后续 commit 失败也只多一条孤儿向量，可由后台对账修复）
                if self._vector_store and rowid:
                    try:
                        await self._vector_store.upsert_kg_relation(rowid, fact)
                    except Exception as e:
                        logger.warning("kg_v2.vec_upsert_failed_during_txn",
                                       rowid=rowid, error=str(e))

            await conn.commit()
            return not is_duplicate, invalidated
        except Exception as e:
            try:
                await conn.rollback()
            except Exception as rb_err:
                logger.debug("kg_v2.merge_rollback_failed", error=str(rb_err))
            logger.warning("kg_v2.merge_relation_failed_rolled_back", error=str(e))
            raise

    async def _detect_contradictions(
        self, new_fact: str, existing_facts: list[str]
    ) -> list[int]:
        """LLM 矛盾检测，返回被矛盾的已有事实索引列表。"""
        if not existing_facts:
            return []
        try:
            facts_list = "\n".join(
                f"{i}. {f}" for i, f in enumerate(existing_facts)
            )
            prompt = CONTRADICTION_PROMPT.format(
                new_fact=new_fact, existing_facts_list=facts_list
            )
            messages = [{"role": "user", "content": prompt}]
            result = await self._call_free_model(messages, temperature=0.0, max_tokens=200)
            if result and isinstance(result, str):
                cleaned = _clean_json_response(result)
                parsed = json.loads(cleaned)
                indices = parsed.get("contradicted_indices", [])
                if isinstance(indices, list):
                    # int(i) 对非数字字符串会抛 ValueError；逐个 try/except 跳过非法值
                    # （LLM 偶尔返回字符串索引如 "0" 或 null，不应让整次检测崩溃）
                    valid: list[int] = []
                    for i in indices:
                        try:
                            idx = int(i)
                        except (ValueError, TypeError):
                            continue
                        if 0 <= idx < len(existing_facts):
                            valid.append(idx)
                    return valid
        except Exception as e:
            logger.debug("kg_v2.detect_contradictions_failed", error=str(e))
        return []

    def _resolve_contradiction(
        self, old_relation: dict, new_valid_at: float
    ) -> bool:
        """时间窗口冲突解析。返回是否标记了旧关系失效。"""
        old_valid_at = old_relation.get("valid_at") or 0
        old_invalid_at = old_relation.get("invalid_at")

        if old_invalid_at and old_invalid_at <= new_valid_at:
            return False

        if old_valid_at < new_valid_at:
            old_relation["invalid_at"] = new_valid_at
            old_relation["expired_at"] = time.time()
            old_relation["is_current"] = 0
            return True

        return False

    # ── 社区发现 ──────────────────────────────────────────────

    async def detect_communities(self) -> list[list[str]]:
        """社区发现: 加载图投影 → 标签传播 → 生成社区摘要。"""
        if self._conn is None:
            logger.warning("kg_v2.detect_communities_no_conn")
            return []
        cursor = await self._conn.execute("""
            SELECT from_entity, to_entity, COUNT(*) as edge_count
            FROM kg_relations_v2
            WHERE is_current = 1
            GROUP BY from_entity, to_entity
        """)
        rows = await cursor.fetchall()

        adjacency: dict[str, list[tuple[str, int]]] = {}
        for row in rows:
            f, t, cnt = row[0], row[1], row[2]
            adjacency.setdefault(f, []).append((t, cnt))
            adjacency.setdefault(t, []).append((f, cnt))

        if not adjacency:
            return []

        clusters = self._label_propagation(adjacency, max_iter=10)

        for cluster in clusters:
            if len(cluster) > 1:
                await self._build_community_summary(cluster)

        return clusters

    def _label_propagation(
        self,
        adjacency: dict[str, list[tuple[str, int]]],
        max_iter: int = 10,
    ) -> list[list[str]]:
        """标签传播算法: 纯 Python 内存计算。"""
        if not adjacency:
            return []
        labels = {node: i for i, node in enumerate(adjacency)}

        for _ in range(max_iter):
            no_change = True
            for node in adjacency:
                neighbor_labels: dict[int, int] = {}
                for neighbor, edge_count in adjacency[node]:
                    lbl = labels[neighbor]
                    neighbor_labels[lbl] = neighbor_labels.get(lbl, 0) + edge_count

                if not neighbor_labels:
                    continue

                best_label = max(neighbor_labels, key=neighbor_labels.get)
                if neighbor_labels[best_label] >= 1 and labels[node] != best_label:
                    labels[node] = best_label
                    no_change = False

            if no_change:
                break

        communities: dict[int, list[str]] = {}
        for node, lbl in labels.items():
            communities.setdefault(lbl, []).append(node)
        return list(communities.values())

    async def _build_community_summary(self, member_names: list[str]) -> None:
        """为社区生成摘要并写入 kg_communities 表。"""
        if self._conn is None or self._db_v2 is None:
            logger.warning("kg_v2.build_community_summary_no_conn")
            return
        placeholders = ",".join("?" * len(member_names))
        cursor = await self._conn.execute(
            f"SELECT name, summary FROM kg_entities_v2 WHERE name IN ({placeholders}) AND summary != ''",
            member_names,
        )
        rows = await cursor.fetchall()

        if not rows:
            return

        summaries = [r[1] for r in rows]
        if len(summaries) <= 4:
            combined = "; ".join(summaries)
        else:
            # 简单截断, 避免过多 LLM 调用
            combined = "; ".join(summaries[:4])

        community_id = f"COM-{uuid.uuid4().hex[:12]}"
        name = await self._generate_community_name(combined)
        await self._db_v2.insert_community(community_id, name, combined, member_names)

    async def _generate_community_name(self, combined_summary: str) -> str:
        """LLM 生成社区名称。"""
        prompt = f"根据以下信息生成一个简短的社区名称（不超过10个字）:\n{combined_summary[:200]}\n\n直接输出名称:"
        messages = [{"role": "user", "content": prompt}]
        result = await self._call_free_model(messages, temperature=0.3, max_tokens=50)
        if result and isinstance(result, str):
            return result.strip()[:20]
        return "未命名社区"

    async def update_community_for_entity(self, entity_name: str) -> None:
        """增量更新: 新增实体后, 查邻居社区归属, 取众数归入。"""
        if self._conn is None or self._db_v2 is None:
            logger.warning("kg_v2.update_community_no_conn")
            return
        cursor = await self._conn.execute(
            """SELECT r.from_entity, r.to_entity FROM kg_relations_v2 r
               WHERE r.is_current = 1 AND (r.from_entity = ? OR r.to_entity = ?)""",
            [entity_name, entity_name],
        )
        rows = await cursor.fetchall()

        neighbor_names = set()
        for row in rows:
            neighbor_names.add(row[0] if row[1] == entity_name else row[1])

        community_votes: dict[str, int] = {}
        for neighbor in neighbor_names:
            comm = await self._db_v2.get_entity_community(neighbor)
            if comm:
                community_votes[comm] = community_votes.get(comm, 0) + 1

        if community_votes:
            best = max(community_votes, key=community_votes.get)
            await self._db_v2.add_entity_to_community(entity_name, best)
