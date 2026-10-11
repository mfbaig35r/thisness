"""Validated field types shared by the core models."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints

from thisness.ids import OBSERVATION_PREFIX, RESERVED_PREFIXES, id_prefix, node_kind


def _prefixed(expected: str):
    def check(value: str) -> str:
        if id_prefix(value) != expected:
            raise ValueError(f"expected a {expected!r} id, got {value!r}")
        return value

    return check


def _entity(value: str) -> str:
    if id_prefix(value) in RESERVED_PREFIXES:
        raise ValueError(f"expected an entity id, got {value!r}")
    return value


def _node(value: str) -> str:
    node_kind(value)  # raises for anything but an observation or entity id
    return value


ObservationId = Annotated[str, AfterValidator(_prefixed(OBSERVATION_PREFIX))]
EntityId = Annotated[str, AfterValidator(_entity)]
NodeId = Annotated[str, AfterValidator(_node)]
MembershipId = Annotated[str, AfterValidator(_prefixed("mem"))]
EvidenceId = Annotated[str, AfterValidator(_prefixed("ev"))]
DecisionId = Annotated[str, AfterValidator(_prefixed("dec"))]
RunId = Annotated[str, AfterValidator(_prefixed("run"))]
StageId = Annotated[str, AfterValidator(_prefixed("stg"))]
ProposalId = Annotated[str, AfterValidator(_prefixed("prop"))]
RelationshipId = Annotated[str, AfterValidator(_prefixed("rel"))]
ReviewId = Annotated[str, AfterValidator(_prefixed("rev"))]
EventId = Annotated[str, AfterValidator(_prefixed("evt"))]
CommitId = Annotated[str, AfterValidator(_prefixed("cmt"))]
ConstraintId = Annotated[str, AfterValidator(_prefixed("con"))]
InvocationId = Annotated[str, AfterValidator(_prefixed("llm"))]

EntityType = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Name = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,127}$")]
NonEmpty = Annotated[str, StringConstraints(min_length=1)]
LedgerSeq = Annotated[int, Field(ge=0)]
