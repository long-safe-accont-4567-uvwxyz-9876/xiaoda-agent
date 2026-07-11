"""EntityExtractor 混合实体提取测试：jieba+规则快抽 → LLM 精抽"""
import asyncio
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory.entity_extractor import EntityExtractor, Entity


class TestEntityDataclass:
    """Entity dataclass"""

    def test_entity_creation(self):
        """创建 Entity 对象"""
        e = Entity(name="Python", entity_type="IDENTIFIER", kind="技术", confidence=0.9)
        assert e.name == "Python"
        assert e.entity_type == "IDENTIFIER"
        assert e.kind == "技术"
        assert e.confidence == 0.9

    def test_entity_defaults(self):
        """Entity 默认值"""
        e = Entity(name="测试")
        assert e.entity_type == "TOPIC"
        assert e.kind == ""
        assert e.confidence == 0.5


class TestRuleBasedExtract:
    """jieba+规则快抽（第1层）"""

    def _make_extractor(self):
        """创建不依赖 LLM 的 extractor"""
        return EntityExtractor(router=None)

    def test_extract_proper_noun(self):
        """提取专有名词（人名）"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract("张三今天去了北京")
        names = [e.name for e in entities]
        # jieba 应能识别 "张三" 或 "北京"
        assert len(entities) > 0

    def test_extract_quoted_content(self):
        """提取引号内容"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract('用户强调了"机器学习"的重要性')
        quoted = [e for e in entities if e.entity_type == "QUOTED"]
        assert len(quoted) >= 1
        assert "机器学习" in quoted[0].name

    def test_extract_identifier(self):
        """提取英文标识符"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract("我喜欢用 Python 和 React 编程")
        identifiers = [e for e in entities if e.entity_type == "IDENTIFIER"]
        id_names = [e.name for e in identifiers]
        assert "Python" in id_names or "React" in id_names

    def test_extract_topic_keywords(self):
        """提取主题关键词"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract("深度学习在计算机视觉领域有很多应用")
        topics = [e for e in entities if e.entity_type == "TOPIC"]
        assert len(topics) > 0

    def test_extract_empty_text(self):
        """空文本返回空列表"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract("")
        assert entities == []

    def test_extract_no_duplicates(self):
        """同一实体不重复提取"""
        extractor = self._make_extractor()
        entities = extractor._rule_based_extract("Python Python Python")
        names = [e.name for e in entities]
        # 同名实体应去重
        assert len(names) == len(set(names))


class TestLLMExtract:
    """LLM 精抽（第2层，低置信度触发）"""

    def _make_extractor_with_mock_router(self):
        """创建带 mock router 的 extractor"""
        mock_router = MagicMock()
        return EntityExtractor(router=mock_router)

    async def test_llm_extract_success(self):
        """LLM 精抽返回结构化 JSON"""
        extractor = self._make_extractor_with_mock_router()
        mock_response = '[{"name":"量子计算","type":"TOPIC","kind":"概念"}]'
        extractor._call_llm = AsyncMock(return_value=mock_response)
        entities = await extractor._llm_extract("量子计算是未来技术")
        assert len(entities) == 1
        assert entities[0].name == "量子计算"
        assert entities[0].entity_type == "TOPIC"

    async def test_llm_extract_failure_fallback(self):
        """LLM 精抽失败返回空列表"""
        extractor = self._make_extractor_with_mock_router()
        extractor._call_llm = AsyncMock(return_value=None)
        entities = await extractor._llm_extract("测试文本")
        assert entities == []

    async def test_llm_extract_invalid_json(self):
        """LLM 返回非法 JSON 返回空列表"""
        extractor = self._make_extractor_with_mock_router()
        extractor._call_llm = AsyncMock(return_value="not a json")
        entities = await extractor._llm_extract("测试文本")
        assert entities == []


class TestExtractIntegration:
    """extract() 集成：jieba+规则 → 低置信度触发 LLM"""

    async def test_extract_no_llm_when_confidence_high(self):
        """jieba 提取 ≥2 个实体且 importance ≤ 0.7 时不触发 LLM"""
        extractor = EntityExtractor(router=None)
        extractor._rule_based_extract = MagicMock(return_value=[
            Entity(name="Python", entity_type="IDENTIFIER", confidence=0.9),
            Entity(name="编程", entity_type="TOPIC", confidence=0.8),
        ])
        extractor._llm_extract = AsyncMock(return_value=[])
        entities = await extractor.extract("我喜欢Python编程", importance=0.5)
        extractor._llm_extract.assert_not_awaited()
        assert len(entities) == 2

    async def test_extract_triggers_llm_when_few_entities(self):
        """jieba 提取 <2 个实体时触发 LLM"""
        extractor = EntityExtractor(router=None)
        extractor._rule_based_extract = MagicMock(return_value=[
            Entity(name="只有", entity_type="TOPIC", confidence=0.5),
        ])
        extractor._llm_extract = AsyncMock(return_value=[
            Entity(name="LLM补充", entity_type="TOPIC", confidence=0.8),
        ])
        entities = await extractor.extract("一些文本", importance=0.5)
        extractor._llm_extract.assert_awaited_once()
        # 合并后应有2个实体
        assert len(entities) >= 2

    async def test_extract_triggers_llm_when_high_importance(self):
        """importance > 0.7 时触发 LLM 精抽"""
        extractor = EntityExtractor(router=None)
        extractor._rule_based_extract = MagicMock(return_value=[
            Entity(name="Python", entity_type="IDENTIFIER", confidence=0.9),
            Entity(name="编程", entity_type="TOPIC", confidence=0.8),
        ])
        extractor._llm_extract = AsyncMock(return_value=[])
        entities = await extractor.extract("重要记忆", importance=0.85)
        extractor._llm_extract.assert_awaited_once()

    def test_merge_entities_dedup(self):
        """合并 jieba 和 LLM 结果，去重"""
        extractor = EntityExtractor(router=None)
        base = [
            Entity(name="Python", entity_type="IDENTIFIER", confidence=0.9),
            Entity(name="编程", entity_type="TOPIC", confidence=0.8),
        ]
        llm = [
            Entity(name="Python", entity_type="IDENTIFIER", confidence=0.95),
            Entity(name="机器学习", entity_type="TOPIC", confidence=0.85),
        ]
        merged = extractor._merge_entities(base, llm)
        names = [e.name for e in merged]
        assert "Python" in names
        assert "编程" in names
        assert "机器学习" in names
        # Python 不重复
        assert names.count("Python") == 1
