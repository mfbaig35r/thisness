"""Observations: immutable claims from a source (design 02 §6.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Self

from pydantic import AwareDatetime, Field, JsonValue, TypeAdapter, model_validator

from thisness._canonical import digest
from thisness.core.base import Model
from thisness.core.types import EntityType, NonEmpty, ObservationId
from thisness.ids import IdFactory

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class SourceRef(Model):
    type: NonEmpty
    id: NonEmpty
    locator: dict[str, JsonValue] = Field(default_factory=dict)
    version: str | None = None


def observation_fingerprint(entity_type: str, source: SourceRef, attributes: dict[str, Any]) -> str:
    """Identifies the same source record so re-ingesting it is idempotent.

    The same text in two documents has two source refs, so it is two observations.
    Context is deliberately excluded: re-extracting a record with richer context is
    still the same observation.
    """
    return digest(
        {
            "entity_type": entity_type,
            "source": source.model_dump(mode="json"),
            "attributes": attributes,
        }
    )[:32]


class Observation(Model):
    observation_id: ObservationId
    entity_type: EntityType
    attributes: dict[str, JsonValue] | None
    source: SourceRef
    context: dict[str, JsonValue] = Field(default_factory=dict)
    observed_at: AwareDatetime | None = None
    ingested_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[0-9a-f]{32}$")
    supersedes: ObservationId | None = None
    redacted_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _redaction_is_consistent(self) -> Self:
        if (self.attributes is None) != (self.redacted_at is not None):
            raise ValueError("attributes are None exactly when the observation is redacted")
        if self.supersedes == self.observation_id:
            raise ValueError("an observation cannot supersede itself")
        return self

    @classmethod
    def new(
        cls,
        *,
        ids: IdFactory,
        entity_type: str,
        attributes: dict[str, Any],
        source: SourceRef | dict[str, Any],
        context: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
        ingested_at: datetime | None = None,
        supersedes: str | None = None,
    ) -> Observation:
        source = source if isinstance(source, SourceRef) else SourceRef.model_validate(source)
        attributes = _JSON_OBJECT.validate_python(attributes)  # before fingerprinting
        return cls(
            observation_id=ids.observation(),
            entity_type=entity_type,
            attributes=attributes,
            source=source,
            context=context or {},
            observed_at=observed_at,
            ingested_at=ingested_at or datetime.now(UTC),
            fingerprint=observation_fingerprint(entity_type, source, attributes),
            supersedes=supersedes,
        )
