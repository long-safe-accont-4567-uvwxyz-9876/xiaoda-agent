"""模型健康检查模块。

扫描配置中所有硬编码的模型ID，验证它们是否仍然在对应 provider 的可用模型列表中。
检测过时模型ID（如已下架的 agnes-v1、deepseek-ai/DeepSeek-V3-0324 等），
返回过时模型列表和建议替代模型。

Issue: deepseek-ai/DeepSeek-V3-0324 过时模型ID问题是模型自动获取脚本的问题，
脚本应该能自动检测和告警过时模型ID，而不是靠手动修改 config.py。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger


@dataclass
class ConfiguredModel:
    """配置中收集到的模型条目"""

    provider: str
    model_id: str
    source: str  # 来源标识，如 "config._PROVIDER_DEFAULT_MODELS"


@dataclass
class StaleModel:
    """过时模型信息"""

    provider: str
    model_id: str
    source: str
    is_available: bool  # False 表示过时
    suggestion: str | None = None  # 建议替代模型


@dataclass
class StaleModelResult:
    """模型健康检查结果"""

    stale_models: list[StaleModel] = field(default_factory=list)
    healthy_models: list[StaleModel] = field(default_factory=list)
    unknown_models: list[StaleModel] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)


def collect_configured_models() -> list[ConfiguredModel]:
    """收集配置中所有硬编码的模型ID。

    扫描以下来源：
    1. config.py 的 _PROVIDER_DEFAULT_MODELS
    2. model_router.py 的 _CUSTOM_PROVIDER_DEFAULT_MODELS
    3. model_router.py 的 ROUTE_TABLE

    Returns:
        ConfiguredModel 列表，每项包含 provider/model_id/source
    """
    models: list[ConfiguredModel] = []
    seen: set[tuple[str, str]] = set()

    def _add(provider: str, model_id: str, source: str) -> None:
        if not provider or not model_id:
            return
        key = (provider, model_id)
        if key in seen:
            return
        seen.add(key)
        models.append(ConfiguredModel(provider=provider, model_id=model_id, source=source))

    # 1. config.py 的 _PROVIDER_DEFAULT_MODELS
    try:
        from config import _PROVIDER_DEFAULT_MODELS
        for provider, model_id in _PROVIDER_DEFAULT_MODELS.items():
            _add(provider, model_id, "config._PROVIDER_DEFAULT_MODELS")
    except (ImportError, AttributeError) as e:
        logger.debug("model_health.import_provider_defaults_failed error={}", str(e))

    # 2. model_router.py 的 _CUSTOM_PROVIDER_DEFAULT_MODELS
    try:
        from model_router import ModelRouter
        custom_defaults = getattr(ModelRouter, "_CUSTOM_PROVIDER_DEFAULT_MODELS", {})
        for provider, model_id in custom_defaults.items():
            _add(provider, model_id, "model_router._CUSTOM_PROVIDER_DEFAULT_MODELS")
    except (ImportError, AttributeError) as e:
        logger.debug("model_health.import_custom_defaults_failed error={}", str(e))

    # 3. model_router.py 的 ROUTE_TABLE
    try:
        from model_router import ROUTE_TABLE
        for task, entry in ROUTE_TABLE.items():
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("model", "")
            provider = entry.get("client", "")
            if model_id and provider:
                _add(provider, model_id, f"model_router.ROUTE_TABLE[{task}]")
    except (ImportError, AttributeError) as e:
        logger.debug("model_health.import_route_table_failed error={}", str(e))

    logger.info("model_health.collected count={} source={}", len(models), [m.source for m in models])
    return models


async def check_model_staleness(
    configured_models: list[ConfiguredModel],
    available_models: dict[str, list[str]],
) -> StaleModelResult:
    """检查配置中的模型ID是否过时。

    Args:
        configured_models: 配置中的模型列表
        available_models: 各 provider 的可用模型ID列表，如 {"siliconflow": ["model1", "model2"]}

    Returns:
        StaleModelResult 包含 stale/healthy/unknown 模型列表和统计
    """
    stale: list[StaleModel] = []
    healthy: list[StaleModel] = []
    unknown: list[StaleModel] = []

    for cm in configured_models:
        available = available_models.get(cm.provider)

        # provider 不在可用列表中 → unknown（可能是本地 provider 或未配置）
        if available is None:
            unknown.append(StaleModel(
                provider=cm.provider,
                model_id=cm.model_id,
                source=cm.source,
                is_available=False,
                suggestion=None,
            ))
            continue

        # 模型在可用列表中 → healthy
        if cm.model_id in available:
            healthy.append(StaleModel(
                provider=cm.provider,
                model_id=cm.model_id,
                source=cm.source,
                is_available=True,
                suggestion=None,
            ))
            continue

        # 模型不在可用列表中 → stale，生成建议
        suggestion = _generate_suggestion(cm.provider, cm.model_id, available)
        stale.append(StaleModel(
            provider=cm.provider,
            model_id=cm.model_id,
            source=cm.source,
            is_available=False,
            suggestion=suggestion,
        ))

    result = StaleModelResult(
        stale_models=stale,
        healthy_models=healthy,
        unknown_models=unknown,
        summary={
            "total": len(configured_models),
            "stale": len(stale),
            "healthy": len(healthy),
            "unknown": len(unknown),
        },
    )
    logger.info(
        "model_health.checked total={} stale={} healthy={} unknown={}",
        result.summary["total"], result.summary["stale"],
        result.summary["healthy"], result.summary["unknown"],
    )
    return result


def _generate_suggestion(provider: str, model_id: str, available: list[str]) -> str | None:
    """为过时模型生成建议替代模型。

    策略：
    1. 如果有同名模型的不同版本，建议最新版本
    2. 否则建议 provider 的第一个可用模型
    3. 如果可用列表为空，返回 None
    """
    if not available:
        return None

    # 策略1：查找同系列的模型（如 agnes-v1 → agnes-2.0-flash）
    # 提取模型系列名（如 "agnes" from "agnes-v1"）
    series = model_id.split("-")[0].split("/")[-1].lower()
    same_series = [m for m in available if series in m.lower()]
    if same_series:
        # 选择版本号最高的
        same_series.sort(reverse=True)
        return same_series[0]

    # 策略2：返回第一个可用模型
    return available[0]
