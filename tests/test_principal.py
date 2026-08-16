from agent_core.principal import ChannelIdentity, PrincipalResolver


class SecurityStub:
    def __init__(self, owners: set[str]) -> None:
        self.owners = owners

    def is_owner(self, subject_id: str) -> bool:
        return subject_id in self.owners or subject_id.removeprefix("qq_") in self.owners


def test_resolver_grants_owner_only_from_stable_registered_subject():
    resolver = PrincipalResolver(SecurityStub({"owner-1"}), owner_address_term="爸爸")

    principal = resolver.resolve(
        ChannelIdentity(source="qq_c2c", user_id="qq_owner-1", subject_id="owner-1")
    )

    assert principal.principal_id == "qq_owner-1"
    assert principal.is_owner is True
    assert principal.address_term == "爸爸"


def test_resolver_grants_owner_for_trusted_web_channel():
    """「登录即主人」模型：web 通道即使 OWNER_IDS 为空也视为主人。

    （旧断言 test_resolver_does_not_grant_owner_from_channel_type 基于
    "web 渠道严格判定"的旧假设，已按新设计更新。）
    """
    resolver = PrincipalResolver(SecurityStub(set()))

    principal = resolver.resolve(
        ChannelIdentity(source="web", user_id="webui", subject_id="webui")
    )

    assert principal.principal_id == "webui"
    assert principal.is_owner is True
    assert principal.address_term == "爸爸"


def test_resolver_grants_owner_for_trusted_cli_channel():
    """cli 通道由本机进程隔离保护，同样视为主人。"""
    resolver = PrincipalResolver(SecurityStub(set()))

    principal = resolver.resolve(
        ChannelIdentity(source="cli", user_id="cli_owner", subject_id="cli_owner")
    )

    assert principal.principal_id == "cli_owner"
    assert principal.is_owner is True


def test_resolver_strict_for_external_qq_group_stranger():
    """外部渠道（qq_group）陌生 subject 保持严格判定：非主人（回归保护）。"""
    resolver = PrincipalResolver(SecurityStub({"owner-1"}))

    principal = resolver.resolve(
        ChannelIdentity(source="qq_group", user_id="qq_stranger", subject_id="stranger")
    )

    assert principal.principal_id == "qq_stranger"
    assert principal.is_owner is False
    assert principal.address_term == "朋友"


def test_resolver_fails_closed_without_stable_subject():
    resolver = PrincipalResolver(SecurityStub({"owner-1"}))

    principal = resolver.resolve(ChannelIdentity(source="qq_c2c", user_id="", subject_id=""))

    assert principal.principal_id == "anonymous:qq_c2c"
    assert principal.is_owner is False
