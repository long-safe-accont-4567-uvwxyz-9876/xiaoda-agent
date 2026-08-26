"""Strict parsing and deterministic classification for memory enrichment."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from utils.llm_cleanup import strip_thinking

MEMORY_TYPES = frozenset({"fact", "event", "affect", "relation", "instruction"})
CLASSIFICATION_VERSION = 1
MAX_ENTITIES = 10
MAX_ENTITY_LENGTH = 100
MAX_EVENT_TYPE_LENGTH = 50
_METADATA_LIMITS = {"decision": 300, "topic": 100, "mood": 100}

_FACT_KEYWORDS = (
    "生日", "电话", "住址", "地址", "姓名", "名字", "邮箱", "身份证",
    "纪念日", "账号", "号码",
)
_AFFECT_LABELS = (
    "喜悦", "开心", "悲伤", "难过", "愤怒", "焦虑", "恐惧", "害怕", "平静",
    "感激", "期待", "沮丧", "孤独",
)
_AFFECT_TRIGGERS = ("情绪触发", "会让我焦虑", "会让我害怕", "感到悲伤", "感到愤怒")
_RELATION_KEYWORDS = ("我答应", "我承诺", "称呼你", "叫你", "禁忌", "不要叫我")
_INSTRUCTION_PATTERNS = ("以后请", "请记住规则", "记住规则", "从今以后请", "以后不要", "以后必须")

_CLASSIFICATION_PROMPT_TEMPLATE = """你是记忆结构化提取助手。从以下对话中提取结构化信息，返回 JSON 格式（只返回 JSON，不要任何其他内容）：

对话内容：
{text}

请返回以下 JSON 格式：
{{
  "summary": "高质量摘要，保留关键信息，200字以内",
  "entities": ["人物、物品、地点、技术名词等实体"],
  "event_type": "事件类型",
  "memory_type": "fact/event/affect/relation/instruction 五选一",
  "importance": 0.0,
  "metadata": {{
    "decision": "决策或结论，没有则空字符串",
    "topic": "主要话题",
    "mood": "用户情绪"
  }}
}}"""


def build_classification_prompt(text: str) -> str:
    """构建与生产 _enrich_memory_async 完全一致的分类 prompt。

    单一事实源：生产编码与离线 golden dataset 评估共用，防止两处 prompt 漂移
    导致评估结果不代表线上行为。
    """
    return _CLASSIFICATION_PROMPT_TEMPLATE.format(text=text)


@dataclass(frozen=True)
class MemoryEnrichment:
    memory_type: str
    classification_status: str
    importance: float | None
    entities: tuple[str, ...]
    event_type: str
    metadata: dict[str, str]
    summary: str


def _extract_json_text(raw: str) -> str:
    cleaned = strip_thinking(raw, context="memory_enrichment").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return fenced.group(1).strip() if fenced else cleaned


def _bounded_string(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if value and len(value) <= limit else ""


def _parse_importance(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        return None
    return parsed


def parse_memory_enrichment(raw: str) -> MemoryEnrichment:
    """Parse one LLM response while rejecting malformed fields independently."""
    data = json.loads(_extract_json_text(raw))
    if not isinstance(data, dict):
        raise ValueError("memory enrichment response must be a JSON object")

    raw_type = data.get("memory_type")
    valid_type = isinstance(raw_type, str) and raw_type in MEMORY_TYPES
    memory_type = raw_type if valid_type else "event"
    status = "classified" if valid_type else "fallback"

    entities: list[str] = []
    raw_entities = data.get("entities")
    if isinstance(raw_entities, list):
        for item in raw_entities:
            entity = _bounded_string(item, MAX_ENTITY_LENGTH)
            if entity:
                entities.append(entity)
            if len(entities) >= MAX_ENTITIES:
                break

    metadata: dict[str, str] = {}
    raw_metadata = data.get("metadata")
    if isinstance(raw_metadata, dict):
        for key, limit in _METADATA_LIMITS.items():
            value = _bounded_string(raw_metadata.get(key), limit)
            if value:
                metadata[key] = value

    return MemoryEnrichment(
        memory_type=memory_type,
        classification_status=status,
        importance=_parse_importance(data.get("importance")),
        entities=tuple(entities),
        event_type=_bounded_string(data.get("event_type"), MAX_EVENT_TYPE_LENGTH),
        metadata=metadata,
        summary=_bounded_string(data.get("summary"), 200),
    )


def classify_memory_deterministically(summary: str, emotion_label: str = "") -> str:
    """Return only high-precision rule matches; all ambiguous content is an event."""
    text = summary or ""
    if any(pattern in text for pattern in _INSTRUCTION_PATTERNS):
        return "instruction"
    if any(keyword in text for keyword in _RELATION_KEYWORDS):
        return "relation"
    if emotion_label in _AFFECT_LABELS or any(trigger in text for trigger in _AFFECT_TRIGGERS):
        return "affect"
    if any(keyword in text for keyword in _FACT_KEYWORDS):
        return "fact"
    return "event"
