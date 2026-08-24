from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import dataclass
from typing import Any, Literal

PromptStatus = Literal["draft", "staging", "production", "retired"]


def _resolve_template_ref(reference: str) -> str:
    module_name, attribute_path = reference.split(":", 1)
    value: Any = importlib.import_module(module_name)
    for attribute in attribute_path.split("."):
        value = getattr(value, attribute)
    if not isinstance(value, str):
        raise TypeError(f"prompt template is not text: {reference}")
    return value


@dataclass(frozen=True)
class PromptProfile:
    prompt_id: str
    version: str
    output_schema: dict[str, Any]
    template_refs: tuple[str, ...] = ()
    variables: tuple[str, ...] = ()
    min_model_capabilities: tuple[str, ...] = ()
    status: PromptStatus = "draft"

    def __post_init__(self) -> None:
        if not self.prompt_id or not self.version:
            raise ValueError("prompt_id and version are required")
        if self.status not in {"draft", "staging", "production", "retired"}:
            raise ValueError(f"invalid prompt status: {self.status}")
        if self.status == "production" and not self.template_refs:
            raise ValueError("production prompt profile must bind real templates")

    @property
    def template_hash(self) -> str:
        templates = [_resolve_template_ref(ref) for ref in self.template_refs]
        payload = json.dumps(
            {
                "prompt_id": self.prompt_id,
                "version": self.version,
                "output_schema": self.output_schema,
                "templates": templates,
                "variables": self.variables,
                "min_model_capabilities": self.min_model_capabilities,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def public_summary(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "version": self.version,
            "template_hash": self.template_hash,
            "status": self.status,
        }


_JSON_OBJECT = {"type": "object"}
_STRING = {"type": "string"}
_EMOTION_SCHEMA = {
    "type": "object",
    "required": ["primary", "P", "A", "D", "needs", "style"],
    "properties": {
        "primary": {"type": "string"},
        "P": {"type": "number"},
        "A": {"type": "number"},
        "D": {"type": "number"},
        "needs": {"type": "array"},
        "style": {"type": "string"},
    },
}
_KG_EXTRACT_SCHEMA = {
    "type": "object",
    "required": ["entities", "relations"],
    "properties": {
        "entities": {"type": "array"},
        "relations": {"type": "array"},
    },
}
_CONFLICT_SCHEMA = {
    "type": "object",
    "required": ["contradicted_indices"],
    "properties": {"contradicted_indices": {"type": "array"}},
}
_PORTRAIT_SCHEMA = {
    "type": "object",
    "required": ["portrait", "changes"],
    "properties": {
        "portrait": {"type": "string"},
        "changes": {"type": "string"},
    },
}
_INTENT_SCHEMA = {
    "type": "object",
    "required": ["factors", "residual"],
    "properties": {
        "factors": {"type": "array"},
        "residual": {"type": "number"},
    },
}

NODE_PROMPT_PROFILES: dict[str, tuple[PromptProfile, ...]] = {
    "query_transform": (
        PromptProfile(
            "query.rewrite", "1.0.0", _STRING,
            ("memory.query_transform:REWRITE_PROMPT",),
            status="production",
        ),
        PromptProfile(
            "query.expand", "1.0.0", {"type": "array"},
            ("memory.query_transform:EXPAND_PROMPT",),
            status="production",
        ),
        PromptProfile(
            "query.hyde", "1.0.0", _STRING,
            ("memory.query_transform:HYDE_PROMPT",),
            status="production",
        ),
        PromptProfile(
            "query.classify", "1.0.0", _STRING,
            ("memory.query_transform:CLASSIFY_PROMPT",),
            status="production",
        ),
    ),
    "instinct": (
        PromptProfile(
            "instinct.extract", "1.0.0", _STRING,
            ("instinct_manager:EXTRACT_PROMPT",),
            status="production",
        ),
    ),
    "error_rule": (
        PromptProfile(
            "error_rule.extract", "1.0.0", _STRING,
            ("tool_engine.error_rule_pipeline:EXTRACT_PROMPT",),
            status="production",
        ),
    ),
    "kg_extract": (
        PromptProfile(
            "kg.extract_episode", "1.0.0", _KG_EXTRACT_SCHEMA,
            ("memory.knowledge_graph_v2:ENTITY_EXTRACT_PROMPT_V2",),
            status="production",
        ),
        PromptProfile(
            "kg.resolve_conflict", "1.0.0", _CONFLICT_SCHEMA,
            ("memory.knowledge_graph_v2:CONTRADICTION_PROMPT",),
            status="production",
        ),
        PromptProfile(
            "kg.summarize_entity", "1.0.0", _STRING,
            ("memory.knowledge_graph_v2:SUMMARY_REWRITE_PROMPT",),
            status="production",
        ),
    ),
    "emotion_llm": (
        PromptProfile(
            "emotion.analyze", "1.0.0", _EMOTION_SCHEMA,
            (
                "emotion.emotion_llm:_SYSTEM_PROMPT",
                "emotion.emotion_llm:_USER_PROMPT_TEMPLATE",
            ),
            status="production",
        ),
    ),
    "portrait": (
        PromptProfile(
            "portrait.consolidate", "1.0.0", _PORTRAIT_SCHEMA,
            ("emotion.portrait_manager:CONSOLIDATE_PROMPT_TEMPLATE",),
            status="production",
        ),
    ),
    "nudge": (PromptProfile("nudge.generate", "1.0.0", _STRING),),
    "reunion": (PromptProfile("reunion.generate", "1.0.0", _STRING),),
    "growth": (PromptProfile("growth.narrative", "1.0.0", _STRING),),
    "memory_distill": (
        PromptProfile(
            "memory.compress_episode", "1.0.0", _STRING,
            ("memory.memory_distiller:DISTILL_PROMPT",),
            status="production",
        ),
        PromptProfile(
            "memory.build_recall_note", "1.0.0", _STRING,
            ("memory.memory_distiller:RECALL_PROMPT_TEMPLATE",),
            status="production",
        ),
    ),
    "spontaneous_recall": (
        PromptProfile("recall.monologue", "1.0.0", _STRING),
    ),
    "dream": (PromptProfile("dream.discover_preferences", "1.0.0", _JSON_OBJECT),),
    "intent_decomposition": (
        PromptProfile(
            "intent.decompose", "1.0.0", _INTENT_SCHEMA,
            ("core.intent_decomposition:IntentDecomposer._SYSTEM_PROMPT",),
            status="production",
        ),
    ),
}


def profiles_for_node(node_id: str) -> tuple[PromptProfile, ...]:
    return NODE_PROMPT_PROFILES.get(node_id, ())


def profile_by_id(prompt_id: str) -> PromptProfile | None:
    for profiles in NODE_PROMPT_PROFILES.values():
        for profile in profiles:
            if profile.prompt_id == prompt_id:
                return profile
    return None
