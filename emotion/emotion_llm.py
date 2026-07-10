"""LLM 深度情绪分析层

双层调度的 LLM 层：
- 关键词层(emotion_simple)即时返回(0ms)，用于 sticker 选择等
- LLM 层(本模块)后台执行(300-800ms)，返回更精准的 PAD + 深层需求
- 超时 500ms 回退到关键词结果

用法：
    from emotion.emotion_llm import detect_emotion_llm
    result = await detect_emotion_llm("我今天好累啊", context="用户连续加班三天")
    # result = {"primary": "悲伤", "P": -0.6, "A": 0.3, "D": 0.2, "needs": ["休息", "被理解"], "style": "温柔陪伴"}
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

from loguru import logger

# 超时时间（秒）
LLM_EMOTION_TIMEOUT = 0.5  # 500ms


async def detect_emotion_llm(
    text: str,
    context: str = "",
    router: Any = None,
) -> dict:
    """LLM 深度情绪分析，返回 PAD + 深层需求

    Args:
        text: 用户输入文本
        context: 上下文（可选，如最近的对话历史）
        router: 模型路由器，需有 route(route_name, messages, temperature) 方法
                如果为 None，尝试从全局获取

    Returns:
        {
            "primary": str,      # 情绪标签（中文）
            "P": float,          # Pleasure -1~1
            "A": float,          # Arousal 0~1
            "D": float,          # Dominance 0~1
            "needs": list[str],  # 深层心理需求
            "style": str,        # 建议回应风格
        }
        超时或异常时返回空字典 {}，由调用方回退到关键词结果
    """
    if not text or not text.strip():
        return {}

    # 获取 router
    if router is None:
        router = _get_global_router()
    if router is None:
        logger.debug("emotion_llm.no_router")
        return {}

    prompt = _build_prompt(text, context)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    try:
        result = await asyncio.wait_for(
            router.route("chat_flash", messages, temperature=0.3),
            timeout=LLM_EMOTION_TIMEOUT,
        )
        raw_text = result if isinstance(result, str) else (
            result.choices[0].message.content if hasattr(result, 'choices') else str(result)
        )
        return _parse_llm_response(raw_text)
    except asyncio.TimeoutError:
        logger.debug("emotion_llm.timeout", text=text[:50])
        return {}
    except Exception as e:
        logger.debug("emotion_llm.error", error=str(e))
        return {}


_SYSTEM_PROMPT = """你是一个情绪分析专家。分析用户情绪并返回 JSON。

返回格式（严格 JSON，不要 markdown）：
{
  "primary": "情绪标签",
  "P": 0.0,
  "A": 0.0,
  "D": 0.0,
  "needs": ["需求1"],
  "style": "建议回应风格"
}

情绪标签从中选择：喜悦/兴奋/悲伤/愤怒/焦虑/害羞/好奇/思考/恐惧/平静
P: Pleasure -1(不悦)~1(愉悦)
A: Arousal 0(平静)~1(激动)
D: Dominance 0(受控)~1(掌控)
needs: 用户深层心理需求（如"被理解"、"休息"、"安全感"）
style: 建议回应风格（如"温柔陪伴"、"轻快回应"、"认真倾听"）"""


def _build_prompt(text: str, context: str) -> str:
    """构建分析提示"""
    prompt = f'用户说：「{text}」'
    if context:
        prompt += f'\n上下文：{context}'
    prompt += '\n请分析情绪并返回 JSON。'
    return prompt


def _parse_llm_response(raw: str) -> dict:
    """解析 LLM 返回的 JSON 响应

    容错处理：
    - 提取 JSON 部分（可能被 markdown 包裹）
    - 验证字段类型和范围
    - 解析失败返回空字典
    """
    if not raw or not raw.strip():
        return {}

    # 去除 markdown 代码块标记
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # 去掉 ```json 或 ``` 开头和 ``` 结尾
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    # 尝试提取 JSON（支持嵌套对象）
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start == -1 or end == -1 or end <= start:
        return {}

    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return {}

    # 验证和规范化
    result = {
        "primary": str(data.get("primary", "平静")),
        "P": _clamp(_safe_float(data.get("P", 0), 0), -1.0, 1.0),
        "A": _clamp(_safe_float(data.get("A", 0), 0), 0.0, 1.0),
        "D": _clamp(_safe_float(data.get("D", 0.5), 0.5), 0.0, 1.0),
        "needs": [str(n) for n in data.get("needs", []) if n],
        "style": str(data.get("style", "")),
    }
    return result


def _safe_float(val: Any, default: float = 0.0) -> float:
    """安全转换为 float，失败时返回默认值"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, min_val: float, max_val: float) -> float:
    """限制数值范围"""
    return max(min_val, min(max_val, value))


def _get_global_router() -> Any:
    """尝试获取全局模型路由器"""
    try:
        from core.model_router import get_model_router
        return get_model_router()
    except Exception:
        return None