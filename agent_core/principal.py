from dataclasses import dataclass
from typing import Protocol


class OwnerRegistry(Protocol):
    def is_owner(self, subject_id: str) -> bool: ...


@dataclass(frozen=True)
class ChannelIdentity:
    source: str
    user_id: str
    subject_id: str


@dataclass(frozen=True)
class Principal:
    principal_id: str
    is_owner: bool
    display_name: str
    address_term: str


class PrincipalResolver:
    def __init__(self, owner_registry: OwnerRegistry,
                 owner_address_term: str = "爸爸") -> None:
        self._owner_registry = owner_registry
        self._owner_address_term = owner_address_term

    def resolve(self, identity: ChannelIdentity) -> Principal:
        subject_id = identity.subject_id.strip()
        principal_id = identity.user_id.strip() or subject_id
        is_owner = bool(subject_id) and self._owner_registry.is_owner(subject_id)
        if is_owner:
            return Principal(
                principal_id=principal_id,
                is_owner=True,
                display_name="爸爸",
                address_term=self._owner_address_term,
            )
        return Principal(
            principal_id=principal_id or f"anonymous:{identity.source or 'unknown'}",
            is_owner=False,
            display_name="朋友",
            address_term="朋友",
        )
