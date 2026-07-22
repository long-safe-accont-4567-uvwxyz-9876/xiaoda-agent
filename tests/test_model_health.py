"""模型健康检查测试

验证模型自动获取脚本能检测配置中的过时模型ID。

Issue: deepseek-ai/DeepSeek-V3-0324 过时模型ID问题是模型自动获取脚本的问题，
脚本应该能自动检测和告警过时模型ID，而不是靠手动修改 config.py。
"""
import os

import pytest

# 设置 TEST_MODE 避免污染生产日志
os.environ.setdefault("TEST_MODE", "true")

from web.model_health import (
    StaleModelResult,
    check_model_staleness,
    collect_configured_models,
)


class TestCollectConfiguredModels:
    """测试收集配置中的所有模型ID"""

    def test_collects_provider_default_models(self):
        """应收集 config.py 中 _PROVIDER_DEFAULT_MODELS 的模型ID"""
        models = collect_configured_models()
        # 应包含 siliconflow 的默认模型
        sf_models = [m for m in models if m.provider == "siliconflow"]
        assert len(sf_models) > 0
        assert all(m.model_id for m in sf_models)

    def test_collects_custom_provider_default_models(self):
        """应收集 model_router.py 中 _CUSTOM_PROVIDER_DEFAULT_MODELS 的模型ID"""
        models = collect_configured_models()
        # 应包含 openrouter 和 modelscope 的默认模型
        providers = {m.provider for m in models}
        assert "openrouter" in providers or "modelscope" in providers

    def test_collects_route_table_models(self):
        """应收集 ROUTE_TABLE 中的模型ID（当模型未被其他来源去重时）"""
        # 用 mock 在 model_router.ROUTE_TABLE 中添加一个唯一条目
        import model_router
        original_route_table = model_router.ROUTE_TABLE.copy()
        try:
            model_router.ROUTE_TABLE["__test_route__"] = {
                "model": "__unique_test_model__",
                "client": "__test_provider__",
            }
            models = collect_configured_models()
            sources = {m.source for m in models}
            assert any("ROUTE_TABLE" in s for s in sources)
            # 验证唯一条目被收集
            test_models = [m for m in models if m.model_id == "__unique_test_model__"]
            assert len(test_models) == 1
            assert test_models[0].provider == "__test_provider__"
        finally:
            model_router.ROUTE_TABLE.clear()
            model_router.ROUTE_TABLE.update(original_route_table)

    def test_each_model_has_required_fields(self):
        """每个收集到的模型应有 provider/model_id/source 字段"""
        models = collect_configured_models()
        assert len(models) > 0
        for m in models:
            assert m.provider, f"provider 为空: {m}"
            assert m.model_id, f"model_id 为空: {m}"
            assert m.source, f"source 为空: {m}"


class TestCheckModelStaleness:
    """测试检测过时模型ID"""

    @pytest.mark.asyncio
    async def test_detects_outdated_model(self):
        """应检测到已下架的模型ID（如 agnes-v1）"""
        # mock 可用模型列表，不包含 agnes-v1
        available = {
            "agnes": ["agnes-2.0-flash"],
            "siliconflow": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-7B-Instruct"],
        }
        result = await check_model_staleness(
            configured_models=[
                _ModelEntry("agnes", "agnes-v1", "config._PROVIDER_DEFAULT_MODELS"),
                _ModelEntry("agnes", "agnes-2.0-flash", "config._PROVIDER_DEFAULT_MODELS"),
            ],
            available_models=available,
        )
        stale = result.stale_models
        assert len(stale) == 1
        assert stale[0].model_id == "agnes-v1"
        assert stale[0].provider == "agnes"
        assert not stale[0].is_available

    @pytest.mark.asyncio
    async def test_available_model_not_flagged(self):
        """可用的模型不应被标记为过时"""
        available = {
            "siliconflow": ["deepseek-ai/DeepSeek-V3", "THUDM/GLM-Z1-9B-0414"],
        }
        result = await check_model_staleness(
            configured_models=[
                _ModelEntry("siliconflow", "deepseek-ai/DeepSeek-V3", "test"),
                _ModelEntry("siliconflow", "THUDM/GLM-Z1-9B-0414", "test"),
            ],
            available_models=available,
        )
        assert len(result.stale_models) == 0
        assert len(result.healthy_models) == 2

    @pytest.mark.asyncio
    async def test_returns_suggestions_for_stale_models(self):
        """应为过时模型返回建议替代模型"""
        available = {
            "agnes": ["agnes-2.0-flash", "agnes-2.0-pro"],
        }
        result = await check_model_staleness(
            configured_models=[
                _ModelEntry("agnes", "agnes-v1", "config._PROVIDER_DEFAULT_MODELS"),
            ],
            available_models=available,
        )
        assert len(result.stale_models) == 1
        # 应提供替代建议
        assert result.stale_models[0].suggestion is not None
        assert "agnes" in result.stale_models[0].suggestion

    @pytest.mark.asyncio
    async def test_provider_not_in_available_treated_as_unknown(self):
        """provider 不在可用列表中时，标记为 unknown 而非 stale"""
        available = {
            "siliconflow": ["deepseek-ai/DeepSeek-V3"],
        }
        result = await check_model_staleness(
            configured_models=[
                _ModelEntry("unknown_provider", "some-model", "test"),
            ],
            available_models=available,
        )
        # unknown provider 的模型不标记为 stale（可能是本地 provider）
        assert len(result.stale_models) == 0

    @pytest.mark.asyncio
    async def test_summary_counts(self):
        """应返回正确的统计计数"""
        available = {
            "agnes": ["agnes-2.0-flash"],
            "siliconflow": ["deepseek-ai/DeepSeek-V3"],
        }
        result = await check_model_staleness(
            configured_models=[
                _ModelEntry("agnes", "agnes-v1", "test"),  # stale
                _ModelEntry("agnes", "agnes-2.0-flash", "test"),  # healthy
                _ModelEntry("siliconflow", "deepseek-ai/DeepSeek-V3", "test"),  # healthy
                _ModelEntry("unknown", "some-model", "test"),  # unknown
            ],
            available_models=available,
        )
        assert len(result.stale_models) == 1
        assert len(result.healthy_models) == 2
        assert len(result.unknown_models) == 1


class TestStaleModelResult:
    """测试结果数据结构"""

    def test_result_has_required_fields(self):
        """StaleModelResult 应有 stale/healthy/unknown/summary 字段"""
        result = StaleModelResult(
            stale_models=[],
            healthy_models=[],
            unknown_models=[],
            summary={"total": 0, "stale": 0, "healthy": 0, "unknown": 0},
        )
        assert result.stale_models == []
        assert result.healthy_models == []
        assert result.unknown_models == []
        assert result.summary["total"] == 0


# 辅助类（测试用）
class _ModelEntry:
    """测试用的模型条目"""
    def __init__(self, provider: str, model_id: str, source: str):
        self.provider = provider
        self.model_id = model_id
        self.source = source
