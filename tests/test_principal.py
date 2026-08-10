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


def test_resolver_does_not_grant_owner_from_channel_type():
    resolver = PrincipalResolver(SecurityStub(set()))

    principal = resolver.resolve(
        ChannelIdentity(source="web", user_id="web-user", subject_id="web-user")
    )

    assert principal.principal_id == "web-user"
    assert principal.is_owner is False
    assert principal.address_term == "朋友"


def test_resolver_fails_closed_without_stable_subject():
    resolver = PrincipalResolver(SecurityStub({"owner-1"}))

    principal = resolver.resolve(ChannelIdentity(source="qq_c2c", user_id="", subject_id=""))

    assert principal.principal_id == "anonymous:qq_c2c"
    assert principal.is_owner is False
