from __future__ import annotations

from types import SimpleNamespace

import pytest

import qq_bot_adapter as qq_module
from agent_core._shared import ProcessResult, RequestContext, _group_context_enabled_var
from agent_core.group_context import GroupContextRegistry
from core.background_tasks import _request_context_var
from qq_bot_adapter import AIQQBot


@pytest.fixture(autouse=True)
def _enable_group_buffer(monkeypatch):
    monkeypatch.setattr(qq_module, "GROUP_CHAT_BUFFER_ENABLED", True)


def _bot(results: list[ProcessResult | None]) -> tuple[AIQQBot, list[tuple[object, dict]]]:
    bot = AIQQBot.__new__(AIQQBot)
    bot._group_context_registry = GroupContextRegistry()
    captured: list[tuple[object, dict]] = []

    async def process(req):
        captured.append((req, _request_context_var.get() or {}))
        assert _group_context_enabled_var.get() is req.group_context_enabled
        return results.pop(0)

    bot._process_with_core = process
    return bot, captured


def _message(message_id: str, group_key: str) -> SimpleNamespace:
    return SimpleNamespace(id=message_id, message_id=message_id, group_openid=group_key)


@pytest.mark.asyncio
async def test_request_context_defaults_group_context_off() -> None:
    assert RequestContext().group_context_enabled is False


@pytest.mark.asyncio
async def test_group_buffer_disabled_still_emits_per_group_anonymous_audit_metadata(
    monkeypatch,
) -> None:
    monkeypatch.setattr(qq_module, "GROUP_CHAT_BUFFER_ENABLED", False)
    bot, captured = _bot([ProcessResult(reply="done")])

    await bot._run_message_pipeline(
        _message("m", "group-raw"), is_group=True,
        user_input="hello", user_id="qq_member", openid="member-openid",
        is_master=False, image_data=None, group_key="group-raw",
    )

    req, metadata = captured[0]
    assert req.group_context_enabled is False
    assert req.system_context == ""
    assert metadata["group_key"] != "group-raw"
    assert metadata["actor_alias"] == "群成员"
    assert "member-openid" not in repr(metadata)


@pytest.mark.asyncio
async def test_group_pipeline_injects_prior_at_without_ids_or_current_duplication() -> None:
    bot, captured = _bot([ProcessResult(reply="答复 A"), ProcessResult(reply="答复 B")])

    await bot._run_message_pipeline(
        _message("m-a", "group-raw"), is_group=True,
        user_input="A 的问题", user_id="qq_member-a", openid="member-a",
        is_master=False, image_data=None, session_id="qq_group:wrong-group",
        group_key="wrong-group",
    )
    await bot._run_message_pipeline(
        _message("m-b", "group-raw"), is_group=True,
        user_input="B 的问题", user_id="qq_member-b", openid="member-b",
        is_master=True, image_data=None, group_key="group-raw",
    )

    first_req, _ = captured[0]
    second_req, metadata = captured[1]
    assert first_req.group_context_enabled is True
    assert first_req.system_context == ""
    assert "成员A: A 的问题" in second_req.system_context
    assert "助手: 答复 A" in second_req.system_context
    assert "B 的问题" not in second_req.system_context
    assert "member-a" not in second_req.system_context
    assert "member-b" not in second_req.system_context
    assert metadata["chat_type"] == "qq_group"
    assert metadata["actor_alias"] == "成员B"
    assert metadata["is_owner"] is True
    assert metadata["message_id"] == "m-b"
    assert metadata["group_key"] != "group-raw"
    assert first_req.session_id == "qq_group:group-raw"
    assert "member-a" not in repr(metadata)
    assert "member-b" not in repr(metadata)


@pytest.mark.asyncio
async def test_group_pipeline_failure_retains_buffer_and_groups_are_isolated() -> None:
    bot, captured = _bot([
        None,
        ProcessResult(reply="群二答复"),
        ProcessResult(reply="群一答复"),
    ])

    await bot._run_message_pipeline(
        _message("g1-a", "group-one"), is_group=True,
        user_input="失败后要保留", user_id="qq_a", openid="member-a",
        is_master=False, image_data=None, group_key="group-one",
    )
    await bot._run_message_pipeline(
        _message("g2-a", "group-two"), is_group=True,
        user_input="另一个群", user_id="qq_b", openid="member-b",
        is_master=False, image_data=None, group_key="group-two",
    )
    await bot._run_message_pipeline(
        _message("g1-b", "group-one"), is_group=True,
        user_input="重试", user_id="qq_c", openid="member-c",
        is_master=False, image_data=None, group_key="group-one",
    )

    assert captured[1][0].system_context == ""
    assert "失败后要保留" in captured[2][0].system_context
    assert "另一个群" not in captured[2][0].system_context
