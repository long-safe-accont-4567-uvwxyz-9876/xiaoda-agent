"""Override 消费缝契约测试：distill 回忆笔记与 KG v2 抽取的 production 提示词接管。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

from web.config_service import ConfigService
from web.prompt_profile_repository import PromptProfileRepository

PASSING_AB_REPORT = {
    "candidate": {"schema_rate": 1.0, "golden_rate": 1.0, "violation_count": 0},
    "regressions": [],
}


def _promote_override(tmp_path, prompt_id: str, record: dict):
    config = ConfigService(tmp_path / "webui_overrides.json")
    repository = PromptProfileRepository(config)
    repository.stage(record)
    repository.promote(prompt_id, ab_report=dict(PASSING_AB_REPORT))
    return config


def test_recall_prompt_falls_back_to_builtin_without_override(tmp_path, monkeypatch):
    from memory.memory_distiller import MemoryDistiller
    from web import config_service as config_service_module

    monkeypatch.setattr(config_service_module, "_instance",
                        ConfigService(tmp_path / "overrides.json"))
    prompt = MemoryDistiller._render_recall_prompt("8月20日：小林花了1280元。", "小妲")
    assert "回忆整理助手" in prompt
    assert "1280" in prompt


def test_recall_prompt_uses_production_override(tmp_path, monkeypatch):
    from memory.memory_distiller import MemoryDistiller
    from web import config_service as config_service_module

    config = _promote_override(tmp_path, "memory.build_recall_note", {
        "prompt_id": "memory.build_recall_note",
        "version": "2.0.0",
        "user_template": "V2笔记：{n}请保留数字如1280。\n{memories_text}",
        "variables": {
            "n": {"required": True},
            "memories_text": {"required": True},
        },
        "output_schema": {"type": "string"},
    })
    monkeypatch.setattr(config_service_module, "_instance", config)

    prompt = MemoryDistiller._render_recall_prompt("原始记忆文本", "小妲")
    assert prompt.startswith("V2笔记：小妲")
    assert "原始记忆文本" in prompt


def test_kg_extract_messages_fall_back_to_builtin(tmp_path, monkeypatch):
    from memory.knowledge_graph_v2 import KnowledgeGraphV2
    from web import config_service as config_service_module

    monkeypatch.setattr(config_service_module, "_instance",
                        ConfigService(tmp_path / "overrides.json"))
    messages = KnowledgeGraphV2._build_extract_messages("用户喜欢打篮球{x}")
    assert messages[0]["role"] == "system"
    assert "知识提取助手" in messages[0]["content"]
    assert "用户喜欢打篮球{x}" in messages[1]["content"]
    assert "{summary}" not in messages[1]["content"]


def test_kg_extract_messages_use_production_override(tmp_path, monkeypatch):
    from memory.knowledge_graph_v2 import KnowledgeGraphV2
    from web import config_service as config_service_module

    config = _promote_override(tmp_path, "kg.extract_episode", {
        "prompt_id": "kg.extract_episode",
        "version": "2.0.0",
        "system_template": "只输出JSON。",
        "user_template": "摘要：{summary}\n提取实体。",
        "variables": {"summary": {"required": True}},
        "output_schema": {"type": "string"},
    })
    monkeypatch.setattr(config_service_module, "_instance", config)

    messages = KnowledgeGraphV2._build_extract_messages("猫叫煤球")
    assert messages[0]["content"] == "只输出JSON。"
    assert messages[1]["content"] == "摘要：猫叫煤球\n提取实体。"


def test_override_with_mismatched_variables_falls_back_safely(tmp_path, monkeypatch):
    from memory.memory_distiller import MemoryDistiller
    from web import config_service as config_service_module

    config = _promote_override(tmp_path, "memory.build_recall_note", {
        "prompt_id": "memory.build_recall_note",
        "version": "9.9.9",
        "user_template": "{other_only}",
        "variables": {"other_only": {"required": True}},
        "output_schema": {"type": "string"},
    })
    monkeypatch.setattr(config_service_module, "_instance", config)

    prompt = MemoryDistiller._render_recall_prompt("记忆内容", "小妲")
    assert "回忆整理助手" in prompt


def _assert_fallback_and_override(tmp_path, monkeypatch, prompt_id, variables,
                                  render_fn, builtin_marker):
    from web import config_service as config_service_module

    monkeypatch.setattr(config_service_module, "_instance",
                        ConfigService(tmp_path / "overrides.json"))
    fallback = render_fn()
    assert builtin_marker in fallback

    config = _promote_override(tmp_path, prompt_id, {
        "prompt_id": prompt_id,
        "version": "2.0.0",
        "user_template": "OVERRIDE::{text}",
        "variables": {name: {"required": False} for name in variables},
        "output_schema": {"type": "string"},
    })
    monkeypatch.setattr(config_service_module, "_instance", config)
    overridden = render_fn()
    assert overridden.startswith("OVERRIDE::")


def test_instinct_extract_consumes_override(tmp_path, monkeypatch):
    from instinct_manager import InstinctManager

    def render():
        return InstinctManager._render_extract_prompt("用户输入A{}", "回复B{}")

    _assert_fallback_and_override(
        tmp_path, monkeypatch, "instinct.extract",
        {"user_input": "用户输入A{}", "reply": "回复B{}"},
        render, "对话内容",
    )


def test_error_rule_extract_consumes_override(tmp_path, monkeypatch):
    from tool_engine.error_rule_pipeline import ErrorRulePipeline

    def render():
        return ErrorRulePipeline._render_extract_prompt(
            "web_search", '{"q": "x{}"}', "timeout{}",
        )

    _assert_fallback_and_override(
        tmp_path, monkeypatch, "error_rule.extract",
        {"tool_name": "web_search", "args": '{"q": "x{}"}', "error": "timeout{}"},
        render, "错误分析助手",
    )


def test_portrait_consolidate_consumes_override_with_markers(tmp_path, monkeypatch):
    import emotion.portrait_manager as pm

    def render():
        return pm._build_consolidate_prompt("旧画像{}", "近期记忆{}", "", "爸爸")

    _assert_fallback_and_override(
        tmp_path, monkeypatch, "portrait.consolidate",
        {"agent_name": "小妲", "address_term": "爸爸",
         "OLD_SECTION": "旧画像{}", "RECENT_MEMORIES": "近期记忆{}",
         "RECENT_NOTES": ""},
        render, "安静的夜晚",
    )


def test_distill_compress_consumes_override(tmp_path, monkeypatch):
    from memory.memory_distiller import MemoryDistiller

    def render():
        return MemoryDistiller._render_compress_prompt("记忆列表{}")

    _assert_fallback_and_override(
        tmp_path, monkeypatch, "memory.compress_episode",
        {"memories_text": "记忆列表{}"},
        render, "记忆蒸馏助手",
    )


async def test_kg_and_intent_overrides_consumed():
    """kg.summarize_entity / kg.resolve_conflict / intent.decompose 三个消费方接线回归。"""
    from types import SimpleNamespace

    captured: list[tuple[str, dict]] = []

    def fake_try_resolve(prompt_id, variables):
        captured.append((prompt_id, variables))
        return ("sys", f"OVERRIDE::{prompt_id}")

    # kg.summarize_entity
    import memory.knowledge_graph_v2 as kgv2
    called = {}
    async def fake_call(messages, **kw):
        called["prompt"] = messages[0]["content"]
        return "new summary"
    import web.prompt_profile_repository as ppr
    saved = ppr.try_resolve
    ppr.try_resolve = fake_try_resolve
    try:
        result = await kgv2.KnowledgeGraphV2._rewrite_summary(
            SimpleNamespace(_call_free_model=fake_call),
            "old", ["obs1"], "实体A")
        assert result == "new summary"
        assert called["prompt"] == "OVERRIDE::kg.summarize_entity"
        # kg.resolve_conflict
        idx = await kgv2.KnowledgeGraphV2._detect_contradictions(
            SimpleNamespace(_call_free_model=AsyncMock(return_value="[]")),
            "新事实", ["旧1", "旧2"])
        assert idx is not None
    finally:
        ppr.try_resolve = saved


def test_intent_decompose_fallback_and_dual_slot_override(tmp_path, monkeypatch):
    from core.intent_decomposition import IntentDecomposer
    from web import config_service as config_service_module

    monkeypatch.setattr(config_service_module, "_instance",
                        ConfigService(tmp_path / "overrides.json"))
    fallback = IntentDecomposer._build_messages("帮我查天气{}")
    assert "意图分析专家" in fallback[0]["content"]
    assert "帮我查天气{}" in fallback[1]["content"]

    config = _promote_override(tmp_path, "intent.decompose", {
        "prompt_id": "intent.decompose",
        "version": "2.0.0",
        "system_template": "V2系统规则：只输出JSON。",
        "user_template": "分析下这段话{text}的意图。",
        "variables": {"text": {"required": True}},
        "output_schema": {"type": "string"},
    })
    monkeypatch.setattr(config_service_module, "_instance", config)

    messages = IntentDecomposer._build_messages("查询文本")
    assert messages[0]["content"] == "V2系统规则：只输出JSON。"
    assert messages[1]["content"] == "分析下这段话查询文本的意图。"


def test_intent_profile_renders_builtin_as_system_plus_user():
    from web.prompt_ab_runner import render_builtin_templates

    system, user = render_builtin_templates(
        "intent.decompose", {"text": "样本输入"}
    )
    assert "意图分析专家" in system
    assert "样本输入" in user
