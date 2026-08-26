from __future__ import annotations

import math
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

SUMMARY_MAX_LENGTH = 4096
REASON_MAX_LENGTH = 2048
PositiveStrictInt = Annotated[StrictInt, Field(gt=0)]


class ReconciliationAction(str, Enum):
    STORE = "store"
    UPDATE = "update"
    MERGE = "merge"
    SKIP = "skip"


class ReconciliationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: PositiveStrictInt
    action: ReconciliationAction
    target_ids: list[PositiveStrictInt]
    canonical_summary: str = Field(max_length=SUMMARY_MAX_LENGTH)
    confidence: float
    reason: str = Field(max_length=REASON_MAX_LENGTH)

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_confidence(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("confidence must be a finite number")
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("confidence must be between 0 and 1")
        return value

    @field_validator("target_ids")
    @classmethod
    def validate_unique_targets(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("target_ids must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_action_cardinality(self) -> ReconciliationDecision:
        count = len(self.target_ids)
        valid = {
            ReconciliationAction.STORE: count == 0,
            ReconciliationAction.UPDATE: count == 1,
            ReconciliationAction.MERGE: count >= 1,
            ReconciliationAction.SKIP: count <= 1,
        }
        if not valid[self.action]:
            raise ValueError(f"invalid target count for {self.action.value}")
        return self


class MemoryIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    memory_id: PositiveStrictInt
    user_id: str
    agent_id: str
    is_raw: bool
    status: str
    version: Annotated[StrictInt, Field(ge=0)]


class DecisionValidationContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    job_id: PositiveStrictInt
    user_id: str
    agent_id: str
    candidate: MemoryIdentity
    targets: dict[int, MemoryIdentity]

    @model_validator(mode="after")
    def validate_candidate_scope(self) -> DecisionValidationContext:
        if self.candidate.user_id != self.user_id or self.candidate.agent_id != self.agent_id:
            raise ValueError("candidate is outside reconciliation scope")
        if any(target_id != target.memory_id for target_id, target in self.targets.items()):
            raise ValueError("target pool key must match memory identity")
        return self

    def validate_decision(self, decision: ReconciliationDecision) -> None:
        if decision.job_id != self.job_id:
            raise ValueError("decision job does not match validation context")
        for target_id in decision.target_ids:
            target = self.targets.get(target_id)
            if target is None:
                raise ValueError("target is outside the candidate pool")
            if target.user_id != self.user_id or target.agent_id != self.agent_id:
                raise ValueError("target is outside reconciliation scope")
            if target.is_raw:
                raise ValueError("raw memories cannot be reconciliation targets")
            if target.status != "active":
                raise ValueError("target must be active")

    def fallback_store(self, candidate_summary: str) -> ReconciliationDecision:
        return ReconciliationDecision(
            job_id=self.job_id,
            action=ReconciliationAction.STORE,
            target_ids=[],
            canonical_summary=candidate_summary[:SUMMARY_MAX_LENGTH],
            confidence=0.0,
            reason="deterministic_fallback",
        )
