from agent_core.conversation_session import ConversationSession
from agent_core.principal import Principal


def test_conversation_session_preserves_identity_and_builds_memory_scope():
    principal = Principal(
        principal_id="person-1",
        is_owner=False,
        display_name="朋友",
        address_term="朋友",
    )

    session = ConversationSession.create(
        principal=principal,
        context_id="shared_context:owner",
        session_id="session-1",
        agent_id="xiaoda",
        source="web",
        channel_subject_id="web-1",
    )
    scope = session.memory_scope("request-1")

    assert session.principal.principal_id == "person-1"
    assert session.context_id == "shared_context:owner"
    assert scope.user_id == "person-1"
    assert scope.session_id == "session-1"
    assert scope.agent_id == "xiaoda"
    assert scope.request_id == "request-1"


def test_conversation_session_normalizes_empty_session_id():
    principal = Principal("person-2", False, "朋友", "朋友")

    session = ConversationSession.create(
        principal=principal,
        context_id="person-2",
        session_id="",
        agent_id="xiaoda",
        source="wechat_c2c",
        channel_subject_id="wechat-2",
    )

    assert session.session_id == "user"


def test_qq_group_session_builds_scope_from_real_group_openid():
    principal = Principal("owner", True, "爸爸", "爸爸")
    session = ConversationSession.create(
        principal=principal,
        context_id="shared_context:owner",
        session_id="qq_group:group-openid-real",
        agent_id="xiaoda",
        source="qq_group",
        channel_subject_id="member-openid",
    )

    scope = session.memory_scope("request-group")

    from memory.scope import Scope

    assert scope == Scope.group(
        user_id="owner", group_id="group-openid-real", request_id="request-group"
    )
    assert session.activation_key == "qq_group:group-openid-real"
    assert "member-openid" not in scope.session_id
