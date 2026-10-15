"""The append-only resolution ledger (design 02 §6.4)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field, JsonValue

from thisness.core.base import Model
from thisness.core.decisions import Actor
from thisness.core.types import CommitId, DecisionId, EntityId, EventId, ObservationId, RunId


class EventType(StrEnum):
    OBSERVATION_INGESTED = "OBSERVATION_INGESTED"
    OBSERVATION_REDACTED = "OBSERVATION_REDACTED"
    RUN_STARTED = "RUN_STARTED"
    RUN_PROPOSED = "RUN_PROPOSED"
    RUN_COMMITTED = "RUN_COMMITTED"
    RUN_ABANDONED = "RUN_ABANDONED"
    ENTITY_CREATED = "ENTITY_CREATED"  # payload carries state (active or provisional)
    ENTITY_PROMOTED = "ENTITY_PROMOTED"
    ENTITY_MERGED = "ENTITY_MERGED"  # payload carries members_at_retirement
    ENTITY_SPLIT = "ENTITY_SPLIT"
    ENTITY_REACTIVATED = "ENTITY_REACTIVATED"
    ENTITY_LABELED = "ENTITY_LABELED"  # payload carries the accepted name (design 08 §5.2)
    ENTITY_LABEL_CLEARED = "ENTITY_LABEL_CLEARED"
    MEMBERSHIP_ACCEPTED = "MEMBERSHIP_ACCEPTED"
    MEMBERSHIP_ENDED = "MEMBERSHIP_ENDED"
    RELATIONSHIP_CREATED = "RELATIONSHIP_CREATED"
    RELATIONSHIP_REVOKED = "RELATIONSHIP_REVOKED"
    REVIEW_OPENED = "REVIEW_OPENED"
    REVIEW_CLOSED = "REVIEW_CLOSED"
    SPLIT_PROPOSED = "SPLIT_PROPOSED"
    CONSTRAINT_ADDED = "CONSTRAINT_ADDED"
    CONSTRAINT_REVOKED = "CONSTRAINT_REVOKED"


class LedgerEvent(Model):
    seq: int = Field(ge=1)
    event_id: EventId
    commit_id: CommitId
    event_type: EventType
    run_id: RunId | None = None
    actor: Actor
    observation_id: ObservationId | None = None
    entity_id: EntityId | None = None
    related_entity_id: EntityId | None = None
    decision_id: DecisionId | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    recorded_at: AwareDatetime
