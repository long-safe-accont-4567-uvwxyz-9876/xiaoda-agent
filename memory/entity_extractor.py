"""EntityExtractor — 混合实体提取器。

两层策略：
1. jieba 词性标注 + 规则快抽（<10ms）
2. 低置信度时触发 LLM 精抽（异步，+200-500ms）

实体类型分类（参考 mem0 原版）：
- PROPER: 专有名词（人名/地名/组织名）
- QUOTED: 引号内容（用户强调的概念）
- TOPIC: 主题关键词（jieba.extract_tags）
- IDENTIFIER: 技术标识符（英文/代码符号）
"""
import asyncio
import re
import json
from dataclasses import dataclass
from typing import Any
from loguru import logger


@dataclass
class Entity:
    """提取的实体"""
    name: str
    entity_type: str = "TOPIC"  # PROPER/QUOTED/TOPIC/IDENTIFIER
    kind: str = ""  # 人物/地点/组织/概念/技术
    confidence: float = 0.5


# 英文标识符正则（技术名词/代码符号）
_IDENTIFIER_PATTERN = re.compile(r'\b[A-Z][a-zA-Z0-9+#]*\b|\b[a-z][a-zA-Z0-9]+(?:\.[a-zA-Z0-9]+)+\b')

# 引号内容正则（中文引号 + 英文引号）
_QUOTED_PATTERN = re.compile(r'["“”‘’「」『』"]([^"“”‘’「」『』"]{2,30})["“”‘’「」『』"]')

# 词性 → entity_type 映射
_POS_TO_TYPE = {
    "nr": ("PROPER", "人物"),
    "ns": ("PROPER", "地点"),
    "nt": ("PROPER", "组织"),
    "nz": ("PROPER", "专有"),
}


class EntityExtractor:
    """混合实体提取器：jieba+规则快抽 → 低置信度时 LLM 精抽"""

    def __init__(self, router: Any | None = None) -> None:
        """
        Args:
            router: ModelRouter 实例（用于 LLM 精抽）。None 时只走 jieba 规则。
        """
        self.router = router
        self._llm_prompt_template = (
            "提取以下文本中的实体，返回JSON数组。\n"
            "每项格式：{{\"name\":\"实体名\",\"type\":\"PROPER|QUOTED|TOPIC|IDENTIFIER\",\"kind\":\"人物|地点|组织|概念|技术\"}}\n"
            "文本：{text}"
        )
        logger.info("entity_extractor.ready")

    async def extract(self, text: str, importance: float = 0.5) -> list[Entity]:
        """提取实体（两层策略）。

        Args:
            text: 输入文本
            importance: 记忆重要性（>0.7 触发 LLM 精抽）
        Returns:
            Entity 列表
        """
        if not text or not text.strip():
            return []

        # 第1层：jieba + 规则快抽
        entities = self._rule_based_extract(text)

        # 第2层：低置信度时触发 LLM 精抽
        # 触发条件：jieba 提取 <2 个实体，或 importance > 0.7
        # 注意：router 为 None 时 _llm_extract 内部会返回 []，因此此处不额外检查 router，
        # 以保证调用方可通过 mock _llm_extract 进行单元测试。
        if len(entities) < 2 or importance > 0.7:
            try:
                llm_entities = await self._llm_extract(text)
                if llm_entities:
                    entities = self._merge_entities(entities, llm_entities)
            except Exception as e:
                logger.debug("entity_extractor.llm_failed", error=str(e))

        return entities

    def _rule_based_extract(self, text: str) -> list[Entity]:
        """jieba 词性标注 + 正则规则快抽（<10ms）。

        提取策略（按特异性优先级排序）：
        1. jieba.posseg.cut → nr/ns/nt/nz → PROPER
        2. 引号匹配 → QUOTED
        3. 英文标识符正则 → IDENTIFIER（先于 TOPIC，避免英文词被归类为 TOPIC）
        4. jieba.analyse.extract_tags → TOPIC
        """
        if not text or not text.strip():
            return []

        entities: list[Entity] = []
        seen_names: set[str] = set()

        try:
            import jieba.posseg as pseg
            import jieba.analyse

            # 1. jieba 词性标注 → PROPER（专有名词）
            for word, flag in pseg.cut(text):
                if flag in _POS_TO_TYPE and len(word) >= 2:
                    if word not in seen_names:
                        entity_type, kind = _POS_TO_TYPE[flag]
                        entities.append(Entity(
                            name=word, entity_type=entity_type, kind=kind,
                            confidence=0.85,
                        ))
                        seen_names.add(word)

            # 2. 引号内容 → QUOTED
            for match in _QUOTED_PATTERN.finditer(text):
                quoted_text = match.group(1).strip()
                if len(quoted_text) >= 2 and quoted_text not in seen_names:
                    entities.append(Entity(
                        name=quoted_text, entity_type="QUOTED", kind="概念",
                        confidence=0.9,
                    ))
                    seen_names.add(quoted_text)

            # 3. 英文标识符 → IDENTIFIER（先于 TOPIC，特异性更高）
            for match in _IDENTIFIER_PATTERN.finditer(text):
                identifier = match.group()
                if len(identifier) >= 2 and identifier not in seen_names:
                    entities.append(Entity(
                        name=identifier, entity_type="IDENTIFIER", kind="技术",
                        confidence=0.8,
                    ))
                    seen_names.add(identifier)

            # 4. jieba 关键词提取 → TOPIC
            try:
                keywords = jieba.analyse.extract_tags(
                    text, topK=5, withWeight=False,
                    allowPOS=("n", "vn", "v", "eng", "nz"),
                )
                for kw in keywords:
                    if len(kw) >= 2 and kw not in seen_names:
                        entities.append(Entity(
                            name=kw, entity_type="TOPIC", kind="概念",
                            confidence=0.7,
                        ))
                        seen_names.add(kw)
            except Exception as e:
                logger.debug("entity_extractor.jieba_tags_failed", error=str(e))

        except ImportError:
            logger.debug("entity_extractor.jieba_not_available, using n-gram fallback")
            # 降级到 n-gram
            for n in range(2, 5):
                for i in range(len(text) - n + 1):
                    word = text[i:i + n]
                    if word not in seen_names and not word.isspace():
                        entities.append(Entity(
                            name=word, entity_type="TOPIC", confidence=0.3,
                        ))
                        seen_names.add(word)
                        if len(entities) >= 10:
                            break
                if len(entities) >= 10:
                    break

        return entities

    async def _llm_extract(self, text: str) -> list[Entity]:
        """LLM 精抽（结构化 JSON 输出）。

        Args:
            text: 输入文本
        Returns:
            Entity 列表。失败返回空列表。
        """
        if not self.router:
            return []

        prompt = self._llm_prompt_template.format(text=text[:500])
        messages = [{"role": "user", "content": prompt}]

        result = await self._call_llm(messages)
        if not result:
            return []

        try:
            # 解析 JSON 数组
            data = json.loads(result)
            entities = []
            for item in data:
                name = item.get("name", "").strip()
                if not name:
                    continue
                entity_type = item.get("type", "TOPIC").upper()
                if entity_type not in ("PROPER", "QUOTED", "TOPIC", "IDENTIFIER"):
                    entity_type = "TOPIC"
                kind = item.get("kind", "")
                entities.append(Entity(
                    name=name, entity_type=entity_type, kind=kind,
                    confidence=0.85,
                ))
            return entities
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug("entity_extractor.llm_parse_failed", error=str(e), raw=result[:200])
            return []

    async def _call_llm(self, messages: list[dict]) -> str | None:
        """调用 LLM（通过 router）。失败返回 None。"""
        if not self.router:
            return None
        try:
            # 修复 P0-2：加 8s 超时保护，防止 router 卡住阻塞实体提取链路
            result = await asyncio.wait_for(
                self.router.route(
                    task_type="entity_extraction",
                    messages=messages,
                    temperature=0.3,
                    max_tokens=512,
                ),
                timeout=8.0,
            )
            if isinstance(result, str):
                return result
            return None
        except asyncio.TimeoutError:
            logger.warning("entity_extractor.llm_call_timeout, fallback to jieba-only")
            return None
        except Exception as e:
            logger.debug("entity_extractor.llm_call_failed", error=str(e))
            return None

    def _merge_entities(self, base: list[Entity], llm: list[Entity]) -> list[Entity]:
        """合并 jieba 和 LLM 结果，去重（以 name 为主键）。

        Args:
            base: jieba 提取结果
            llm: LLM 提取结果
        Returns:
            合并去重后的 Entity 列表
        """
        merged: list[Entity] = []
        seen: set[str] = set()

        # 先加入 base
        for e in base:
            key = e.name.lower()
            if key not in seen:
                merged.append(e)
                seen.add(key)

        # 再加入 LLM 独有的
        for e in llm:
            key = e.name.lower()
            if key not in seen:
                merged.append(e)
                seen.add(key)

        return merged
