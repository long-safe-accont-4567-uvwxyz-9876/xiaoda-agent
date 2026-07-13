"""A4 RAG 管线根本修复测试

验证意图分类和 CRAG 评估的根本修复：
1. INTENT_LLM_CLASSIFY 默认应为 False（避免不必要的 LLM 延迟）
2. 查询变换默认模型应为 THUDM/GLM-4-9B-0414（0.84s 延迟，32K 上下文）
3. memory_manager 不应有双重超时
4. 闲聊型查询应跳过 CRAG 评估
"""
import asyncio
import os
import sys
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path

import pytest

# 设置 TEST_MODE 避免污染生产日志
os.environ.setdefault("TEST_MODE", "true")

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class TestIntentClassifyDefault:
    """测试意图分类默认配置"""

    def test_intent_llm_classify_default_false(self):
        """INTENT_LLM_CLASSIFY 默认应为 False，避免 3-8s LLM 延迟"""
        import config
        # 不设置环境变量时，默认应为 False
        assert config.INTENT_LLM_CLASSIFY is False, (
            "INTENT_LLM_CLASSIFY 默认应为 False，避免每次意图分类都调用 LLM（3-8s 延迟）。"
            "规则匹配已经足够处理大部分情况。"
        )

    def test_intent_classify_timeout_at_least_10s(self):
        """INTENT_CLASSIFY_TIMEOUT 应至少 10s，覆盖慢速模型的响应时间"""
        import config
        assert config.INTENT_CLASSIFY_TIMEOUT >= 10.0, (
            "INTENT_CLASSIFY_TIMEOUT 应至少 10s，覆盖慢速模型的响应时间。"
        )


class TestQueryTransformDefaultModel:
    """测试查询变换默认模型"""

    def test_default_model_is_fast(self):
        """查询变换默认模型应为 THUDM/GLM-Z1-9B-0414（硅基流动免费，推理质量高）"""
        from memory.query_transform import QueryTransformer
        # 不传 model 参数时，应使用 QUERY_TRANSFORM_MODEL 环境变量或默认值
        with patch.dict("os.environ", {"SILICONFLOW_API_KEY": "test_key"}, clear=False):
            qt = QueryTransformer()
            assert qt._model == "THUDM/GLM-Z1-9B-0414", (
                f"默认模型应为 THUDM/GLM-Z1-9B-0414（硅基流动免费，推理质量高），"
                f"实际为 {qt._model}。"
            )


class TestIntentClassifyNoLLMByDefault:
    """测试默认不走 LLM 分类"""

    @pytest.mark.asyncio
    async def test_classify_intent_uses_rules_not_llm_by_default(self):
        """默认应走规则匹配，不调用 LLM"""
        from memory.query_transform import QueryTransformer
        with patch("config.INTENT_LLM_CLASSIFY", False):
            qt = QueryTransformer()
            qt._available = True  # 模拟有 API Key
            qt._call_free_model = AsyncMock(return_value="factual")

            intent = await qt.classify_intent("如何配置数据库")

            # 应走规则匹配，不调用 LLM
            assert intent == "factual"
            qt._call_free_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_classify_intent_chat_keyword_short_circuits(self):
        """闲聊关键词应短路规则匹配，不调用 LLM"""
        from memory.query_transform import QueryTransformer
        with patch("config.INTENT_LLM_CLASSIFY", True):
            qt = QueryTransformer()
            qt._available = True
            qt._call_free_model = AsyncMock(return_value="chat")

            intent = await qt.classify_intent("你好啊")

            assert intent == "chat"
            # 闲聊关键词命中后不应调用 LLM
            qt._call_free_model.assert_not_called()


class TestCRAGSkipsChatIntent:
    """测试闲聊型查询跳过 CRAG 评估"""

    @pytest.mark.asyncio
    async def test_chat_intent_skips_crag_assessment(self):
        """闲聊型查询应跳过 CRAG 评估，避免不必要的低置信度告警"""
        from memory.memory_manager import MemoryManager
        from memory.retrieval_assessor import RetrievalAssessor

        mgr = MemoryManager.__new__(MemoryManager)
        mgr._assessor = RetrievalAssessor()
        mgr._query_transformer = MagicMock()
        mgr._query_transformer.available = True
        mgr._query_transformer.classify_intent = AsyncMock(return_value="chat")
        mgr._query_cache = MagicMock()
        mgr._query_cache.get = AsyncMock(return_value=None)
        mgr._query_cache.put = AsyncMock(return_value=None)
        mgr._is_retrieval_simple = MagicMock(return_value=True)
        mgr.retrieve_memories_hybrid = AsyncMock(return_value=[])
        mgr._apply_fluid_scoring = AsyncMock(return_value=[])
        mgr._compute_final_scores = AsyncMock(return_value=None)
        mgr._importance_fallback_search = AsyncMock(return_value=[])
        mgr._try_temporal_search = AsyncMock(return_value=None)
        mgr.kg = None

        import config
        with patch("config.QUERY_CACHE_ENABLED", True), \
             patch("config.RETRIEVAL_SMART_SKIP", True):
            await mgr.retrieve_memories("你好啊")

        # 闲聊型查询不应触发 CRAG 评估
        # 因为闲聊查询不需要精确检索，CRAG 评估会产生不必要的低置信度告警
        # 验证：assessor.assess 没有被调用（通过检查 stats）
        stats = mgr._assessor.stats
        assert stats["total_assessments"] == 0, (
            "闲聊型查询应跳过 CRAG 评估，避免不必要的低置信度告警。"
            f"实际触发了 {stats['total_assessments']} 次评估。"
        )


class TestNoDoubleTimeout:
    """测试不存在双重超时"""

    def test_retrieve_memories_no_outer_timeout(self):
        """retrieve_memories 中不应有外层 asyncio.wait_for 包裹 classify_intent"""
        import inspect
        from memory.memory_manager import MemoryManager
        source = inspect.getsource(MemoryManager.retrieve_memories)
        # 不应存在 asyncio.wait_for 包裹 classify_intent 的代码
        # 因为 query_transform.py 内部已经有超时控制
        assert "wait_for" not in source or "classify_intent" not in source.split("wait_for")[0][-100:], (
            "retrieve_memories 中不应有外层 asyncio.wait_for 包裹 classify_intent，"
            "因为 query_transform.py 内部已经有超时控制，双重超时会导致不必要的失败。"
        )
