"""模型路由配置验证器。

在 Web UI 保存模型路由配置前，验证模型ID与provider的一致性，
防止用户配置错误的模型ID（如 MiniMax/ 应为 MiniMaxAI/）或
不匹配的provider（如将 siliconflow 的模型配到 modelscope）。

Issue: A1 - MiniMax/MiniMax-M2.5 配置到 modelscope 导致 LLM 链路大面积失败
"""

from __future__ import annotations

from web.model_capabilities import BUILTIN_CAPABILITIES


# 已知的模型ID前缀错误映射（错误前缀 → 正确前缀）
# 用于检测用户输入的常见错误
_KNOWN_PREFIX_FIXES: dict[str, str] = {
    # SiliconFlow 上的 MiniMax 模型前缀是 MiniMaxAI/，不是 MiniMax/
    "MiniMax/": "MiniMaxAI/",
    "minimax/": "MiniMaxAI/",
    # 其他常见错误可在此添加
}


# 内置 provider 的默认模型（不在 BUILTIN_CAPABILITIES 中，但已知有效）
_BUILTIN_PROVIDER_DEFAULTS: dict[str, set[str]] = {
    "agnes": {"agnes-2.0-flash"},
    "mimo": {"mimo-v2.5", "mimo-v2.5-pro"},
}


def validate_model_route(model_id: str, provider: str) -> str | None:
    """验证模型ID与provider的匹配性。

    Args:
        model_id: 模型ID（如 "MiniMaxAI/MiniMax-M2.5"）
        provider: provider名称（如 "siliconflow"）

    Returns:
        None 如果验证通过（无警告），否则返回警告消息字符串。
    """
    if not model_id or not provider:
        return None

    # 1. 检查已知前缀错误
    for wrong_prefix, correct_prefix in _KNOWN_PREFIX_FIXES.items():
        if model_id.startswith(wrong_prefix):
            correct_id = correct_prefix + model_id[len(wrong_prefix):]
            # 确认修正后的ID确实在已知库中
            if correct_id in BUILTIN_CAPABILITIES:
                correct_provider = BUILTIN_CAPABILITIES[correct_id].provider
                return (
                    f"模型ID「{model_id}」前缀可能有误，"
                    f"建议改为「{correct_id}」（provider: {correct_provider}）"
                )
            # 即使修正后不在库中，前缀本身也是错的
            return (
                f"模型ID「{model_id}」前缀可能有误，"
                f"建议改为「{correct_prefix}」前缀"
            )

    # 2. 检查 BUILTIN_CAPABILITIES 中的模型
    if model_id in BUILTIN_CAPABILITIES:
        cap = BUILTIN_CAPABILITIES[model_id]
        if cap.provider and provider != cap.provider:
            return (
                f"模型「{model_id}」的推荐 provider 是「{cap.provider}」，"
                f"当前选择的 provider「{provider}」可能无法调用此模型"
            )
        return None

    # 3. 检查内置 provider 的默认模型
    if provider in _BUILTIN_PROVIDER_DEFAULTS:
        if model_id in _BUILTIN_PROVIDER_DEFAULTS[provider]:
            return None

    # 4. 未知模型，不报警告（可能是自定义模型）
    return None
