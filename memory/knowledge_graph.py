from typing import Any
import asyncio
import json
import re
import time
from loguru import logger

from db.db_knowledge import KnowledgeDB


ENTITY_EXTRACT_PROMPT = """从以下对话摘要中提取关键实体和关系，只提取最显著的3-5个。

严格输出JSON，不要添加任何其他文字。格式如下：
{{"entities": [{{"name": "实体名", "kind": "人物/游戏/地点/概念/物品", "observations": ["观察1"]}}], "relations": [{{"from_entity": "实体A", "relation_type": "关系类型", "to_entity": "实体B"}}]}}

规则：
1. 只提取明确提及的实体，不要推测
2. observations 是关于实体的具体描述
3. relation_type 使用简洁的动词短语，如"喜欢"、"属于"、"住在"
4. 如果没有明确的实体和关系，返回 {{"entities": [], "relations": []}}

对话摘要：
{summary}"""


def _clean_json_response(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    brace_start = text.find('{')
    if brace_start > 0:
        text = text[brace_start:]
    brace_end = text.rfind('}')
    if brace_end >= 0 and brace_end < len(text) - 1:
        text = text[:brace_end + 1]
    return text


def _repair_json(text: str) -> str:
    """修复 LLM 输出中常见的 JSON 语法错误。"""
    # 修复多余逗号: },, → },
    text = re.sub(r'},\s*,', '},', text)
    text = re.sub(r',\s*,', ',', text)
    # 修复缺少逗号: "key":"val" "key2" → "key":"val","key2"
    text = re.sub(r'"\s+(")', r',\1', text)
    # 修复 } 后面缺少逗号直接跟 { : }{ → },{
    text = re.sub(r'}\s*{', '},{', text)
    # 修复 ] 后面缺少逗号直接跟 { : ]{ → ],{
    return re.sub(r'\]\s*{', '],{', text)


def _normalize_json_keys(obj: Any) -> Any:
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            clean_key = k.strip().strip('"').strip("'").strip()
            while clean_key.startswith(('"', "'")):
                clean_key = clean_key[1:]
            while clean_key.endswith(('"', "'")):
                clean_key = clean_key[:-1]
            clean_key = clean_key.strip()
            cleaned[clean_key] = _normalize_json_keys(v)
        return cleaned
    if isinstance(obj, list):
        return [_normalize_json_keys(item) for item in obj]
    return obj


class KnowledgeGraph:
    """管理知识图谱实体与关系的存取、检索与清理。"""

    MAX_ENTITIES = 500
    CLEANUP_AGE_DAYS = 30
    # 修复 P0-2：query 实体提取结果缓存（LRU + TTL）
    # 根因：同一 query 在主检索路径中可能被调用 3 次（fast path L1310、complex path L1379、
    # KG 召回通道 L686 via recall_by_query），每次都触发一次 LLM 调用，最坏 30s × 3 = 90s 阻塞。
    # LRU 缓存让同一 query 只调一次 LLM，后续命中缓存 <1ms。
    QUERY_ENTITY_CACHE_TTL = 300  # 5 分钟，与 query_cache 对齐
    QUERY_ENTITY_CACHE_MAX = 256  # 上限 256 条，避免内存膨胀

    def __init__(self, db: Any=None, knowledge_db: KnowledgeDB | None = None, router: Any=None) -> None:
        self._db = db
        self.knowledge_db = knowledge_db
        self._router = router
        # P0-2: query → (entities_set, expire_timestamp)
        self._query_entity_cache: dict[str, tuple[set[str], float]] = {}
        # P0-2: 简单的 LRU 顺序记录（按访问时间），用于淘汰
        self._query_entity_lru: list[str] = []

    def set_db(self, db: Any) -> None:
        self._db = db

    def set_knowledge_db(self, knowledge_db: KnowledgeDB) -> None:
        self.knowledge_db = knowledge_db

    def set_router(self, router: Any) -> None:
        self._router = router

    def set_free_model_client(self, api_key: str, base_url: str, model: str) -> None:
        """配置硅基流动免费模型客户端，用于知识提取（不占用主模型配额）"""
        self._free_api_key = api_key
        self._free_base_url = base_url
        self._free_model = model

    async def _call_free_model(self, messages: list, temperature: float = 0.1,
                                max_tokens: int = 800) -> str | None:
        """调用硅基流动免费模型"""
        if not getattr(self, '_free_api_key', ''):
            return None
        import httpx
        from utils.http_pool import get_shared_client
        try:
            # 修复 P0-2：timeout 从 30s → 10s
            # 根因：实体提取是检索路径的阻塞点，30s 超时会让单次检索最坏阻塞 30s。
            # 10s 足够 GLM-4-9B-0414 完成实体提取（正常 1-3s），超时则降级到 router 或返回空。
            # G4: 共享 httpx.AsyncClient（连接池复用 + HTTP/2），单次请求级别覆盖 timeout
            client = get_shared_client()
            response = await client.post(
                f"{self._free_base_url}/chat/completions",
                json={
                    "model": self._free_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {self._free_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(10.0),
            )
            response.raise_for_status()
            data = response.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning("kg.free_model_failed", error=str(e))
            return None

    async def extract_from_summary(self, summary: str) -> dict:
        if not summary:
            return {"entities": [], "relations": []}

        try:
            prompt = ENTITY_EXTRACT_PROMPT.format(summary=summary[:500])
            messages = [
                {"role": "system", "content": "你是一个知识提取助手，只输出纯JSON，不要输出任何其他内容，不要用markdown代码块包裹。"},
                {"role": "user", "content": prompt},
            ]
            # 优先使用免费模型，降级到主路由
            result = await self._call_free_model(messages, temperature=0.1, max_tokens=1024)
            if result is None and self._router:
                # 修复 P0-2：降级路由加 8s 超时保护
                # 根因：原代码 router.route 无超时，主模型卡住会让实体提取无限阻塞，
                # 进而阻塞整个记忆检索流程（kg.get_query_entities 在主检索路径中被同步等待）。
                # 8s 超时：与 _call_free_model(10s) 互补，总最长 18s 后强制降级返回空实体。
                try:
                    result = await asyncio.wait_for(
                        self._router.route(
                            "memory_encoding",
                            messages,
                            temperature=0.1,
                            user_openid="system",
                            session_id="kg_extract",
                        ),
                        timeout=8.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("kg.extract_router_timeout, fallback to empty entities")
                    return {"entities": [], "relations": []}
            if isinstance(result, str):
                cleaned = _clean_json_response(result)
                try:
                    parsed = json.loads(cleaned)
                except json.JSONDecodeError:
                    # 第一次解析失败，尝试修复 JSON 后重新解析
                    repaired = _repair_json(cleaned)
                    parsed = json.loads(repaired)
                if isinstance(parsed, list) and len(parsed) > 0:
                    parsed = parsed[0] if isinstance(parsed[0], dict) else {}
                if not isinstance(parsed, dict):
                    return {"entities": [], "relations": []}
                parsed = _normalize_json_keys(parsed)
                try:
                    entities = parsed.get("entities", [])
                    relations = parsed.get("relations", [])
                except (KeyError, TypeError):
                    entities = []
                    relations = []
                if not isinstance(entities, list):
                    entities = []
                if not isinstance(relations, list):
                    relations = []
                return {
                    "entities": entities[:5],
                    "relations": relations[:5],
                }
        except json.JSONDecodeError as e:
            logger.debug("kg.extract_json_error", error=str(e), raw=result[:200] if isinstance(result, str) else "")
        except Exception as e:
            logger.warning("kg.extract_failed", error=str(e))

        return {"entities": [], "relations": []}

    async def merge_entities(self, entities: list[dict]) -> None:
        if not self.knowledge_db or not entities:
            return

        for ent in entities[:5]:
            try:
                await self.knowledge_db.merge_entity(ent)
            except Exception as e:
                logger.warning("kg.merge_entity_failed", name=ent.get("name", ""), error=str(e))

    async def merge_relations(self, relations: list[dict]) -> None:
        if not self.knowledge_db or not relations:
            return

        for rel in relations[:5]:
            try:
                await self.knowledge_db.merge_relation(rel)
            except Exception as e:
                logger.warning("kg.merge_relation_failed", error=str(e))

    async def get_related_knowledge(self, entity_names: list[str], depth: int = 1) -> list[dict]:
        if not self.knowledge_db or not entity_names:
            return []

        try:
            result = await self.knowledge_db.get_related_knowledge(entity_names[:5], depth)
            items = []
            for ent in result.get("entities", []):
                obs = ent.get("observations", [])
                if isinstance(obs, str):
                    try:
                        obs = json.loads(obs)
                    except (json.JSONDecodeError, TypeError):
                        obs = []
                ent["observations"] = obs
                items.append({"type": "entity", "data": ent})
            for rel in result.get("relations", []):
                items.append({"type": "relation", "data": rel})
            return items
        except Exception as e:
            logger.warning("kg.get_related_failed", error=str(e))
            return []

    async def get_relevance_boost(self, query: str, memory_summaries: list[str]) -> list[float]:
        """基于知识图谱的检索增强评分"""
        boosts = []

        query_entities = set()
        try:
            entities = await self.extract_from_summary(query)
            for ent in entities.get("entities", []):
                query_entities.add(ent.get("name", ""))
        except Exception as e:
            logger.debug("kg.query_entities_failed", error=str(e))

        for summary in memory_summaries:
            boost = 0.0
            summary_entities = set()
            try:
                entities = await self.extract_from_summary(summary)
                for ent in entities.get("entities", []):
                    summary_entities.add(ent.get("name", ""))
            except Exception as e:
                logger.debug("kg.summary_entities_failed", error=str(e))

            overlap = query_entities & summary_entities
            if overlap:
                boost += len(overlap) * 0.15

            if self.knowledge_db and query_entities and summary_entities:
                try:
                    for qe in list(query_entities)[:3]:
                        for se in list(summary_entities)[:3]:
                            relations = await self.knowledge_db.get_knowledge_relations(qe)
                            for rel in relations[:5]:
                                if rel.get("to_entity") == se or rel.get("from_entity") == se:
                                    boost += 0.05
                                    break
                except Exception as e:
                    logger.debug("kg.relation_boost_failed", error=str(e))

            boosts.append(min(boost, 0.5))

        return boosts

    async def get_query_entities(self, query: str) -> set[str]:
        """提取 query 实体（单次 LLM 调用）。

        I6: 供召回通道和快速评分共用，避免 N+1 次 LLM 调用。
        修复 P0-2: 加 LRU + TTL 缓存，同一 query 5 分钟内只调一次 LLM。
        """
        # P0-2: 缓存命中检查
        now = time.time()
        cached = self._query_entity_cache.get(query)
        if cached is not None:
            entities_set, expire_ts = cached
            if now < expire_ts:
                # 命中缓存，更新 LRU 顺序
                try:
                    self._query_entity_lru.remove(query)
                    self._query_entity_lru.append(query)
                except ValueError:
                    pass
                logger.debug("kg.query_entities_cache_hit", query=query[:50])
                return entities_set
            else:
                # 过期，清除
                self._query_entity_cache.pop(query, None)
                try:
                    self._query_entity_lru.remove(query)
                except ValueError:
                    pass

        try:
            entities = await self.extract_from_summary(query)
            result_set = {ent.get("name", "") for ent in entities.get("entities", [])
                          if ent.get("name")}
        except Exception as e:
            logger.debug("kg.query_entities_failed", error=str(e))
            result_set = set()

        # P0-2: 写入缓存（即使是空集也缓存，避免重复调用 LLM 浪费）
        self._query_entity_cache[query] = (result_set, now + self.QUERY_ENTITY_CACHE_TTL)
        self._query_entity_lru.append(query)
        # LRU 淘汰
        while len(self._query_entity_lru) > self.QUERY_ENTITY_CACHE_MAX:
            oldest = self._query_entity_lru.pop(0)
            self._query_entity_cache.pop(oldest, None)
        return result_set

    async def get_relevance_boost_fast(self, query_entities: set[str],
                                         memory_entities_list: list[list[str]]) -> list[float]:
        """快速 KG 评分 — 复用已存储的 entities 字段，不再 N+1 次 LLM 调用。

        I6: 修复 get_relevance_boost 的性能黑洞（原实现每条记忆都调 extract_from_summary）。
        """
        boosts: list[float] = []
        for mem_entities in memory_entities_list:
            boost = 0.0
            mem_set = set(mem_entities)
            overlap = query_entities & mem_set
            if overlap:
                boost += len(overlap) * 0.15
            # 关系增强：query 实体与记忆实体在 KG 中是否有边
            if self.knowledge_db and query_entities and mem_set:
                try:
                    for qe in list(query_entities)[:3]:
                        relations = await self.knowledge_db.get_knowledge_relations(qe)
                        for rel in relations[:5]:
                            if (rel.get("to_entity") in mem_set
                                    or rel.get("from_entity") in mem_set):
                                boost += 0.05
                                break
                except Exception:
                    logger.debug("kg.relation_boost_fast_failed: {}", exc_info=True)
            boosts.append(min(boost, 0.5))
        return boosts

    async def recall_by_entities(self, query_entities: set[str],
                                   limit: int = 5) -> list[str]:
        """KG 召回: query 实体 → KG 关联实体 → 返回关联实体名列表。

        I6: 供 memory_manager 反查 episodic_memories.entities 字段，
        让 KG 真正参与召回候选池（而非仅后置评分）。
        """
        if not query_entities or not self.knowledge_db:
            return []
        try:
            related = await self.get_related_knowledge(
                list(query_entities)[:3], depth=1)
            related_names: set[str] = set()
            for item in related:
                if item["type"] == "entity":
                    related_names.add(item["data"].get("name", ""))
                elif item["type"] == "relation":
                    related_names.add(item["data"].get("from_entity", ""))
                    related_names.add(item["data"].get("to_entity", ""))
            # 排除 query 自身实体，只返回关联实体
            return list(related_names - query_entities)[:limit]
        except Exception as e:
            logger.debug("kg.recall_failed", error=str(e))
            return []

    async def recall_by_query(self, query: str, limit: int = 50) -> list[str]:
        """一站式 KG 召回：提取实体 → 关联召回 → 返回实体名称列表

        Args:
            query: 用户查询
            limit: 最大返回实体数
        Returns:
            关联实体名称列表（用于反查记忆）
        """
        entities = await self.get_query_entities(query)
        if not entities:
            return []
        return await self.recall_by_entities(entities, limit=limit)

    async def format_knowledge_context(self, knowledge: list[dict]) -> str:
        if not knowledge:
            return ""

        parts = []
        for item in knowledge:
            if item["type"] == "entity":
                ent = item["data"]
                obs = ent.get("observations", [])
                obs_str = "；".join(str(o) for o in obs[:3]) if obs else ""
                parts.append(f"{ent['name']}({ent.get('kind', '')}): {obs_str}")
            elif item["type"] == "relation":
                rel = item["data"]
                parts.append(f"{rel['from_entity']} → {rel['relation_type']} → {rel['to_entity']}")

        return "[知识图谱]\n" + "\n".join(parts[:8])

    async def cleanup_stale(self) -> None:
        if not self.knowledge_db:
            return
        try:
            count = await self.knowledge_db.cleanup_stale(self.CLEANUP_AGE_DAYS)
            if count:
                logger.info("kg.cleanup", deleted=count)
        except Exception as e:
            logger.warning("kg.cleanup_failed", error=str(e))

    async def get_entity_count(self) -> int:
        if not self.knowledge_db:
            return 0
        try:
            return await self.knowledge_db.get_entity_count()
        except Exception:
            logger.debug("kg.get_entity_count_failed: {}", exc_info=True)
            return 0

    def set_kg_v2(self, kg_v2: Any) -> None:
        """注入 KnowledgeGraphV2 实例。"""
        self._kg_v2 = kg_v2

    async def auto_extract_and_merge(self, summary: str) -> None:
        if not summary:
            return

        # KG v2 分支: 功能开关开启时走 v2 路径
        try:
            import config as _cfg
            if getattr(_cfg, 'KG_V2_ENABLED', False) and getattr(self, '_kg_v2', None):
                try:
                    await self._kg_v2.add_facts_from_episode(summary, time.time())
                    return
                except Exception as e:
                    logger.warning("kg.v2_extract_failed_fallback_to_v1", error=str(e))
        except Exception:
            logger.debug("kg.v2_extract_fatal_fallback_to_v1")

        # v1 逻辑 (原有代码)
        entity_count = await self.get_entity_count()
        if entity_count > self.MAX_ENTITIES:
            await self.cleanup_stale()

        # OntoLearner B1: 复杂度评分, 跳过高复杂度摘要的 KG 提取
        # 论文实证: 失败模式与本体复杂度正相关 (非模型大小)
        try:
            from memory.ontology_complexity import should_extract
            import config as _cfg
            _threshold = float(getattr(_cfg, "ONTOLOGY_SKIP_THRESHOLD", 0.75))
            _do_extract, _score = should_extract(summary, skip_threshold=_threshold)
            if not _do_extract:
                logger.debug("kg.skip_complex_summary",
                             total=round(_score.total, 3),
                             detail=_score.detail)
                return
        except Exception as e:
            logger.debug("kg.complexity_check_failed", error=str(e))

        extracted = await self.extract_from_summary(summary)
        if extracted.get("entities"):
            await self.merge_entities(extracted["entities"])
        if extracted.get("relations"):
            await self.merge_relations(extracted["relations"])
