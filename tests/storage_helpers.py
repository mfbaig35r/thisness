"""A small harness for store tests: ingest by name, record decisions, commit ops."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from thisness.core import (
    Actor,
    ChangeSet,
    ConsensusDecision,
    Observation,
    OperatorDecision,
    ReadSet,
)
from thisness.ids import IdFactory
from thisness.storage.duckdb import DuckDBStore

SYSTEM = Actor(kind="system", id="test")
HUMAN = Actor(kind="human", id="reviewer")


class World:
    def __init__(self, store: DuckDBStore, ids: IdFactory, now: datetime) -> None:
        self.store, self.ids, self.now = store, ids, now
        self._row = 0

    def obs(self, name: str, entity_type: str = "organization", **attributes: Any) -> str:
        self._row += 1
        observation = Observation.new(
            ids=self.ids,
            entity_type=entity_type,
            attributes={"name": name, **attributes},
            source={"type": "csv", "id": "suppliers.csv", "locator": {"row": self._row}},
            ingested_at=self.now,
        )
        self.store.ingest([observation])
        return observation.observation_id

    def decision(self, outcome: str = "MERGE", subjects: tuple[str, ...] = ("x",)) -> str:
        decision = OperatorDecision(
            decision_id=self.ids.decision(),
            actor=HUMAN,
            action=outcome,
            subject_ids=subjects,
            reason="test",
            created_at=self.now,
        )
        self.store.record_decisions([decision])
        return decision.decision_id

    def consensus(self, members: tuple[str, ...], run_id: str | None = None) -> str:
        decision = ConsensusDecision(
            decision_id=self.ids.decision(),
            actor=SYSTEM,
            run_id=run_id or self.ids.run(),
            members=members,
            outcome="CREATE_ENTITY",
            created_at=self.now,
        )
        self.store.record_decisions([decision])
        return decision.decision_id

    def commit(
        self,
        *ops: Any,
        base_seq: int | None = None,
        read_set: ReadSet | None = None,
        run_id: str | None = None,
        actor: Actor = SYSTEM,
    ):
        changes = ChangeSet(
            run_id=run_id,
            base_seq=self.store.head_seq() if base_seq is None else base_seq,
            ops=ops,
            read_set=read_set or ReadSet(),
        )
        return self.store.commit(changes, actor=actor)

    def row_counts(self) -> dict[str, int]:
        tables = [
            "ledger_events",
            "entities",
            "memberships",
            "entity_snapshots",
            "relationships",
            "review_items",
        ]
        return {
            t: self.store._con.execute(f"SELECT count(*) FROM {t}").fetchall()[0][0] for t in tables
        }
