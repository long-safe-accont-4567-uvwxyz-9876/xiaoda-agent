from types import SimpleNamespace
from unittest.mock import MagicMock

from loguru import logger

from agent_core._shared import ProcessResult, _current_request_ctx
from agent_core.core import AgentCore
from memory.scope import current_scope


def make_core(owner_ids: set[str]) -> AgentCore:
    core = AgentCore.__new__(AgentCore)
    core.security = MagicMock()
    core.security.is_owner.side_effect = lambda subject_id: subject_id in owner_ids
    core.read_address_term_from_user_md = MagicMock(return_value="爸爸")
    return core


def test_agent_core_treats_web_user_as_owner_by_channel_trust():
    """「登录即主人」模型：web 通道即使 owner registry 为空也解析为主人。

    （旧断言 test_agent_core_does_not_promote_web_user_from_source 基于
    "web 渠道严格判定"的旧假设，已按新设计更新。）
    """
    core = make_core(set())

    principal = core._resolve_principal("webui", "webui", "web")

    assert principal.principal_id == "webui"
    assert principal.is_owner is True


def test_agent_core_strict_for_external_qq_group_stranger():
    """外部渠道（qq_group）陌生 subject 仍严格判定为非主人（回归保护）。"""
    core = make_core({"owner-openid"})

    principal = core._resolve_principal("qq_stranger", "stranger-openid", "qq_group")

    assert principal.is_owner is False


def test_agent_core_builds_session_with_separate_principal_and_context_ids():
    core = make_core({"owner-openid"})
    principal = core._resolve_principal("qq_owner", "owner-openid", "qq_c2c")

    session = core._build_conversation_session(
        principal=principal,
        context_id="shared_context:shared",
        session_id="session-1",
        source="qq_c2c",
        channel_subject_id="owner-openid",
    )

    assert session.principal.principal_id == "qq_owner"
    assert session.context_id == "shared_context:shared"
    assert session.memory_scope("request-1").user_id == "qq_owner"


async def test_process_binds_principal_session_scope_and_diagnostic_events(monkeypatch):
    core = make_core({"owner-openid"})
    core._initialized = True
    core.context = SimpleNamespace(current_address_term="")
    core._hook_engine = MagicMock()
    monkeypatch.setattr(
        core,
        "_resolve_shared_context_id",
        lambda *_: "shared_context:family",
    )
    observed = {}

    async def process_impl(ctx, *args, **kwargs):
        observed["ctx"] = _current_request_ctx.get()
        observed["scope"] = current_scope()
        return ProcessResult(reply="ok")

    monkeypatch.setattr(core, "_process_impl_locked", process_impl)
    events = []
    sink_id = logger.add(lambda message: events.append(message.record), level="DEBUG")
    try:
        result = await core.process(
            "测试",
            user_id="qq_owner",
            user_openid="owner-openid",
            source="qq_c2c",
            session_id="session-1",
        )
    finally:
        logger.remove(sink_id)

    assert result.reply == "ok"
    assert observed["ctx"].principal.principal_id == "qq_owner"
    assert observed["ctx"].conversation_session.context_id == "shared_context:family"
    assert observed["scope"].user_id == "qq_owner"
    principal_log = next(
        record for record in events
        if record["message"] == "agent.principal_resolved"
    )["extra"]
    session_log = next(
        record for record in events
        if record["message"] == "agent.session_bound"
    )["extra"]
    assert principal_log["principal_id"] == "qq_owner"
    assert principal_log["is_owner"] is True
    assert session_log["context_id"] == "shared_context:family"
    assert session_log["scope_user_id"] == "qq_owner"
    assert session_log["request_id"] == observed["scope"].request_id
