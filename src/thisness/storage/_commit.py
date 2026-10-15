"""Commit planning (design 02 §6.5, §7).

``CommitPlan`` applies a ``ChangeSet`` to a working copy of identity state and
produces the rows to write. It reads current state lazily through a
``StateReader`` and writes nothing itself, so a change set that fails any check
leaves the store untouched.

Ledger sequence numbers are assigned here, gapless from the store's head. Each
entity whose identity events appear in the commit gets exactly one version bump,
and every live touched entity gets a snapshot at its new version.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from thisness.core.decisions import Actor
from thisness.core.ledger import EventType
from thisness.core.run import (
    AddConstraint,
    Attach,
    ChangeSet,
    ClearLabel,
    CloseReview,
    CreateEntity,
    Label,
    Merge,
    OpenReview,
    Promote,
    Relate,
    RevokeConstraint,
    RevokeRelationship,
    Split,
)
from thisness.ids import IdFactory
from thisness.storage.errors import InvalidOperation, StaleVersion

# Events that change identity and therefore advance entity versions.
IDENTITY_EVENTS = frozenset(
    {
        EventType.ENTITY_CREATED,
        EventType.ENTITY_PROMOTED,
        EventType.ENTITY_MERGED,
        EventType.ENTITY_SPLIT,
        EventType.ENTITY_REACTIVATED,
        EventType.MEMBERSHIP_ACCEPTED,
        EventType.MEMBERSHIP_ENDED,
        EventType.ENTITY_LABELED,
        EventType.ENTITY_LABEL_CLEARED,
    }
)


@dataclass
class EntityRow:
    entity_id: str
    entity_type: str
    state: str
    version: int
    created_seq: int
    retired_seq: int | None = None
    merged_into: str | None = None
    is_new: bool = False
    dirty: bool = False


@dataclass
class MembershipRow:
    membership_id: str
    observation_id: str
    entity_id: str
    basis_decision_id: str
    valid_from_seq: int
    valid_to_seq: int | None = None
    is_new: bool = False


class StateReader(Protocol):
    # Bulk loads, used to prefetch everything a change set references.
    def load_entities(self, entity_ids: list[str]) -> dict[str, EntityRow]: ...
    def load_observation_types(self, observation_ids: list[str]) -> dict[str, str]: ...
    def load_open_memberships(self, observation_ids: list[str]) -> dict[str, MembershipRow]: ...
    def load_members(self, entity_ids: list[str]) -> dict[str, list[MembershipRow]]: ...
    # Single loads, for anything the prefetch could not anticipate.
    def load_entity(self, entity_id: str) -> EntityRow | None: ...
    def load_open_membership(self, observation_id: str) -> MembershipRow | None: ...
    def load_entity_members(self, entity_id: str) -> list[MembershipRow]: ...
    def load_observation_type(self, observation_id: str) -> str | None: ...
    def relationship_is_open(self, relationship_id: str) -> bool | None: ...  # None: unknown
    def review_status(self, review_id: str) -> str | None: ...
    def run_status(self, run_id: str) -> str | None: ...
    def constraint_is_open(self, constraint_id: str) -> bool | None: ...  # None: unknown
    def load_cannot_links(self, observation_ids: list[str]) -> list[tuple[str, str, str]]: ...


class CommitPlan:
    def __init__(
        self,
        reader: StateReader,
        changes: ChangeSet,
        *,
        actor: Actor,
        ids: IdFactory,
        now: datetime,
        head_seq: int,
    ) -> None:
        self._reader = reader
        self._changes = changes
        self._actor = actor
        self._ids = ids
        self._now = now
        self._head = head_seq
        self.commit_id = ids.commit()
        self.run_id = changes.run_id

        self.events: list[dict[str, Any]] = []
        self.entities: dict[str, EntityRow] = {}
        self._missing_entities: set[str] = set()
        self._open: dict[str, MembershipRow | None] = {}  # observation -> open membership
        self._members: dict[str, set[str]] = {}  # entity -> open member observations
        self._obs_types: dict[str, str | None] = {}
        self.new_memberships: list[MembershipRow] = []
        self.ended_memberships: dict[str, int] = {}  # existing membership_id -> valid_to_seq
        self.new_relationships: list[dict[str, Any]] = []
        self.revoked_relationships: dict[str, int] = {}
        self.new_reviews: list[dict[str, Any]] = []
        self.closed_reviews: dict[str, dict[str, Any]] = {}
        self.snapshot_targets: list[tuple[str, int, list[str]]] = []
        self.touched: list[str] = []
        self.new_constraints: list[dict[str, Any]] = []
        self.revoked_constraints: dict[str, int] = {}

    # -- public ----------------------------------------------------------------

    def apply(self) -> None:
        handlers = {
            "create_entity": self._create,
            "attach": self._attach,
            "merge": self._merge,
            "split": self._split,
            "promote": self._promote,
            "label": self._label,
            "clear_label": self._clear_label,
            "relate": self._relate,
            "revoke_relationship": self._revoke,
            "open_review": self._open_review,
            "close_review": self._close_review,
            "add_constraint": self._add_constraint,
            "revoke_constraint": self._revoke_constraint,
        }
        self._check_run()
        self._prefetch()
        for index, op in enumerate(self._changes.ops):
            handlers[op.op](index, op)
        if self.run_id is not None:
            self._emit(EventType.RUN_COMMITTED, decision_id=None)
        self._finalize_versions()
        self._check_cannot_links()

    @property
    def first_seq(self) -> int:
        return self._head + 1

    @property
    def last_seq(self) -> int:
        return self._head + len(self.events)

    # -- state access ------------------------------------------------------------

    def _prefetch(self) -> None:
        """Load every entity, observation and membership the ops name, in four queries,
        so applying a large change set does not issue one query per row."""
        entity_ids: set[str] = set()
        observation_ids: set[str] = set()
        for op in self._changes.ops:
            match op.op:
                case "create_entity":
                    entity_ids.add(op.entity_id)
                    observation_ids.update(op.members)
                case "attach":
                    entity_ids.add(op.entity_id)
                    observation_ids.add(op.observation_id)
                case "merge":
                    entity_ids.update((op.survivor_id, *op.merged_ids))
                case "split":
                    entity_ids.update((op.source_id, *op.targets))
                    for observations in op.targets.values():
                        observation_ids.update(observations)
                case "promote" | "label" | "clear_label":
                    entity_ids.add(op.entity_id)
                case "relate":
                    entity_ids.update((op.subject_entity_id, op.object_entity_id))
        found = self._reader.load_entities(sorted(entity_ids))
        self.entities.update(found)
        self._missing_entities.update(entity_ids - found.keys())
        types = self._reader.load_observation_types(sorted(observation_ids))
        for obs in observation_ids:
            self._obs_types[obs] = types.get(obs)
        open_rows = self._reader.load_open_memberships(sorted(observation_ids))
        for obs in observation_ids:
            self._open[obs] = open_rows.get(obs)
        members = self._reader.load_members(sorted(found))
        for entity_id in found:
            rows = members.get(entity_id, [])
            for row in rows:
                self._open.setdefault(row.observation_id, row)
            self._members[entity_id] = {row.observation_id for row in rows}

    def _maybe_entity(self, entity_id: str) -> EntityRow | None:
        if entity_id in self.entities:
            return self.entities[entity_id]
        if entity_id in self._missing_entities:
            return None
        row = self._reader.load_entity(entity_id)
        if row is None:
            self._missing_entities.add(entity_id)
        else:
            self.entities[entity_id] = row
        return row

    def _entity(self, index: int, entity_id: str, *, live: bool = True) -> EntityRow:
        row = self._maybe_entity(entity_id)
        if row is None:
            raise InvalidOperation(index, f"entity {entity_id} does not exist")
        if live and row.state == "retired":
            raise InvalidOperation(index, f"entity {entity_id} is retired")
        return row

    def _observation_type(self, index: int, observation_id: str) -> str:
        if observation_id not in self._obs_types:
            self._obs_types[observation_id] = self._reader.load_observation_type(observation_id)
        found = self._obs_types[observation_id]
        if found is None:
            raise InvalidOperation(index, f"observation {observation_id} does not exist")
        return found

    def _open_membership(self, observation_id: str) -> MembershipRow | None:
        if observation_id not in self._open:
            self._open[observation_id] = self._reader.load_open_membership(observation_id)
        return self._open[observation_id]

    def _entity_members(self, entity_id: str) -> set[str]:
        # Loaded before the first change to the entity in this plan, then kept current.
        if entity_id not in self._members:
            members = set()
            for row in self._reader.load_entity_members(entity_id):
                self._open.setdefault(row.observation_id, row)
                members.add(row.observation_id)
            self._members[entity_id] = members
        return self._members[entity_id]

    # -- events and memberships ----------------------------------------------------

    def _emit(
        self,
        event_type: EventType,
        *,
        decision_id: str | None,
        observation_id: str | None = None,
        entity_id: str | None = None,
        related_entity_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        seq = self._head + len(self.events) + 1
        self.events.append(
            {
                "seq": seq,
                "event_id": self._ids.event(),
                "commit_id": self.commit_id,
                "event_type": str(event_type),
                "run_id": self._changes.run_id,
                "actor_kind": self._actor.kind,
                "actor_id": self._actor.id,
                "observation_id": observation_id,
                "entity_id": entity_id,
                "related_entity_id": related_entity_id,
                "decision_id": decision_id,
                "payload": json.dumps(payload or {}, sort_keys=True),
                "recorded_at": self._now,
            }
        )
        return seq

    def _accept(self, observation_id: str, entity_id: str, decision_id: str) -> None:
        members = self._entity_members(entity_id)
        membership_id = self._ids.membership()
        seq = self._emit(
            EventType.MEMBERSHIP_ACCEPTED,
            decision_id=decision_id,
            observation_id=observation_id,
            entity_id=entity_id,
            payload={"membership_id": membership_id},
        )
        row = MembershipRow(membership_id, observation_id, entity_id, decision_id, seq, is_new=True)
        self.new_memberships.append(row)
        self._open[observation_id] = row
        members.add(observation_id)

    def _end(self, observation_id: str, decision_id: str) -> None:
        row = self._open_membership(observation_id)
        assert row is not None
        members = self._entity_members(row.entity_id)
        seq = self._emit(
            EventType.MEMBERSHIP_ENDED,
            decision_id=decision_id,
            observation_id=observation_id,
            entity_id=row.entity_id,
            payload={"membership_id": row.membership_id},
        )
        if row.is_new:
            row.valid_to_seq = seq
        else:
            self.ended_memberships[row.membership_id] = seq
        self._open[observation_id] = None
        members.discard(observation_id)

    def _move(self, observation_id: str, entity_id: str, decision_id: str) -> None:
        self._end(observation_id, decision_id)
        self._accept(observation_id, entity_id, decision_id)

    def _new_entity(self, entity_id: str, entity_type: str, state: str, decision_id: str) -> None:
        seq = self._emit(
            EventType.ENTITY_CREATED,
            decision_id=decision_id,
            entity_id=entity_id,
            payload={"entity_type": entity_type, "state": state},
        )
        self.entities[entity_id] = EntityRow(
            entity_id, entity_type, state, version=0, created_seq=seq, is_new=True
        )
        self._missing_entities.discard(entity_id)
        self._members[entity_id] = set()

    # -- operations --------------------------------------------------------------------

    def _create(self, index: int, op: CreateEntity) -> None:
        if self._maybe_entity(op.entity_id) is not None:
            raise InvalidOperation(index, f"entity {op.entity_id} already exists")
        for obs in op.members:
            self._check_attachable(index, obs, op.entity_type)
        self._new_entity(op.entity_id, op.entity_type, op.state, op.decision_id)
        for obs in op.members:
            self._accept(obs, op.entity_id, op.decision_id)

    def _attach(self, index: int, op: Attach) -> None:
        entity = self._entity(index, op.entity_id)
        self._check_attachable(index, op.observation_id, entity.entity_type)
        self._accept(op.observation_id, op.entity_id, op.decision_id)

    def _check_attachable(self, index: int, observation_id: str, entity_type: str) -> None:
        found_type = self._observation_type(index, observation_id)
        if found_type != entity_type:
            raise InvalidOperation(
                index, f"observation {observation_id} is a {found_type}, not a {entity_type}"
            )
        current = self._open_membership(observation_id)
        if current is not None:
            raise InvalidOperation(
                index,
                f"observation {observation_id} already belongs to {current.entity_id}; "
                "use merge or split to move it",
            )

    @staticmethod
    def _check_version(entity: EntityRow, expected: int | None) -> None:
        if expected is not None and entity.version != expected:
            raise StaleVersion(entity.entity_id, expected, entity.version)

    def _merge(self, index: int, op: Merge) -> None:
        survivor = self._entity(index, op.survivor_id)
        expected = op.expected_versions or {}
        unknown = set(expected) - {op.survivor_id, *op.merged_ids}
        if unknown:
            raise InvalidOperation(index, f"expected_versions names unrelated entities {unknown}")
        self._check_version(survivor, expected.get(op.survivor_id))
        for merged_id in op.merged_ids:
            merged = self._entity(index, merged_id)
            self._check_version(merged, expected.get(merged_id))
            if merged.entity_type != survivor.entity_type:
                raise InvalidOperation(
                    index, f"cannot merge a {merged.entity_type} into a {survivor.entity_type}"
                )
            final_members = sorted(self._entity_members(merged_id))
            for obs in final_members:
                self._move(obs, op.survivor_id, op.decision_id)
            seq = self._emit(
                EventType.ENTITY_MERGED,
                decision_id=op.decision_id,
                entity_id=merged_id,
                related_entity_id=op.survivor_id,
                payload={"members_at_retirement": final_members},
            )
            merged.state, merged.retired_seq, merged.merged_into = "retired", seq, op.survivor_id
            merged.dirty = True

    def _split(self, index: int, op: Split) -> None:
        source = self._entity(index, op.source_id)
        self._check_version(source, op.expected_version)
        source_members = self._entity_members(op.source_id)
        for observations in op.targets.values():
            outside = sorted(set(observations) - source_members)
            if outside:
                raise InvalidOperation(index, f"{outside} are not members of {op.source_id}")
        self._emit(
            EventType.ENTITY_SPLIT,
            decision_id=op.decision_id,
            entity_id=op.source_id,
            payload={"targets": sorted(op.targets)},
        )
        for target_id in sorted(op.targets):
            target = self._maybe_entity(target_id)
            if target is None:
                self._new_entity(target_id, source.entity_type, source.state, op.decision_id)
            elif target.state == "retired":
                if target.entity_type != source.entity_type:
                    raise InvalidOperation(index, f"{target_id} is a {target.entity_type}")
                self._emit(
                    EventType.ENTITY_REACTIVATED, decision_id=op.decision_id, entity_id=target_id
                )
                target.state, target.retired_seq, target.merged_into = "active", None, None
                target.dirty = True
            else:
                raise InvalidOperation(
                    index, f"split target {target_id} is live; split into a new or retired entity"
                )
            for obs in sorted(op.targets[target_id]):
                self._move(obs, target_id, op.decision_id)
        if not source_members:
            raise InvalidOperation(index, f"split would leave {op.source_id} with no members")

    def _promote(self, index: int, op: Promote) -> None:
        entity = self._entity(index, op.entity_id)
        if entity.state != "provisional":
            raise InvalidOperation(index, f"{op.entity_id} is {entity.state}, not provisional")
        self._emit(EventType.ENTITY_PROMOTED, decision_id=op.decision_id, entity_id=op.entity_id)
        entity.state, entity.dirty = "active", True

    def _label(self, index: int, op: Label) -> None:
        entity = self._entity(index, op.entity_id)
        if entity.state == "retired":
            raise InvalidOperation(index, f"{op.entity_id} is retired")
        self._emit(EventType.ENTITY_LABELED, decision_id=op.decision_id, entity_id=op.entity_id,
                   payload={"name": op.name, "suggestion_id": op.suggestion_id})  # fmt: skip
        entity.dirty = True

    def _clear_label(self, index: int, op: ClearLabel) -> None:
        entity = self._entity(index, op.entity_id)
        if entity.state == "retired":
            raise InvalidOperation(index, f"{op.entity_id} is retired")
        self._emit(EventType.ENTITY_LABEL_CLEARED, decision_id=op.decision_id,
                   entity_id=op.entity_id)  # fmt: skip
        entity.dirty = True

    def _relate(self, index: int, op: Relate) -> None:
        self._entity(index, op.subject_entity_id)
        self._entity(index, op.object_entity_id)
        known = self._reader.relationship_is_open(op.relationship_id) is not None
        if known or any(r["relationship_id"] == op.relationship_id for r in self.new_relationships):
            raise InvalidOperation(index, f"relationship {op.relationship_id} already exists")
        seq = self._emit(
            EventType.RELATIONSHIP_CREATED,
            decision_id=op.decision_id,
            entity_id=op.subject_entity_id,
            related_entity_id=op.object_entity_id,
            payload={"relationship_id": op.relationship_id, "relation": op.relation},
        )
        self.new_relationships.append(
            {
                "relationship_id": op.relationship_id,
                "relation": op.relation,
                "subject_entity_id": op.subject_entity_id,
                "object_entity_id": op.object_entity_id,
                "basis_decision_id": op.decision_id,
                "evidence_ids": json.dumps(list(op.evidence_ids)),
                "effective_from": op.effective_from,
                "effective_to": op.effective_to,
                "valid_from_seq": seq,
                "valid_to_seq": None,
            }
        )

    def _revoke(self, index: int, op: RevokeRelationship) -> None:
        new = next(
            (r for r in self.new_relationships if r["relationship_id"] == op.relationship_id), None
        )
        is_open = (
            new["valid_to_seq"] is None
            if new is not None
            else self._reader.relationship_is_open(op.relationship_id)
            and op.relationship_id not in self.revoked_relationships
        )
        if not is_open:
            raise InvalidOperation(index, f"relationship {op.relationship_id} is not open")
        seq = self._emit(
            EventType.RELATIONSHIP_REVOKED,
            decision_id=op.decision_id,
            payload={"relationship_id": op.relationship_id},
        )
        if new is not None:
            new["valid_to_seq"] = seq
        else:
            self.revoked_relationships[op.relationship_id] = seq

    def _open_review(self, index: int, op: OpenReview) -> None:
        exists = self._reader.review_status(op.review_id) is not None or any(
            r["review_id"] == op.review_id for r in self.new_reviews
        )
        if exists:
            raise InvalidOperation(index, f"review {op.review_id} already exists")
        seq = self._emit(
            EventType.REVIEW_OPENED,
            decision_id=op.decision_id,
            payload={"review_id": op.review_id, "kind": op.kind},
        )
        self.new_reviews.append(
            {
                "review_id": op.review_id,
                "kind": op.kind,
                "run_id": self._changes.run_id,
                "decision_id": op.decision_id,
                "members": json.dumps(list(op.members)),
                "status": "open",
                "opened_seq": seq,
                "closed_seq": None,
                "resolution_decision_id": None,
            }
        )

    def _close_review(self, index: int, op: CloseReview) -> None:
        new = next((r for r in self.new_reviews if r["review_id"] == op.review_id), None)
        status = new["status"] if new is not None else self._reader.review_status(op.review_id)
        if status != "open" or op.review_id in self.closed_reviews:
            raise InvalidOperation(index, f"review {op.review_id} is not open")
        seq = self._emit(
            EventType.REVIEW_CLOSED,
            decision_id=op.decision_id,
            payload={"review_id": op.review_id, "status": op.status},
        )
        closure = {"status": op.status, "closed_seq": seq, "resolution_decision_id": op.decision_id}
        if new is not None:
            new.update(closure)
        else:
            self.closed_reviews[op.review_id] = closure

    def _add_constraint(self, index: int, op: AddConstraint) -> None:
        for obs in (op.left_id, op.right_id):
            self._observation_type(index, obs)
        known = self._reader.constraint_is_open(op.constraint_id) is not None
        if known or any(c["constraint_id"] == op.constraint_id for c in self.new_constraints):
            raise InvalidOperation(index, f"constraint {op.constraint_id} already exists")
        seq = self._emit(
            EventType.CONSTRAINT_ADDED,
            decision_id=op.decision_id,
            observation_id=op.left_id,
            payload={"constraint_id": op.constraint_id, "kind": op.kind,
                     "left_id": op.left_id, "right_id": op.right_id},
        )  # fmt: skip
        self.new_constraints.append(
            {
                "constraint_id": op.constraint_id,
                "kind": op.kind,
                "left_id": op.left_id,
                "right_id": op.right_id,
                "basis_decision_id": op.decision_id,
                "valid_from_seq": seq,
                "valid_to_seq": None,
            }
        )

    def _revoke_constraint(self, index: int, op: RevokeConstraint) -> None:
        new = next(
            (c for c in self.new_constraints if c["constraint_id"] == op.constraint_id), None
        )
        is_open = (
            new["valid_to_seq"] is None
            if new is not None
            else self._reader.constraint_is_open(op.constraint_id)
            and op.constraint_id not in self.revoked_constraints
        )
        if not is_open:
            raise InvalidOperation(index, f"constraint {op.constraint_id} is not open")
        seq = self._emit(
            EventType.CONSTRAINT_REVOKED,
            decision_id=op.decision_id,
            payload={"constraint_id": op.constraint_id},
        )
        if new is not None:
            new["valid_to_seq"] = seq
        else:
            self.revoked_constraints[op.constraint_id] = seq

    def _check_cannot_links(self) -> None:
        """No live entity may end the commit holding both sides of an open cannot-link,
        whichever operation brought them together (design 04 §2.5)."""
        entity_of: dict[str, str] = {}
        for entity_id in self.touched:
            if self.entities[entity_id].state != "retired":
                for obs in self._entity_members(entity_id):
                    entity_of[obs] = entity_id
        for c in self.new_constraints:
            if c["kind"] == "cannot_link" and c["valid_to_seq"] is None:
                for obs in (c["left_id"], c["right_id"]):
                    row = self._open_membership(obs)
                    if row is not None:
                        entity_of.setdefault(obs, row.entity_id)
        if not entity_of:
            return
        links = [
            (cid, a, b)
            for cid, a, b in self._reader.load_cannot_links(sorted(entity_of))
            if cid not in self.revoked_constraints
        ]
        links += [
            (c["constraint_id"], c["left_id"], c["right_id"])
            for c in self.new_constraints
            if c["kind"] == "cannot_link" and c["valid_to_seq"] is None
        ]
        for cid, a, b in sorted(links):
            if a in entity_of and entity_of.get(a) == entity_of.get(b):
                raise InvalidOperation(
                    -1,
                    f"cannot-link {cid} would leave {a} and {b} together in {entity_of[a]}; "
                    "revoke it first",
                )

    # -- finish ----------------------------------------------------------------------------

    def _check_run(self) -> None:
        if self.run_id is None:
            return
        status = self._reader.run_status(self.run_id)
        if status is None:
            raise InvalidOperation(-1, f"run {self.run_id} was never recorded")
        if status in ("committed", "abandoned"):
            raise InvalidOperation(-1, f"run {self.run_id} is already {status}")

    def _finalize_versions(self) -> None:
        touched: set[str] = set()
        for event in self.events:
            if event["event_type"] in IDENTITY_EVENTS:
                touched.update(
                    e for e in (event["entity_id"], event["related_entity_id"]) if e is not None
                )
        self.touched = sorted(touched)
        for entity_id in self.touched:
            row = self.entities[entity_id]
            row.version += 1
            row.dirty = True
            if row.state != "retired":
                members = sorted(self._entity_members(entity_id))
                self.snapshot_targets.append((entity_id, row.version, members))
