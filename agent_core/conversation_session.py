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

    @property
    def activation_key(self) -> str:
        """Return the AgentContext key for this privacy boundary."""
        if self.source == "qq_group":
            return self.memory_scope().session_id
        return self.context_id

    def memory_scope(self, request_id: str = "") -> Scope:
        if self.source == "qq_group":
            group_id = self.session_id.removeprefix("qq_group:")
            return Scope.group(
                user_id=self.principal.principal_id,
                group_id=group_id,
                agent_id=self.agent_id,
                request_id=request_id,
            )
        return Scope.personal(
            user_id=self.principal.principal_id,
            session_id=self.session_id,
            agent_id=self.agent_id,
            request_id=request_id,
        )
