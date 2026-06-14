import json
import re
import time
from loguru import logger

from db.db_knowledge import KnowledgeDB


ENTITY_EXTRACT_PROMPT = """从以下对话摘要中提取关键实体和关系，只提取最显著的3-5个。

严格输出JSON，不要添加任何其他文字。格式如下：
{"entities": [{"name": "实体名", "kind": "人物/游戏/地点/概念/物品", "observations": ["观察1"]}], "relations": [{"from_entity": "实体A", "relation_type": "关系类型", "to_entity": "实体B"}]}

规则：
1. 只提取明确提及的实体，不要推测
2. observations 是关于实体的具体描述
3. relation_type 使用简洁的动词短语，如"喜欢"、"属于"、"住在"
4. 如果没有明确的实体和关系，返回 {"entities": [], "relations": []}

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


def _normalize_json_keys(obj):
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            clean_key = k.strip().strip('"').strip("'").strip()
            while clean_key.startswith('"') or clean_key.startswith("'"):
                clean_key = clean_key[1:]
            while clean_key.endswith('"') or clean_key.endswith("'"):
                clean_key = clean_key[:-1]
            clean_key = clean_key.strip()
            cleaned[clean_key] = _normalize_json_keys(v)
        return cleaned
    elif isinstance(obj, list):
        return [_normalize_json_keys(item) for item in obj]
    return obj


class KnowledgeGraph:

    MAX_ENTITIES = 500
    CLEANUP_AGE_DAYS = 30

    def __init__(self, db=None, knowledge_db: KnowledgeDB | None = None, router=None):
        self._db = db
        self.knowledge_db = knowledge_db
        self._router = router

    def set_db(self, db):
        self._db = db

    def set_knowledge_db(self, knowledge_db: KnowledgeDB):
        self.knowledge_db = knowledge_db

    def set_router(self, router):
        self._router = router

    async def extract_from_summary(self, summary: str) -> dict:
        if not self._router or not summary:
            return {"entities": [], "relations": []}

        try:
            prompt = ENTITY_EXTRACT_PROMPT.format(summary=summary[:500])
            messages = [
                {"role": "system", "content": "你是一个知识提取助手，只输出纯JSON，不要输出任何其他内容，不要用markdown代码块包裹。"},
                {"role": "user", "content": prompt},
            ]
            result = await self._router.route(
                "memory_encoding",
                messages,
                temperature=0.1,
                user_openid="system",
                session_id="kg_extract",
            )
            if isinstance(result, str):
                cleaned = _clean_json_response(result)
                parsed = json.loads(cleaned)
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

    async def merge_entities(self, entities: list[dict]):
        if not self.knowledge_db or not entities:
            return

        for ent in entities[:5]:
            try:
                await self.knowledge_db.merge_entity(ent)
            except Exception as e:
                logger.warning("kg.merge_entity_failed", name=ent.get("name", ""), error=str(e))

    async def merge_relations(self, relations: list[dict]):
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

    async def cleanup_stale(self):
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
            return 0

    async def auto_extract_and_merge(self, summary: str):
        if not summary:
            return

        entity_count = await self.get_entity_count()
        if entity_count > self.MAX_ENTITIES:
            await self.cleanup_stale()

        extracted = await self.extract_from_summary(summary)
        if extracted.get("entities"):
            await self.merge_entities(extracted["entities"])
        if extracted.get("relations"):
            await self.merge_relations(extracted["relations"])
