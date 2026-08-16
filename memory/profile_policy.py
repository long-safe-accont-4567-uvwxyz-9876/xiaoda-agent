import time
import math
import json
from dataclasses import dataclass
from typing import ClassVar, Any

from db.profile_store import ProfileField, ProfileStore
from memory.scope import Scope


@dataclass(frozen=True, slots=True)
class ProfileCandidate:
    namespace: str
    field_key: str
    value: Any
    confidence: float
    source_type: str
    source_id: str
    effective_at: float | None = None


@dataclass(frozen=True, slots=True)
class ProfileDecision:
    status: str
    reason: str
    field: ProfileField | None = None


class ProfilePolicy:
    _VALUE_TYPES: ClassVar[set[str]] = {"string", "integer", "number", "boolean", "string_list"}
    _PYTHON_TYPES: ClassVar[dict[str, type]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "string_list": list,
    }

    def __init__(self, store: ProfileStore, *, confidence_threshold: float = 0.8) -> None:
        self._store = store
        self._confidence_threshold = confidence_threshold
        self._fields: dict[tuple[str, str], str] = {
            ("identity", "preferred_name"): "string",
            ("identity", "occupation"): "string",
            ("finance", "purchase_budget"): "integer",
            ("locale", "timezone"): "string",
            ("preferences", "dietary_preferences"): "string_list",
            ("preferences", "communication_style"): "string",
        }

    def register_field(self, namespace: str, field_key: str, *, value_type: str) -> None:
        if value_type not in self._VALUE_TYPES:
            raise ValueError(f"unsupported profile value type: {value_type}")
        namespace = namespace.strip()
        field_key = field_key.strip()
        if not namespace or not field_key:
            raise ValueError("namespace and field_key must not be empty")
        self._fields[(namespace, field_key)] = value_type

    def _validate_value(self, value: Any, value_type: str) -> bool:
        if value_type == "integer" and isinstance(value, bool):
            return False
        if value_type == "number" and isinstance(value, bool):
            return False
        if not isinstance(value, self._PYTHON_TYPES[value_type]):
            return False
        if value_type == "string_list":
            return all(isinstance(item, str) for item in value)
        return True

    @staticmethod
    def _value_too_large(value: Any) -> bool:
        if isinstance(value, str):
            return len(value) > 4096
        if isinstance(value, list):
            return len(value) > 32 or any(len(item) > 512 for item in value if isinstance(item, str))
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False)) > 8192

    async def apply(
        self,
        scope: Scope,
        candidate: ProfileCandidate,
        *,
        known_at: float | None = None,
    ) -> ProfileDecision:
        known_at = time.time() if known_at is None else known_at
        value_type = self._fields.get((candidate.namespace, candidate.field_key))
        status = "accepted"
        reason = "accepted"
        field = None
        if (
            isinstance(candidate.confidence, bool)
            or not isinstance(candidate.confidence, (int, float))
            or not math.isfinite(candidate.confidence)
            or not 0 <= candidate.confidence <= 1
        ):
            status = "rejected"
            reason = "invalid_confidence"
        elif value_type is None:
            status = "rejected"
            reason = "unknown_field"
        elif candidate.confidence < self._confidence_threshold:
            status = "rejected"
            reason = "confidence_below_threshold"
        elif not self._validate_value(candidate.value, value_type):
            status = "rejected"
            reason = "invalid_value_type"
        elif self._value_too_large(candidate.value):
            status = "rejected"
            reason = "value_too_large"
        else:
            field = await self._store.put_with_event(
                user_id=scope.user_id,
                agent_id=scope.agent_id,
                session_id=scope.session_id,
                namespace=candidate.namespace,
                field_key=candidate.field_key,
                value=candidate.value,
                value_type=value_type,
                confidence=candidate.confidence,
                source_type=candidate.source_type,
                source_id=candidate.source_id,
                effective_at=candidate.effective_at,
                known_at=known_at,
            )
        if status != "accepted":
            event_confidence = (
                float(candidate.confidence)
                if isinstance(candidate.confidence, (int, float))
                and not isinstance(candidate.confidence, bool)
                and math.isfinite(candidate.confidence)
                else 0.0
            )
            await self._store.record_event(
                user_id=scope.user_id,
                agent_id=scope.agent_id,
                session_id=scope.session_id,
                namespace=candidate.namespace,
                field_key=candidate.field_key,
                candidate_value=candidate.value,
                confidence=event_confidence,
                status=status,
                reason=reason,
                source_type=candidate.source_type,
                source_id=candidate.source_id,
                field_id=None,
                recorded_at=known_at,
            )
        return ProfileDecision(status=status, reason=reason, field=field)
