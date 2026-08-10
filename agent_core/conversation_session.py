from dataclasses import dataclass

from agent_core.principal import Principal
from memory.scope import Scope


@dataclass(frozen=True)
class ConversationSession:
    principal: Principal
    context_id: str
    session_id: str
    agent_id: str
    source: str
    channel_subject_id: str

    @classmethod
    def create(cls, principal: Principal, context_id: str, session_id: str,
               agent_id: str, source: str,
               channel_subject_id: str) -> "ConversationSession":
        return cls(
            principal=principal,
            context_id=context_id or principal.principal_id,
            session_id=session_id or "user",
            agent_id=agent_id,
            source=source,
            channel_subject_id=channel_subject_id,
        )

    def memory_scope(self, request_id: str = "") -> Scope:
        return Scope(
            user_id=self.principal.principal_id,
            session_id=self.session_id,
            agent_id=self.agent_id,
            request_id=request_id,
        )
