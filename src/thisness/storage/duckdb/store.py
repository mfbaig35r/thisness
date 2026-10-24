"""DuckDB store (design 02 §8-10): file-backed or ``:memory:``.

Writes are serialized by a lock and run in one transaction each. Bulk rows move
through Arrow; DuckDB ingests an Arrow table in one scan instead of one round
trip per row.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from typing import Any

import duckdb
import pyarrow as pa
from pydantic import TypeAdapter

from thisness.canonicalize import Canonicalizer, MostFrequentValue
from thisness.core.components import ComponentRef
from thisness.core.decisions import (
    Actor,
    ConsensusDecision,
    Decision,
    LabelSuggestion,
    OperatorDecision,
    PairJudgment,
    ReviewDecision,
    ReviewRecommendation,
)
from thisness.core.evidence import EvidenceItem, Score
from thisness.core.identity import (
    Entity,
    EntitySnapshot,
    PairConstraint,
    RelationshipAssertion,
)
from thisness.core.invocation import ModelInvocation
from thisness.core.ledger import EventType, LedgerEvent
from thisness.core.observation import Observation, SourceRef
from thisness.core.review import ReviewItem
from thisness.core.run import (
    ChangeSet,
    GroupProposal,
    ProposalMember,
    ResolutionRun,
    StageExecution,
)
from thisness.ids import IdFactory
from thisness.storage._commit import CommitPlan, EntityRow, MembershipRow
from thisness.storage.errors import CommitConflict, InvalidOperation, NotFound, UnknownDecision
from thisness.storage.protocol import CommitResult, IngestResult

SCHEMA_VERSION = 1
_DECISION = TypeAdapter(Decision)
_TS = pa.timestamp("us", tz="UTC")

# Column name, Arrow type, and whether the target column is JSON.
_COLUMNS: dict[str, list[tuple[str, pa.DataType, bool]]] = {
    "observations": [
        ("observation_id", pa.string(), False),
        ("entity_type", pa.string(), False),
        ("fingerprint", pa.string(), False),
        ("source_type", pa.string(), False),
        ("source_id", pa.string(), False),
        ("source_locator", pa.string(), True),
        ("source_version", pa.string(), False),
        ("attributes", pa.string(), True),
        ("context", pa.string(), True),
        ("observed_at", _TS, False),
        ("ingested_at", _TS, False),
        ("supersedes", pa.string(), False),
        ("redacted_at", _TS, False),
    ],
    "ledger_events": [
        ("seq", pa.int64(), False),
        ("event_id", pa.string(), False),
        ("commit_id", pa.string(), False),
        ("event_type", pa.string(), False),
        ("run_id", pa.string(), False),
        ("actor_kind", pa.string(), False),
        ("actor_id", pa.string(), False),
        ("observation_id", pa.string(), False),
        ("entity_id", pa.string(), False),
        ("related_entity_id", pa.string(), False),
        ("decision_id", pa.string(), False),
        ("payload", pa.string(), True),
        ("recorded_at", _TS, False),
    ],
    "entities": [
        ("entity_id", pa.string(), False),
        ("entity_type", pa.string(), False),
        ("state", pa.string(), False),
        ("version", pa.int32(), False),
        ("created_seq", pa.int64(), False),
        ("retired_seq", pa.int64(), False),
        ("merged_into", pa.string(), False),
    ],
    "memberships": [
        ("membership_id", pa.string(), False),
        ("observation_id", pa.string(), False),
        ("entity_id", pa.string(), False),
        ("basis_decision_id", pa.string(), False),
        ("valid_from_seq", pa.int64(), False),
        ("valid_to_seq", pa.int64(), False),
    ],
    "entity_snapshots": [
        ("entity_id", pa.string(), False),
        ("version", pa.int32(), False),
        ("attributes", pa.string(), True),
        ("derived_from", pa.string(), True),
        ("canonicalizer_ref", pa.string(), False),
        ("created_seq", pa.int64(), False),
    ],
    "relationships": [
        ("relationship_id", pa.string(), False),
        ("relation", pa.string(), False),
        ("subject_entity_id", pa.string(), False),
        ("object_entity_id", pa.string(), False),
        ("basis_decision_id", pa.string(), False),
        ("evidence_ids", pa.string(), True),
        ("effective_from", pa.date32(), False),
        ("effective_to", pa.date32(), False),
        ("valid_from_seq", pa.int64(), False),
        ("valid_to_seq", pa.int64(), False),
    ],
    "review_items": [
        ("review_id", pa.string(), False),
        ("kind", pa.string(), False),
        ("run_id", pa.string(), False),
        ("decision_id", pa.string(), False),
        ("members", pa.string(), True),
        ("status", pa.string(), False),
        ("opened_seq", pa.int64(), False),
        ("closed_seq", pa.int64(), False),
        ("resolution_decision_id", pa.string(), False),
    ],
    "pair_constraints": [
        ("constraint_id", pa.string(), False),
        ("kind", pa.string(), False),
        ("left_id", pa.string(), False),
        ("right_id", pa.string(), False),
        ("basis_decision_id", pa.string(), False),
        ("valid_from_seq", pa.int64(), False),
        ("valid_to_seq", pa.int64(), False),
    ],
    "model_invocations": [
        ("invocation_id", pa.string(), False),
        ("provider", pa.string(), False),
        ("model", pa.string(), False),
        ("model_build", pa.string(), False),
        ("prompt_ref", pa.string(), False),
        ("settings", pa.string(), True),
        ("cache_key", pa.string(), False),
        ("payload", pa.string(), True),
        ("response", pa.string(), True),
        ("output", pa.string(), True),
        ("status", pa.string(), False),
        ("error", pa.string(), False),
        ("attempts", pa.int32(), False),
        ("cached_from", pa.string(), False),
        ("input_tokens", pa.int64(), False),
        ("output_tokens", pa.int64(), False),
        ("latency_ms", pa.int64(), False),
        ("cost_usd", pa.float64(), False),
        ("run_id", pa.string(), False),
        ("stage_id", pa.string(), False),
        ("created_at", _TS, False),
    ],
    "evidence": [
        ("evidence_id", pa.string(), False),
        ("run_id", pa.string(), False),
        ("stage_id", pa.string(), False),
        ("left_id", pa.string(), False),
        ("right_id", pa.string(), False),
        ("kind", pa.string(), False),
        ("attribute", pa.string(), False),
        ("level", pa.string(), False),
        ("direction", pa.string(), False),
        ("strength", pa.string(), False),
        ("value", pa.string(), True),
        ("score", pa.string(), True),
        ("method_ref", pa.string(), False),
        ("inputs", pa.string(), True),
        ("created_at", _TS, False),
    ],
    "decisions": [
        ("decision_id", pa.string(), False),
        ("decision_type", pa.string(), False),
        ("run_id", pa.string(), False),
        ("left_id", pa.string(), False),
        ("right_id", pa.string(), False),
        ("verdict", pa.string(), False),
        ("outcome", pa.string(), False),
        ("target_entity_id", pa.string(), False),
        ("members", pa.string(), True),
        ("scores", pa.string(), True),
        ("evidence_ids", pa.string(), True),
        ("flags", pa.string(), True),
        ("component_refs", pa.string(), True),
        ("actor_kind", pa.string(), False),
        ("actor_id", pa.string(), False),
        ("reason", pa.string(), False),
        ("created_at", _TS, False),
        ("body", pa.string(), True),
    ],
    "proposal_members": [
        ("proposal_id", pa.string(), False),
        ("node_id", pa.string(), False),
        ("shown", pa.string(), False),
        ("group_no", pa.int32(), False),
        ("label_out", pa.string(), False),
        ("uncertain", pa.bool_(), False),
    ],
    "run_scope": [
        ("run_id", pa.string(), False),
        ("observation_id", pa.string(), False),
    ],
}

_FEATURE_COLUMNS = ["observation_id", "feature", "ordinal", "value", "value_num", "normalizer_ref"]
_CANDIDATE_COLUMNS = [
    "run_id",
    "stage_id",
    "left_id",
    "right_id",
    "generator_ref",
    "rank",
    "score_kind",
    "score_value",
]


def _arrow(result: duckdb.DuckDBPyConnection) -> pa.Table:
    """Fetch a result as an Arrow table across DuckDB versions (the method was renamed
    in 1.5). Reading through Arrow also yields timezone-aware datetimes without pytz."""
    fetch = getattr(result, "to_arrow_table", None) or result.fetch_arrow_table
    return fetch()


def _first(result: duckdb.DuckDBPyConnection) -> tuple | None:
    """The first row of a result, or None, read to the end. DuckDB 2.0 counts a result
    read with ``fetchone()`` as unfinished and refuses ``register`` until it is done."""
    rows = result.fetchall()
    return rows[0] if rows else None


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _loads(value: str | None) -> Any:
    return None if value is None else json.loads(value)


class DuckDBStore:
    """The M1 store. ``path=":memory:"`` is the in-memory mode."""

    def __init__(
        self,
        path: str = ":memory:",
        *,
        ids: IdFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._con = duckdb.connect(path)
        self._con.execute("SET TimeZone = 'UTC'")
        self._ids = ids or IdFactory()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self.migrate()

    def close(self) -> None:
        self._con.close()

    # -- plumbing -------------------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._con.execute("BEGIN TRANSACTION")
            try:
                yield
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
            self._con.execute("COMMIT")

    def _insert(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        spec = _COLUMNS[table]
        schema = pa.schema([(name, typ) for name, typ, _ in spec])
        data = pa.Table.from_pylist([{n: r.get(n) for n, _, _ in spec} for r in rows], schema)
        selects = ", ".join(f"CAST({n} AS JSON)" if is_json else n for n, _, is_json in spec)
        self._insert_arrow(table, data, [n for n, _, _ in spec], selects)

    def _insert_arrow(self, table: str, data: pa.Table, columns: list[str], selects: str) -> None:
        self._con.register("_thisness_rows", data)
        try:
            self._con.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) SELECT {selects} FROM _thisness_rows"
            )
        finally:
            self._con.unregister("_thisness_rows")

    def _head(self) -> int:
        return _first(self._con.execute("SELECT coalesce(max(seq), 0) FROM ledger_events"))[0]

    def _append_events(self, events: list[dict[str, Any]]) -> None:
        self._insert("ledger_events", events)

    def _bookkeeping_event(
        self,
        event_type: EventType,
        *,
        run_id: str | None = None,
        observation_ids: Sequence[str] = (),
        payload: dict | None = None,
    ) -> None:
        """Non-identity events written alongside facts (ingest, run start)."""
        head, commit_id, now = self._head(), self._ids.commit(), self._clock()
        targets = list(observation_ids) or [None]
        self._append_events(
            [
                {
                    "seq": head + i + 1,
                    "event_id": self._ids.event(),
                    "commit_id": commit_id,
                    "event_type": str(event_type),
                    "run_id": run_id,
                    "actor_kind": "system",
                    "actor_id": "store",
                    "observation_id": obs,
                    "payload": _dumps(payload or {}),
                    "recorded_at": now,
                }
                for i, obs in enumerate(targets)
            ]
        )

    # -- lifecycle -------------------------------------------------------------------

    def migrate(self) -> None:
        with self._lock:
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL)"
            )
            row = _first(self._con.execute("SELECT value FROM meta WHERE key = 'schema_version'"))
            current = int(row[0]) if row else 0
            migrations = resources.files("thisness.storage.duckdb") / "migrations"
            for path in sorted(migrations.iterdir(), key=lambda p: p.name):
                if not path.name.endswith(".sql"):
                    continue
                number = int(path.name.split("_", 1)[0])
                if number <= current:
                    continue
                sql = path.read_text().replace(
                    "CREATE TABLE meta (", "CREATE TABLE IF NOT EXISTS meta ("
                )
                with self._transaction():
                    self._con.execute(sql)
                    self._con.execute(
                        "INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", [str(number)]
                    )

    def schema_version(self) -> int:
        row = _first(self._con.execute("SELECT value FROM meta WHERE key = 'schema_version'"))
        return int(row[0]) if row else 0

    def head_seq(self) -> int:
        with self._lock:
            return self._head()

    # -- facts -------------------------------------------------------------------------

    def ingest(self, observations: Sequence[Observation]) -> IngestResult:
        with self._transaction():
            fingerprints = sorted({o.fingerprint for o in observations})
            stored: dict[str, str] = {}
            if fingerprints:
                self._con.register("_fps", pa.table({"fp": fingerprints}))
                try:
                    stored = dict(
                        self._con.execute(
                            "SELECT fingerprint, observation_id FROM observations "
                            "JOIN _fps ON fingerprint = fp"
                        ).fetchall()
                    )
                finally:
                    self._con.unregister("_fps")
            rows, inserted, existing = [], [], {}
            for obs in observations:
                if obs.fingerprint in stored:
                    existing[obs.observation_id] = stored[obs.fingerprint]
                    continue
                stored[obs.fingerprint] = obs.observation_id
                inserted.append(obs.observation_id)
                rows.append(
                    {
                        "observation_id": obs.observation_id,
                        "entity_type": obs.entity_type,
                        "fingerprint": obs.fingerprint,
                        "source_type": obs.source.type,
                        "source_id": obs.source.id,
                        "source_locator": _dumps(obs.source.locator),
                        "source_version": obs.source.version,
                        "attributes": None if obs.attributes is None else _dumps(obs.attributes),
                        "context": _dumps(obs.context),
                        "observed_at": obs.observed_at,
                        "ingested_at": obs.ingested_at,
                        "supersedes": obs.supersedes,
                        "redacted_at": obs.redacted_at,
                    }
                )
            self._insert("observations", rows)
            if inserted:
                self._bookkeeping_event(EventType.OBSERVATION_INGESTED, observation_ids=inserted)
        return IngestResult(inserted=tuple(inserted), existing=existing)

    def write_features(self, batch: pa.Table) -> None:
        with self._transaction():
            self._insert_arrow(
                "observation_features",
                batch.select(_FEATURE_COLUMNS),
                _FEATURE_COLUMNS,
                ", ".join(_FEATURE_COLUMNS),
            )

    def feature_normalizers(self, entity_type: str) -> set[str]:
        """The normalizer versions that wrote the stored features of a type."""
        rows = self._con.execute(
            "SELECT DISTINCT f.normalizer_ref FROM observation_features f "
            "JOIN observations o USING (observation_id) WHERE o.entity_type = ?",
            [entity_type],
        ).fetchall()
        return {r[0] for r in rows}

    def replace_features(self, entity_type: str, batch: pa.Table) -> None:
        """Every stored feature of a type, replaced by ``batch`` in one transaction."""
        with self._transaction():
            self._con.execute(
                "DELETE FROM observation_features WHERE observation_id IN "
                "(SELECT observation_id FROM observations WHERE entity_type = ?)",
                [entity_type],
            )
            self._insert_arrow("observation_features", batch.select(_FEATURE_COLUMNS),
                               _FEATURE_COLUMNS, ", ".join(_FEATURE_COLUMNS))  # fmt: skip

    def drop_features(self, normalizer_ref: str) -> None:
        with self._transaction():
            self._con.execute(
                "DELETE FROM observation_features WHERE normalizer_ref = ?", [normalizer_ref]
            )

    def register_component(self, ref: ComponentRef, config: Mapping[str, Any]) -> None:
        with self._transaction():
            self._register(ref, config)

    def _register(self, ref: ComponentRef, config: Mapping[str, Any] | None) -> None:
        self._con.execute(
            "INSERT OR IGNORE INTO components VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ref.ref,
                ref.kind,
                ref.name,
                ref.version,
                ref.config_hash,
                None if config is None else _dumps(dict(config)),
                self._clock(),
            ],
        )

    def record_run(self, run: ResolutionRun, scope: Sequence[str]) -> None:
        with self._transaction():
            self._con.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    run.run_id,
                    run.entity_type,
                    run.profile_ref,
                    run.base_seq,
                    str(run.status),
                    _dumps(run.plan),
                    run.created_at,
                    run.committed_seq,
                ],
            )
            self._insert("run_scope", [{"run_id": run.run_id, "observation_id": o} for o in scope])
            self._bookkeeping_event(EventType.RUN_STARTED, run_id=run.run_id)

    def record_stage(self, stage: StageExecution) -> None:
        with self._transaction():
            self._con.execute(
                "INSERT INTO stages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    stage.stage_id,
                    stage.run_id,
                    stage.kind,
                    stage.producer_ref,
                    stage.family,
                    stage.view,
                    _dumps(list(stage.conditioned_on)),
                    stage.started_at,
                    stage.finished_at,
                ],
            )

    def record_candidates(self, batch: pa.Table) -> None:
        with self._transaction():
            self._insert_arrow(
                "candidates",
                batch.select(_CANDIDATE_COLUMNS),
                _CANDIDATE_COLUMNS,
                ", ".join(_CANDIDATE_COLUMNS),
            )

    def record_evidence(self, items: Sequence[EvidenceItem]) -> None:
        rows = [
            {
                "evidence_id": e.evidence_id,
                "run_id": e.run_id,
                "stage_id": e.stage_id,
                "left_id": e.left_id,
                "right_id": e.right_id,
                "kind": e.kind,
                "attribute": e.attribute,
                "level": e.level,
                "direction": e.direction,
                "strength": e.strength,
                "value": _dumps(e.value),
                "score": None if e.score is None else e.score.model_dump_json(),
                "method_ref": e.method_ref,
                "inputs": _dumps(e.inputs),
                "created_at": e.created_at,
            }
            for e in items
        ]
        with self._transaction():
            self._insert("evidence", rows)

    def record_proposals(self, proposals: Sequence[GroupProposal]) -> None:
        with self._transaction():
            for p in proposals:
                self._con.execute(
                    "INSERT INTO proposals VALUES (?, ?, ?, ?, ?)",
                    [p.proposal_id, p.run_id, p.stage_id, p.batch_id, p.invocation_id],
                )
            self._insert(
                "proposal_members",
                [
                    {"proposal_id": p.proposal_id, **m.model_dump()}
                    for p in proposals
                    for m in p.members
                ],
            )

    def record_invocation(self, invocation: ModelInvocation) -> None:
        row = invocation.model_dump(mode="python")
        for key in ("settings", "payload", "response", "output"):
            row[key] = _dumps(row[key])
        with self._transaction():
            self._insert("model_invocations", [row])

    def record_decisions(self, decisions: Sequence[Decision]) -> None:
        rows = [self._decision_row(d) for d in decisions]
        with self._transaction():
            self._insert("decisions", rows)

    @staticmethod
    def _decision_row(d: Decision) -> dict[str, Any]:
        row: dict[str, Any] = {
            "decision_id": d.decision_id,
            "decision_type": d.decision_type,
            "evidence_ids": _dumps(list(d.evidence_ids)),
            "component_refs": _dumps(list(d.component_refs)),
            "actor_kind": d.actor.kind,
            "actor_id": d.actor.id,
            "created_at": d.created_at,
            "body": d.model_dump_json(),
        }
        if isinstance(d, PairJudgment):
            row |= {
                "run_id": d.run_id,
                "left_id": d.left_id,
                "right_id": d.right_id,
                "verdict": str(d.verdict),
                "scores": _dumps([s.model_dump(mode="json") for s in d.scores]),
            }
        elif isinstance(d, ConsensusDecision):
            flags = dict(d.flags)
            if d.suggested_cut is not None:
                flags["suggested_cut"] = d.suggested_cut.model_dump(mode="json")
            row |= {
                "run_id": d.run_id,
                "outcome": str(d.outcome),
                "target_entity_id": d.target_entity_id,
                "members": _dumps(list(d.members)),
                "flags": _dumps(flags),
            }
        elif isinstance(d, OperatorDecision):
            row |= {
                "outcome": str(d.action),
                "members": _dumps(list(d.subject_ids)),
                "reason": d.reason,
            }
        elif isinstance(d, ReviewDecision):
            row |= {"verdict": str(d.verdict), "reason": d.reason}
        elif isinstance(d, ReviewRecommendation):
            row |= {"outcome": d.resolution, "reason": d.rationale}
        elif isinstance(d, LabelSuggestion):
            row |= {"target_entity_id": d.entity_id, "outcome": "label_suggestion",
                    "reason": d.rationale}  # fmt: skip
        return row

    # -- identity state ------------------------------------------------------------------

    def commit(
        self, changes: ChangeSet, *, actor: Actor, canonicalizer: Canonicalizer | None = None
    ) -> CommitResult:
        canonicalizer = canonicalizer or MostFrequentValue()
        with self._transaction():
            head = self._head()
            if changes.base_seq > head:
                raise InvalidOperation(-1, f"base_seq {changes.base_seq} is ahead of head {head}")
            self._check_decisions(sorted(changes.decision_ids))
            self._check_conflicts(changes)
            plan = CommitPlan(
                _Reader(self._con),
                changes,
                actor=actor,
                ids=self._ids,
                now=self._clock(),
                head_seq=head,
            )
            plan.apply()
            self._register(canonicalizer.ref, None)
            self._write_plan(plan, canonicalizer)
        return CommitResult(
            commit_id=plan.commit_id,
            first_seq=plan.first_seq,
            last_seq=plan.last_seq,
            touched_entity_ids=tuple(plan.touched),
        )

    def _check_decisions(self, decision_ids: list[str]) -> None:
        self._con.register("_dids", pa.table({"id": decision_ids}))
        try:
            missing = [
                r[0]
                for r in self._con.execute(
                    "SELECT id FROM _dids LEFT JOIN decisions ON decision_id = id "
                    "WHERE decision_id IS NULL ORDER BY id"
                ).fetchall()
            ]
        finally:
            self._con.unregister("_dids")
        if missing:
            raise UnknownDecision(missing)

    def _check_conflicts(self, changes: ChangeSet) -> None:
        read = changes.read_set
        if not read.entity_ids and not read.blocking_keys:
            return
        identity_events = [
            str(e)
            for e in (
                EventType.ENTITY_CREATED,
                EventType.ENTITY_PROMOTED,
                EventType.ENTITY_MERGED,
                EventType.ENTITY_SPLIT,
                EventType.ENTITY_REACTIVATED,
                EventType.MEMBERSHIP_ACCEPTED,
                EventType.MEMBERSHIP_ENDED,
            )
        ]
        entities: list[str] = []
        if read.entity_ids:
            self._con.register("_read", pa.table({"id": sorted(read.entity_ids)}))
            try:
                entities = [
                    r[0]
                    for r in self._con.execute(
                        "SELECT DISTINCT id FROM _read JOIN ledger_events "
                        "ON id IN (entity_id, related_entity_id) "
                        "WHERE seq > ? AND list_contains(?, event_type) ORDER BY id",
                        [changes.base_seq, identity_events],
                    ).fetchall()
                ]
            finally:
                self._con.unregister("_read")
        keys: list[str] = []
        if read.blocking_keys:
            parsed = [k.split("=", 1) for k in sorted(read.blocking_keys)]
            self._con.register(
                "_keys",
                pa.table({"feature": [p[0] for p in parsed], "value": [p[-1] for p in parsed]}),
            )
            try:
                keys = [
                    r[0]
                    for r in self._con.execute(
                        "SELECT DISTINCT k.feature || '=' || k.value FROM ledger_events e "
                        "JOIN observation_features f ON f.observation_id = e.observation_id "
                        "JOIN _keys k ON k.feature = f.feature AND k.value = f.value "
                        "WHERE e.seq > ? AND e.event_type = 'MEMBERSHIP_ACCEPTED' ORDER BY 1",
                        [changes.base_seq],
                    ).fetchall()
                ]
            finally:
                self._con.unregister("_keys")
        if entities or keys:
            raise CommitConflict(changes.base_seq, entities, keys)

    def label_candidates(self, entity_type: str) -> list[tuple[str, str | None]]:
        """Live entities of a type with their current name, that have no accepted display
        name and no suggestion yet (design 08 §5.2), largest first."""
        rows = self._con.execute(
            """
            WITH live AS (
                SELECT e.entity_id, (s.attributes->>'name') AS name FROM entities e
                JOIN entity_snapshots s ON s.entity_id = e.entity_id AND s.version = e.version
                WHERE e.entity_type = ? AND e.state <> 'retired'),
            labeled AS (
                SELECT entity_id FROM ledger_events
                WHERE event_type IN ('ENTITY_LABELED', 'ENTITY_LABEL_CLEARED')
                QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY seq DESC) = 1
                  AND event_type = 'ENTITY_LABELED'),
            suggested AS (
                SELECT DISTINCT target_entity_id AS entity_id FROM decisions
                WHERE decision_type = 'label_suggestion'),
            size AS (
                SELECT entity_id, count(*) AS n FROM memberships WHERE valid_to_seq IS NULL
                GROUP BY entity_id)
            SELECT live.entity_id, live.name FROM live LEFT JOIN size USING (entity_id)
            WHERE live.entity_id NOT IN (SELECT entity_id FROM labeled)
              AND live.entity_id NOT IN (SELECT entity_id FROM suggested)
            ORDER BY coalesce(size.n, 0) DESC, live.entity_id
            """,
            [entity_type],
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def label_suggestions(self, entity_ids: Sequence[str]) -> dict[str, Decision]:
        """The latest display-name suggestion for each entity that has one."""
        rows = self._by_ids(
            "SELECT target_entity_id AS entity_id, body FROM decisions JOIN _qids ON "
            "target_entity_id = id WHERE decision_type = 'label_suggestion' "
            "QUALIFY row_number() OVER (PARTITION BY target_entity_id ORDER BY created_at DESC, "
            "decision_id DESC) = 1",
            entity_ids,
        )
        return {r["entity_id"]: _DECISION.validate_json(r["body"]) for r in rows}

    def labels(self, entity_ids: Sequence[str]) -> dict[str, str]:
        return self._labels(entity_ids)

    def _labels(self, entity_ids: Sequence[str]) -> dict[str, str]:
        """Each entity's accepted display name, from the ledger (design 08 §5.2)."""
        if not entity_ids:
            return {}
        rows = self._by_ids(
            "SELECT entity_id, event_type, payload->>'name' AS name FROM ledger_events "
            "JOIN _qids ON entity_id = id "
            "WHERE event_type IN ('ENTITY_LABELED', 'ENTITY_LABEL_CLEARED') "
            "QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY seq DESC) = 1",
            entity_ids,
        )
        return {r["entity_id"]: r["name"] for r in rows if r["event_type"] == "ENTITY_LABELED"}

    def _write_plan(self, plan: CommitPlan, canonicalizer: Canonicalizer) -> None:
        self._append_events(plan.events)
        new = [e for e in plan.entities.values() if e.is_new]
        changed = [e for e in plan.entities.values() if e.dirty and not e.is_new]
        self._insert("entities", [vars(e) for e in new])
        if changed:
            self._update(
                "entities",
                "entity_id",
                [vars(e) for e in changed],
                ["state", "version", "retired_seq", "merged_into"],
            )
        self._insert("memberships", [vars(m) for m in plan.new_memberships])
        if plan.ended_memberships:
            self._update(
                "memberships",
                "membership_id",
                [
                    {"membership_id": k, "valid_to_seq": v}
                    for k, v in plan.ended_memberships.items()
                ],
                ["valid_to_seq"],
            )
        snapshots = []
        needed = sorted({o for _, _, members in plan.snapshot_targets for o in members})
        labels = self._labels([e for e, _, _ in plan.snapshot_targets])
        by_id = {o.observation_id: o for o in self.observations(needed)}
        registered: set[str] = set()  # once per commit, not once per snapshot (design 10 §3.1)
        for entity_id, version, members in plan.snapshot_targets:
            observations = [by_id[o] for o in members]
            # A dispatching canonicalizer names the one that actually built the snapshot.
            chosen = canonicalizer.pick(observations) if hasattr(canonicalizer, "pick") \
                else canonicalizer  # fmt: skip
            if chosen is not canonicalizer and chosen.ref.ref not in registered:
                registered.add(chosen.ref.ref)
                self._register(chosen.ref, None)
            attributes = chosen.build(observations)
            if entity_id in labels:  # an accepted name outranks any canonicalizer's
                attributes = {**attributes, "name": labels[entity_id]}
            snapshots.append(
                {
                    "entity_id": entity_id,
                    "version": version,
                    "attributes": _dumps(attributes),
                    "derived_from": _dumps(members),
                    "canonicalizer_ref": chosen.ref.ref,
                    "created_seq": plan.last_seq,
                }
            )
        self._insert("entity_snapshots", snapshots)
        self._insert("relationships", plan.new_relationships)
        if plan.revoked_relationships:
            self._update(
                "relationships",
                "relationship_id",
                [
                    {"relationship_id": k, "valid_to_seq": v}
                    for k, v in plan.revoked_relationships.items()
                ],
                ["valid_to_seq"],
            )
        self._insert("pair_constraints", plan.new_constraints)
        if plan.revoked_constraints:
            self._update(
                "pair_constraints",
                "constraint_id",
                [
                    {"constraint_id": k, "valid_to_seq": v}
                    for k, v in plan.revoked_constraints.items()
                ],
                ["valid_to_seq"],
            )
        self._insert("review_items", plan.new_reviews)
        if plan.closed_reviews:
            self._update(
                "review_items",
                "review_id",
                [{"review_id": k, **v} for k, v in plan.closed_reviews.items()],
                ["status", "closed_seq", "resolution_decision_id"],
            )
        if plan.run_id is not None:
            self._con.execute(
                "UPDATE runs SET status = 'committed', committed_seq = ? WHERE run_id = ?",
                [plan.last_seq, plan.run_id],
            )

    def _update(self, table: str, key: str, rows: list[dict[str, Any]], columns: list[str]) -> None:
        spec = {n: t for n, t, _ in _COLUMNS[table]}
        names = [key, *columns]
        data = pa.Table.from_pylist(
            [{n: r.get(n) for n in names} for r in rows],
            pa.schema([(n, spec[n]) for n in names]),
        )
        assignments = ", ".join(f"{c} = u.{c}" for c in columns)
        self._con.register("_thisness_updates", data)
        try:
            self._con.execute(
                f"UPDATE {table} SET {assignments} FROM _thisness_updates u "
                f"WHERE {table}.{key} = u.{key}"
            )
        finally:
            self._con.unregister("_thisness_updates")

    # -- reads -----------------------------------------------------------------------------

    def observations(self, ids: Sequence[str]) -> list[Observation]:
        if not ids:
            return []
        with self._lock:
            self._con.register("_oids", pa.table({"id": list(ids)}))
            try:
                rows = _arrow(
                    self._con.execute(
                        "SELECT o.* FROM observations o JOIN _oids ON observation_id = id "
                        "ORDER BY observation_id"
                    )
                ).to_pylist()
            finally:
                self._con.unregister("_oids")
        out = []
        for r in rows:
            out.append(
                Observation(
                    observation_id=r["observation_id"],
                    entity_type=r["entity_type"],
                    attributes=_loads(r["attributes"]),
                    source=SourceRef(
                        type=r["source_type"],
                        id=r["source_id"],
                        locator=_loads(r["source_locator"]) or {},
                        version=r["source_version"],
                    ),
                    context=_loads(r["context"]) or {},
                    observed_at=r["observed_at"],
                    ingested_at=r["ingested_at"],
                    fingerprint=r["fingerprint"],
                    supersedes=r["supersedes"],
                    redacted_at=r["redacted_at"],
                )
            )
        return out

    def _all(self, sql: str, params: list[Any] | dict[str, Any] | None = None) -> list[dict]:
        with self._lock:
            return _arrow(self._con.execute(sql, params or [])).to_pylist()

    def _one(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    def observation_ids(self, entity_type: str) -> list[str]:
        """Every unredacted observation of a type, in id (ingest) order."""
        rows = self._all(
            "SELECT observation_id FROM observations WHERE entity_type = ? "
            "AND redacted_at IS NULL ORDER BY observation_id",
            [entity_type],
        )
        return [r["observation_id"] for r in rows]

    def features(self, observation_ids: Sequence[str]) -> dict[str, dict[str, list[str]]]:
        """observation -> feature -> values in ordinal order."""
        out: dict[str, dict[str, list[str]]] = {o: {} for o in observation_ids}
        if not observation_ids:
            return out
        with self._lock:
            self._con.register("_fids", pa.table({"id": list(observation_ids)}))
            try:
                table = _arrow(
                    self._con.execute(
                        "SELECT f.observation_id, f.feature, f.value FROM observation_features f "
                        "JOIN _fids ON f.observation_id = id WHERE f.value IS NOT NULL "
                        "ORDER BY f.observation_id, f.feature, f.ordinal"
                    )
                )
            finally:
                self._con.unregister("_fids")
        for obs, feature, value in zip(
            table.column("observation_id").to_pylist(),
            table.column("feature").to_pylist(),
            table.column("value").to_pylist(),
            strict=True,
        ):
            out[obs].setdefault(feature, []).append(value)
        return out

    def _by_ids(self, sql: str, ids: Sequence[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        with self._lock:
            self._con.register("_qids", pa.table({"id": list(ids)}))
            try:
                return _arrow(self._con.execute(sql)).to_pylist()
            finally:
                self._con.unregister("_qids")

    def entities_of(self, observation_ids: Sequence[str]) -> dict[str, str]:
        """Current entity of each observation that has one."""
        rows = self._by_ids(
            "SELECT observation_id, entity_id FROM memberships "
            "JOIN _qids ON observation_id = id WHERE valid_to_seq IS NULL",
            observation_ids,
        )
        return {r["observation_id"]: r["entity_id"] for r in rows}

    def snapshot_names(self, entity_ids: Sequence[str]) -> dict[str, str]:
        """The ``name`` in each entity's latest snapshot, in one query (design 07 §13.4:
        one query per entity took 24 s for a page of 16,225)."""
        rows = self._by_ids(
            "SELECT entity_id, attributes FROM entity_snapshots JOIN _qids ON entity_id = id "
            "QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY version DESC) = 1",
            entity_ids,
        )
        out = {}
        for r in rows:
            name = _loads(r["attributes"]).get("name")
            if isinstance(name, str) and name:
                out[r["entity_id"]] = name
        return out

    def mapping(self, entity_type: str, as_of_seq: int | None = None) -> pa.Table:
        """Every observation of a type with its entity as of a ledger point (design 08 §6):
        source, name, entity, the entity's name then, and a status. ``in_review`` when the
        observation or its entity is a member of a review open at that point; then
        ``unresolved`` with no entity, ``provisional`` when the entity was created
        provisional and not yet promoted, else ``resolved``."""
        query = """
            WITH head AS (SELECT coalesce(?, (SELECT max(seq) FROM ledger_events), 0) AS s),
            obs AS (
                SELECT o.observation_id, o.source_type, o.source_id,
                       CAST(o.source_locator AS VARCHAR) AS locator, (o.attributes->>'name') AS name
                FROM observations o JOIN ledger_events i
                  ON i.observation_id = o.observation_id AND i.event_type = 'OBSERVATION_INGESTED'
                WHERE o.entity_type = ? AND i.seq <= (SELECT s FROM head)),
            mem AS (
                SELECT observation_id, entity_id, basis_decision_id, valid_from_seq FROM memberships
                WHERE valid_from_seq <= (SELECT s FROM head)
                  AND (valid_to_seq IS NULL OR valid_to_seq > (SELECT s FROM head))),
            snap AS (
                SELECT entity_id, (attributes->>'name') AS entity_name FROM entity_snapshots
                WHERE created_seq <= (SELECT s FROM head)
                QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY version DESC) = 1),
            provisional AS (
                SELECT c.entity_id FROM ledger_events c
                WHERE c.event_type = 'ENTITY_CREATED' AND (c.payload->>'state') = 'provisional'
                  AND c.seq <= (SELECT s FROM head)
                  AND NOT EXISTS (SELECT 1 FROM ledger_events p
                                  WHERE p.event_type = 'ENTITY_PROMOTED'
                                    AND p.entity_id = c.entity_id AND p.seq <= (SELECT s FROM head))),
            open_nodes AS (
                SELECT DISTINCT unnest(from_json(members, '["VARCHAR"]')) AS node FROM review_items
                WHERE opened_seq <= (SELECT s FROM head)
                  AND (closed_seq IS NULL OR closed_seq > (SELECT s FROM head)))
            SELECT obs.source_type, obs.source_id, obs.locator, obs.name, obs.observation_id,
                   mem.entity_id, coalesce(snap.entity_name, mem.entity_id) AS entity_name,
                   CASE WHEN obs.observation_id IN (SELECT node FROM open_nodes)
                          OR mem.entity_id IN (SELECT node FROM open_nodes) THEN 'in_review'
                        WHEN mem.entity_id IS NULL THEN 'unresolved'
                        WHEN provisional.entity_id IS NOT NULL THEN 'provisional'
                        ELSE 'resolved' END AS status,
                   status.record_status,
                   mem.valid_from_seq AS since_seq, mem.basis_decision_id AS decision_id
            FROM obs LEFT JOIN mem USING (observation_id)
            LEFT JOIN (SELECT observation_id, max(value) AS record_status FROM observation_features
                       WHERE feature = 'record_status' GROUP BY observation_id) status
              USING (observation_id)
            LEFT JOIN snap ON snap.entity_id = mem.entity_id
            LEFT JOIN provisional ON provisional.entity_id = mem.entity_id
            ORDER BY obs.observation_id
        """
        return _arrow(self._con.execute(query, [as_of_seq, entity_type]))

    def members_of(self, entity_ids: Sequence[str]) -> dict[str, list[str]]:
        """Current members of each entity, in id (ingest) order."""
        rows = self._by_ids(
            "SELECT entity_id, observation_id FROM memberships "
            "JOIN _qids ON entity_id = id WHERE valid_to_seq IS NULL "
            "ORDER BY entity_id, observation_id",
            entity_ids,
        )
        out: dict[str, list[str]] = {e: [] for e in entity_ids}
        for r in rows:
            out[r["entity_id"]].append(r["observation_id"])
        return out

    def entity_changed_at(self) -> dict[str, datetime]:
        """When each entity was last touched by a ledger event (design 10 §4.2)."""
        rows = self._all(
            "SELECT e AS entity_id, max(recorded_at) AS at FROM ("
            "SELECT entity_id AS e, recorded_at FROM ledger_events WHERE entity_id IS NOT NULL "
            "UNION ALL SELECT related_entity_id, recorded_at FROM ledger_events "
            "WHERE related_entity_id IS NOT NULL) GROUP BY e"
        )
        return {r["entity_id"]: r["at"] for r in rows}

    def entity_states(self, entity_ids: Sequence[str]) -> dict[str, str]:
        rows = self._by_ids(
            "SELECT entity_id, state FROM entities JOIN _qids ON entity_id = id", entity_ids
        )
        return {r["entity_id"]: r["state"] for r in rows}

    def unresolved(self, entity_type: str) -> list[str]:
        """Unredacted observations of a type with no current membership."""
        rows = self._all(
            "SELECT o.observation_id FROM observations o "
            "LEFT JOIN memberships m ON m.observation_id = o.observation_id "
            "AND m.valid_to_seq IS NULL "
            "WHERE o.entity_type = ? AND o.redacted_at IS NULL AND m.observation_id IS NULL "
            "ORDER BY o.observation_id",
            [entity_type],
        )
        return [r["observation_id"] for r in rows]

    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", [key])
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        with self._transaction():
            self._con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", [key, value])

    def close_run(self, run_id: str, status: str) -> None:
        """Mark a run that will not commit: ``proposed`` (left for review) or
        ``abandoned``. Committed runs are closed by ``commit``."""
        if status not in ("proposed", "abandoned"):
            raise ValueError("close_run sets proposed or abandoned")
        event = EventType.RUN_PROPOSED if status == "proposed" else EventType.RUN_ABANDONED
        with self._transaction():
            current = _first(
                self._con.execute("SELECT status FROM runs WHERE run_id = ?", [run_id])
            )
            if current is None:
                raise NotFound(f"run {run_id}")
            if current[0] in ("committed", "abandoned"):
                raise InvalidOperation(-1, f"run {run_id} is already {current[0]}")
            self._con.execute("UPDATE runs SET status = ? WHERE run_id = ?", [status, run_id])
            self._bookkeeping_event(event, run_id=run_id)

    def component_config(self, ref: str) -> dict[str, Any] | None:
        row = self._one("SELECT config FROM components WHERE component_ref = ?", [ref])
        return None if row is None else _loads(row["config"])

    def entity(self, entity_id: str) -> Entity:
        row = self._one("SELECT * FROM entities WHERE entity_id = ?", [entity_id])
        if row is None:
            raise NotFound(f"entity {entity_id}")
        return Entity.model_validate(row)

    def resolve_id(self, entity_id: str) -> list[str]:
        """A live entity resolves to itself. A retired one resolves to wherever its
        final members live now: one entity, or several after a partial split."""
        entity = self.entity(entity_id)
        if entity.state != "retired":
            return [entity_id]
        rows = self._all(
            """
            WITH last_merge AS (
                SELECT payload FROM ledger_events
                WHERE event_type = 'ENTITY_MERGED' AND entity_id = $id
                ORDER BY seq DESC LIMIT 1),
            final_members AS (
                SELECT unnest(from_json(payload->'members_at_retirement', '["VARCHAR"]'))
                    AS observation_id
                FROM last_merge)
            SELECT DISTINCT m.entity_id FROM final_members f
            JOIN memberships m ON m.observation_id = f.observation_id AND m.valid_to_seq IS NULL
            ORDER BY m.entity_id
            """,
            {"id": entity_id},
        )
        return [r["entity_id"] for r in rows]

    def snapshot(self, entity_id: str, version: int | None = None) -> EntitySnapshot:
        if version is None:
            row = self._one(
                "SELECT * FROM entity_snapshots WHERE entity_id = ? ORDER BY version DESC LIMIT 1",
                [entity_id],
            )
        else:
            row = self._one(
                "SELECT * FROM entity_snapshots WHERE entity_id = ? AND version = ?",
                [entity_id, version],
            )
        if row is None:
            raise NotFound(f"snapshot of {entity_id} at version {version}")
        row["attributes"] = _loads(row["attributes"])
        row["derived_from"] = tuple(_loads(row["derived_from"]))
        return EntitySnapshot.model_validate(row)

    def members(self, entity_id: str, as_of_seq: int | None = None) -> list[str]:
        if as_of_seq is None:
            sql = (
                "SELECT observation_id FROM memberships WHERE entity_id = ? "
                "AND valid_to_seq IS NULL ORDER BY observation_id"
            )
            params: list[Any] = [entity_id]
        else:
            sql = (
                "SELECT observation_id FROM memberships WHERE entity_id = ? "
                "AND valid_from_seq <= ? AND (valid_to_seq IS NULL OR valid_to_seq > ?) "
                "ORDER BY observation_id"
            )
            params = [entity_id, as_of_seq, as_of_seq]
        return [r["observation_id"] for r in self._all(sql, params)]

    def entity_of(self, observation_id: str, as_of_seq: int | None = None) -> str | None:
        if as_of_seq is None:
            row = self._one(
                "SELECT entity_id FROM memberships WHERE observation_id = ? "
                "AND valid_to_seq IS NULL",
                [observation_id],
            )
        else:
            row = self._one(
                "SELECT entity_id FROM memberships WHERE observation_id = ? "
                "AND valid_from_seq <= ? AND (valid_to_seq IS NULL OR valid_to_seq > ?)",
                [observation_id, as_of_seq, as_of_seq],
            )
        return None if row is None else row["entity_id"]

    def relationships(self, entity_id: str) -> list[RelationshipAssertion]:
        rows = self._all(
            "SELECT * FROM relationships WHERE valid_to_seq IS NULL "
            "AND ? IN (subject_entity_id, object_entity_id) ORDER BY relationship_id",
            [entity_id],
        )
        for r in rows:
            r["evidence_ids"] = tuple(_loads(r["evidence_ids"]) or ())
        return [RelationshipAssertion.model_validate(r) for r in rows]

    # -- listings for the Workbench (design 04 §4) ----------------------------------------------

    _ENTITY_ROWS = """
        WITH live AS (
            SELECT entity_id, state, version, created_seq FROM entities
            WHERE entity_type = $type AND state != 'retired' {state}),
        named AS (
            SELECT s.entity_id, arg_max(s.attributes, s.version)->>'name' AS name
            FROM entity_snapshots s JOIN live USING (entity_id) GROUP BY s.entity_id),
        counted AS (
            SELECT m.entity_id, count(*) AS observations,
                   count(DISTINCT o.source_type) AS sources
            FROM memberships m JOIN live USING (entity_id)
            JOIN observations o ON o.observation_id = m.observation_id
            WHERE m.valid_to_seq IS NULL GROUP BY m.entity_id)
        SELECT l.entity_id, l.state, l.version, l.created_seq, n.name,
               coalesce(c.observations, 0) AS observations, coalesce(c.sources, 0) AS sources
        FROM live l LEFT JOIN named n USING (entity_id) LEFT JOIN counted c USING (entity_id)
        WHERE $query IS NULL OR l.entity_id = $query
           OR strip_accents(lower(n.name)) LIKE '%' || strip_accents(lower($query)) || '%'
    """

    def entity_rows(
        self,
        entity_type: str,
        *,
        state: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """Live entities of a type with their snapshot name and member counts, largest
        first, and the total matching."""
        sql = self._ENTITY_ROWS.format(state="AND state = $state" if state else "")
        params: dict[str, Any] = {"type": entity_type, "query": query or None}
        if state:
            params["state"] = state
        total = self._all(f"SELECT count(*) AS n FROM ({sql})", params)[0]["n"]
        rows = self._all(
            f"{sql} ORDER BY observations DESC, l.entity_id LIMIT {int(limit)} OFFSET {int(offset)}",
            params,
        )
        return rows, total

    def entity_counts(self) -> dict[str, dict[str, int]]:
        """Entities per type and state."""
        out: dict[str, dict[str, int]] = {}
        for r in self._all(
            "SELECT entity_type, state, count(*) AS n FROM entities GROUP BY ALL ORDER BY ALL"
        ):
            out.setdefault(r["entity_type"], {})[r["state"]] = r["n"]
        return out

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recent runs, newest first, with their scope size and ledger events."""
        rows = self._all(
            "SELECT r.*, (SELECT count(*) FROM run_scope s WHERE s.run_id = r.run_id) AS scope, "
            "(SELECT count(*) FROM ledger_events e WHERE e.run_id = r.run_id) AS events "
            f"FROM runs r ORDER BY r.run_id DESC LIMIT {int(limit)}"
        )
        for r in rows:
            r["plan"] = _loads(r["plan"])
        return rows

    def observation_count(self) -> dict[str, int]:
        return {
            r["entity_type"]: r["n"]
            for r in self._all(
                "SELECT entity_type, count(*) AS n FROM observations GROUP BY ALL ORDER BY ALL"
            )
        }

    # -- model invocations (design 05 §3.2) -------------------------------------------------

    def _invocations(self, where: str, params: list[Any], tail: str = "") -> list[ModelInvocation]:
        rows = self._all(f"SELECT * FROM model_invocations WHERE {where} {tail}", params)
        for r in rows:
            for key in ("settings", "payload", "response", "output"):
                r[key] = _loads(r[key])
            r["settings"] = r["settings"] or {}
        return [ModelInvocation.model_validate(r) for r in rows]

    def invocation(self, invocation_id: str) -> ModelInvocation:
        found = self._invocations("invocation_id = ?", [invocation_id])
        if not found:
            raise NotFound(f"invocation {invocation_id}")
        return found[0]

    def cached_invocation(self, cache_key: str) -> ModelInvocation | None:
        """The earliest successful call with this cache key, if any."""
        found = self._invocations("cache_key = ? AND status = 'ok'", [cache_key],
                                  "ORDER BY invocation_id LIMIT 1")  # fmt: skip
        return found[0] if found else None

    def invocations(self, *, run_id: str | None = None, limit: int = 500) -> list[ModelInvocation]:
        """Recent invocations, newest first, optionally for one run."""
        where, params = ("run_id = ?", [run_id]) if run_id else ("TRUE", [])
        return self._invocations(where, params, f"ORDER BY invocation_id DESC LIMIT {int(limit)}")

    def merged_from(self, entity_id: str) -> list[Entity]:
        """Entities retired by merging directly into ``entity_id``."""
        rows = self._all(
            "SELECT * FROM entities WHERE merged_into = ? ORDER BY retired_seq, entity_id",
            [entity_id],
        )
        return [Entity.model_validate(r) for r in rows]

    def relationships_among(self, entity_ids: Sequence[str]) -> list[RelationshipAssertion]:
        """Open relationships with both ends in ``entity_ids``."""
        rows = self._by_ids(
            "SELECT * FROM relationships WHERE valid_to_seq IS NULL "
            "AND subject_entity_id IN (SELECT id FROM _qids) "
            "AND object_entity_id IN (SELECT id FROM _qids) ORDER BY relationship_id",
            entity_ids,
        )
        for r in rows:
            r["evidence_ids"] = tuple(_loads(r["evidence_ids"]) or ())
        return [RelationshipAssertion.model_validate(r) for r in rows]

    def relationship_signals(self, levels: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
        """Evidence at the given (comparison, level) pairs: ``left_id, right_id, attribute,
        level, evidence_id``, ordered by pair."""
        if not levels:
            return []
        return self._all(
            "SELECT left_id, right_id, attribute, level, evidence_id FROM evidence "
            "WHERE list_contains(?, attribute || '/' || level) "
            "ORDER BY left_id, right_id, evidence_id",
            [[f"{a}/{lv}" for a, lv in levels]],
        )

    def recommendations(self, review_id: str) -> list[Decision]:
        """A review's model recommendations, oldest first."""
        rows = self._all(
            "SELECT body FROM decisions WHERE decision_type = 'recommendation' "
            "AND json_extract_string(body, '$.review_id') = ? ORDER BY decision_id",
            [review_id],
        )
        return [_DECISION.validate_json(r["body"]) for r in rows]

    def decisions_of_type(self, decision_type: str) -> list[Decision]:
        rows = self._all(
            "SELECT body FROM decisions WHERE decision_type = ? ORDER BY decision_id",
            [decision_type],
        )
        return [_DECISION.validate_json(r["body"]) for r in rows]

    def decisions(self, ids: Sequence[str]) -> list[Decision]:
        if not ids:
            return []
        rows = self._all(
            "SELECT body FROM decisions WHERE list_contains(?, decision_id) ORDER BY decision_id",
            [list(ids)],
        )
        return [_DECISION.validate_json(r["body"]) for r in rows]

    def pair_judgments(
        self, observation_ids: Sequence[str], *, run_id: str | None = None
    ) -> list[PairJudgment]:
        """Recorded pair judgments with both sides in ``observation_ids``."""
        run_filter = "AND d.run_id = $run" if run_id is not None else ""
        if not observation_ids:
            return []
        with self._lock:
            self._con.register("_jids", pa.table({"id": list(observation_ids)}))
            try:
                rows = _arrow(
                    self._con.execute(
                        f"SELECT d.body FROM decisions d WHERE d.decision_type = 'pair' "
                        f"AND d.left_id IN (SELECT id FROM _jids) "
                        f"AND d.right_id IN (SELECT id FROM _jids) {run_filter} "
                        "ORDER BY d.decision_id",
                        {"run": run_id} if run_id is not None else {},
                    )
                ).to_pylist()
            finally:
                self._con.unregister("_jids")
        return [_DECISION.validate_json(r["body"]) for r in rows]

    def evidence(self, ids: Sequence[str]) -> list[EvidenceItem]:
        if not ids:
            return []
        rows = self._all(
            "SELECT * FROM evidence WHERE list_contains(?, evidence_id) ORDER BY evidence_id",
            [list(ids)],
        )
        out = []
        for r in rows:
            r["value"] = _loads(r["value"])
            r["inputs"] = _loads(r["inputs"]) or {}
            r["score"] = None if r["score"] is None else Score.model_validate_json(r["score"])
            out.append(EvidenceItem.model_validate(r))
        return out

    def proposals(self, run_id: str) -> list[GroupProposal]:
        heads = self._all("SELECT * FROM proposals WHERE run_id = ? ORDER BY proposal_id", [run_id])
        members = self._all(
            "SELECT m.* FROM proposal_members m JOIN proposals p USING (proposal_id) "
            "WHERE p.run_id = ? ORDER BY m.proposal_id, m.node_id",
            [run_id],
        )
        by_proposal: dict[str, list[ProposalMember]] = {}
        for m in members:
            pid = m.pop("proposal_id")
            by_proposal.setdefault(pid, []).append(ProposalMember.model_validate(m))
        return [
            GroupProposal(**h, members=tuple(by_proposal.get(h["proposal_id"], ()))) for h in heads
        ]

    def run(self, run_id: str) -> ResolutionRun:
        row = self._one("SELECT * FROM runs WHERE run_id = ?", [run_id])
        if row is None:
            raise NotFound(f"run {run_id}")
        row["plan"] = _loads(row["plan"])
        return ResolutionRun.model_validate(row)

    def ledger(
        self,
        *,
        since_seq: int = 0,
        entity_id: str | None = None,
        observation_id: str | None = None,
        decision_id: str | None = None,
    ) -> Iterator[LedgerEvent]:
        sql = "SELECT * FROM ledger_events WHERE seq > ?"
        params: list[Any] = [since_seq]
        if decision_id is not None:
            sql += " AND decision_id = ?"
            params.append(decision_id)
        if entity_id is not None:
            sql += " AND ? IN (entity_id, related_entity_id)"
            params.append(entity_id)
        if observation_id is not None:
            sql += " AND observation_id = ?"
            params.append(observation_id)
        for r in self._all(sql + " ORDER BY seq", params):
            r["actor"] = Actor(kind=r.pop("actor_kind"), id=r.pop("actor_id"))
            r["payload"] = _loads(r["payload"]) or {}
            yield LedgerEvent.model_validate(r)

    def review(self, review_id: str) -> ReviewItem:
        row = self._one("SELECT * FROM review_items WHERE review_id = ?", [review_id])
        if row is None:
            raise NotFound(f"review {review_id}")
        row["members"] = tuple(_loads(row["members"]))
        return ReviewItem.model_validate(row)

    def constraints(
        self, observation_ids: Sequence[str] | None = None, *, open_only: bool = True
    ) -> list[PairConstraint]:
        """Pair constraints touching any of ``observation_ids`` (all, when None)."""
        where = "WHERE valid_to_seq IS NULL" if open_only else "WHERE TRUE"
        if observation_ids is None:
            rows = self._all(f"SELECT * FROM pair_constraints {where} ORDER BY constraint_id")
        else:
            rows = self._by_ids(
                f"SELECT * FROM pair_constraints {where} AND (left_id IN (SELECT id FROM _qids) "
                "OR right_id IN (SELECT id FROM _qids)) ORDER BY constraint_id",
                observation_ids,
            )
        return [PairConstraint.model_validate(r) for r in rows]

    def constraints_among(self, entity_ids: Sequence[str]) -> list[tuple[PairConstraint, str, str]]:
        """Open constraints whose two observations currently belong to entities in
        ``entity_ids``, with those entities."""
        rows = self._by_ids(
            """
            SELECT c.*, ml.entity_id AS left_entity, mr.entity_id AS right_entity
            FROM pair_constraints c
            JOIN memberships ml ON ml.observation_id = c.left_id AND ml.valid_to_seq IS NULL
            JOIN memberships mr ON mr.observation_id = c.right_id AND mr.valid_to_seq IS NULL
            WHERE c.valid_to_seq IS NULL
              AND ml.entity_id IN (SELECT id FROM _qids) AND mr.entity_id IN (SELECT id FROM _qids)
            ORDER BY c.constraint_id
            """,
            entity_ids,
        )
        out = []
        for r in rows:
            left, right = r.pop("left_entity"), r.pop("right_entity")
            out.append((PairConstraint.model_validate(r), left, right))
        return out

    def open_reviews(self) -> list[ReviewItem]:
        rows = self._all("SELECT * FROM review_items WHERE status = 'open' ORDER BY review_id")
        for r in rows:
            r["members"] = tuple(_loads(r["members"]))
        return [ReviewItem.model_validate(r) for r in rows]

    # -- set-oriented ---------------------------------------------------------------------------

    # Candidate queries. Each pairs observations in a run's scope with every stored
    # observation of the run's entity type, so incremental runs find candidates among
    # already-resolved observations. At least one side of every pair is in scope;
    # pairs are ordered (left_id < right_id) and distinct.

    _TYPED = """
        run_type AS (SELECT entity_type FROM runs WHERE run_id = $run_id),
        scope AS (SELECT observation_id FROM run_scope WHERE run_id = $run_id),
        typed AS (
            SELECT DISTINCT f.observation_id, f.value FROM observation_features f
            JOIN observations o USING (observation_id)
            WHERE f.feature = $feature AND f.value IS NOT NULL
              AND o.entity_type = (SELECT entity_type FROM run_type)),
        df AS (SELECT value, count(*) AS n FROM typed GROUP BY value)
    """

    def _pairs(self, sql: str, params: dict[str, Any]) -> pa.Table:
        with self._lock:
            return _arrow(self._con.execute(sql, params))

    def blocking_pairs(self, run_id: str, feature: str, max_block: int) -> pa.Table:
        """Observations sharing a value of ``feature``. Values held by more than
        ``max_block`` observations are skipped (see ``oversize_blocks``)."""
        return self._pairs(
            f"""
            WITH {self._TYPED}
            SELECT least(a.observation_id, b.observation_id) AS left_id,
                   greatest(a.observation_id, b.observation_id) AS right_id,
                   min(a.value) AS value
            FROM typed a JOIN typed b ON a.value = b.value AND a.observation_id <> b.observation_id
            JOIN df ON df.value = a.value
            WHERE df.n <= $max_block AND a.observation_id IN (SELECT observation_id FROM scope)
            GROUP BY ALL ORDER BY left_id, right_id
            """,
            {"run_id": run_id, "feature": feature, "max_block": max_block},
        )

    def oversize_blocks(self, run_id: str, feature: str, max_block: int) -> list[tuple[str, int]]:
        rows = self._all(
            f"""
            WITH {self._TYPED}
            SELECT value, n FROM df
            WHERE n > $max_block AND value IN (
                SELECT value FROM typed WHERE observation_id IN (SELECT observation_id FROM scope))
            ORDER BY value
            """,
            {"run_id": run_id, "feature": feature, "max_block": max_block},
        )
        return [(r["value"], r["n"]) for r in rows]

    def rare_value_pairs(
        self, run_id: str, feature: str, per_observation: int, max_df: int
    ) -> pa.Table:
        """Pairs sharing a value that is among each side's ``per_observation`` rarest
        shared values (ties by value), with document frequency between 2 and ``max_df``.
        Values no other observation holds are not ranked: they can never form a pair."""
        return self._pairs(
            f"""
            WITH {self._TYPED},
            rare AS (
                SELECT t.observation_id, t.value, df.n FROM typed t JOIN df USING (value)
                WHERE df.n BETWEEN 2 AND $max_df  -- a value nobody else has pairs with no one
                QUALIFY row_number() OVER (
                    PARTITION BY t.observation_id ORDER BY df.n, t.value) <= $k)
            SELECT least(a.observation_id, b.observation_id) AS left_id,
                   greatest(a.observation_id, b.observation_id) AS right_id,
                   min(a.n)::DOUBLE AS score_value
            FROM rare a JOIN rare b ON a.value = b.value AND a.observation_id <> b.observation_id
            WHERE a.observation_id IN (SELECT observation_id FROM scope)
            GROUP BY ALL ORDER BY left_id, right_id
            """,
            {"run_id": run_id, "feature": feature, "max_df": max_df, "k": per_observation},
        )

    def cross_feature_pairs(
        self, run_id: str, left_feature: str, right_feature: str, max_block: int
    ) -> pa.Table:
        """Pairs where one side's ``left_feature`` equals the other's ``right_feature``
        (an acronym and a short name, for instance)."""
        return self._pairs(
            """
            WITH run_type AS (SELECT entity_type FROM runs WHERE run_id = $run_id),
            scope AS (SELECT observation_id FROM run_scope WHERE run_id = $run_id),
            typed AS (
                SELECT f.observation_id, f.feature, f.value FROM observation_features f
                JOIN observations o USING (observation_id)
                WHERE f.feature IN ($left, $right) AND f.value IS NOT NULL
                  AND o.entity_type = (SELECT entity_type FROM run_type)),
            l AS (SELECT DISTINCT observation_id, value FROM typed WHERE feature = $left),
            r AS (SELECT DISTINCT observation_id, value FROM typed WHERE feature = $right),
            sized AS (SELECT value FROM r GROUP BY value HAVING count(*) <= $max_block)
            SELECT least(l.observation_id, r.observation_id) AS left_id,
                   greatest(l.observation_id, r.observation_id) AS right_id,
                   min(l.value) AS value
            FROM l JOIN r ON l.value = r.value AND l.observation_id <> r.observation_id
            WHERE l.value IN (SELECT value FROM sized)
              AND (l.observation_id IN (SELECT observation_id FROM scope)
                   OR r.observation_id IN (SELECT observation_id FROM scope))
            GROUP BY ALL ORDER BY left_id, right_id
            """,
            {"run_id": run_id, "left": left_feature, "right": right_feature,
             "max_block": max_block},
        )  # fmt: skip

    def overlap_pairs(self, run_id: str, feature: str, min_share: float, max_df: int) -> pa.Table:
        """Pairs sharing at least ``min_share`` of the smaller side's values, counting
        only values with document frequency at most ``max_df``."""
        return self._pairs(
            f"""
            WITH {self._TYPED},
            kept AS (SELECT t.* FROM typed t JOIN df USING (value) WHERE df.n <= $max_df),
            sizes AS (SELECT observation_id, count(*) AS k FROM kept GROUP BY observation_id),
            shared AS (
                SELECT least(a.observation_id, b.observation_id) AS left_id,
                       greatest(a.observation_id, b.observation_id) AS right_id,
                       count(DISTINCT a.value) AS s
                FROM kept a JOIN kept b ON a.value = b.value AND a.observation_id <> b.observation_id
                WHERE a.observation_id IN (SELECT observation_id FROM scope)
                GROUP BY ALL)
            SELECT shared.left_id, shared.right_id,
                   s / least(sl.k, sr.k)::DOUBLE AS score_value
            FROM shared
            JOIN sizes sl ON sl.observation_id = shared.left_id
            JOIN sizes sr ON sr.observation_id = shared.right_id
            WHERE s >= $min_share * least(sl.k, sr.k)
            ORDER BY left_id, right_id
            """,
            {"run_id": run_id, "feature": feature, "max_df": max_df, "min_share": min_share},
        )

    def feature_frequencies(
        self, feature: str, *, by: str = "entity", resolved_only: bool = False
    ) -> pa.Table:
        """How many entities (or observations) carry each value of a feature.

        By entity, an observation with no membership counts as its own entity, unless
        ``resolved_only``: then only observations with a membership count. An
        incremental run uses ``resolved_only`` so its own unresolved scope does not
        make a name look common (design 03 §8.5)."""
        if by not in ("entity", "observation"):
            raise ValueError("by must be 'entity' or 'observation'")
        unit = (
            "count(DISTINCT coalesce(m.entity_id, f.observation_id))"
            if by == "entity"
            else "count(DISTINCT f.observation_id)"
        )
        with self._lock:
            result = self._con.execute(
                f"""
                SELECT f.value, {unit} AS n FROM observation_features f
                LEFT JOIN memberships m
                    ON m.observation_id = f.observation_id AND m.valid_to_seq IS NULL
                WHERE f.feature = ? {"AND m.entity_id IS NOT NULL" if resolved_only else ""}
                GROUP BY f.value ORDER BY f.value
                """,
                [feature],
            )
            return _arrow(result)


class _Reader:
    """Current-state reads for ``CommitPlan``, inside the commit's transaction."""

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con

    def _by_ids(self, sql: str, ids: list[str]) -> list[tuple]:
        if not ids:
            return []
        self._con.register("_plan_ids", pa.table({"id": ids}))
        try:
            return self._con.execute(sql).fetchall()
        finally:
            self._con.unregister("_plan_ids")

    def load_entities(self, entity_ids: list[str]) -> dict[str, EntityRow]:
        rows = self._by_ids(
            "SELECT entity_id, entity_type, state, version, created_seq, retired_seq, "
            "merged_into FROM entities JOIN _plan_ids ON entity_id = id",
            entity_ids,
        )
        return {r[0]: EntityRow(*r) for r in rows}

    def load_observation_types(self, observation_ids: list[str]) -> dict[str, str]:
        rows = self._by_ids(
            "SELECT observation_id, entity_type FROM observations "
            "JOIN _plan_ids ON observation_id = id",
            observation_ids,
        )
        return dict(rows)

    def load_open_memberships(self, observation_ids: list[str]) -> dict[str, MembershipRow]:
        rows = self._by_ids(
            "SELECT membership_id, observation_id, entity_id, basis_decision_id, valid_from_seq "
            "FROM memberships JOIN _plan_ids ON observation_id = id WHERE valid_to_seq IS NULL",
            observation_ids,
        )
        return {r[1]: MembershipRow(*r) for r in rows}

    def load_members(self, entity_ids: list[str]) -> dict[str, list[MembershipRow]]:
        rows = self._by_ids(
            "SELECT membership_id, observation_id, entity_id, basis_decision_id, valid_from_seq "
            "FROM memberships JOIN _plan_ids ON entity_id = id WHERE valid_to_seq IS NULL",
            entity_ids,
        )
        out: dict[str, list[MembershipRow]] = {}
        for r in rows:
            out.setdefault(r[2], []).append(MembershipRow(*r))
        return out

    def load_entity(self, entity_id: str) -> EntityRow | None:
        row = _first(
            self._con.execute(
                "SELECT entity_id, entity_type, state, version, created_seq, retired_seq, "
                "merged_into FROM entities WHERE entity_id = ?",
                [entity_id],
            )
        )
        return None if row is None else EntityRow(*row)

    def load_open_membership(self, observation_id: str) -> MembershipRow | None:
        row = _first(
            self._con.execute(
                "SELECT membership_id, observation_id, entity_id, basis_decision_id, valid_from_seq "
                "FROM memberships WHERE observation_id = ? AND valid_to_seq IS NULL",
                [observation_id],
            )
        )
        return None if row is None else MembershipRow(*row)

    def load_entity_members(self, entity_id: str) -> list[MembershipRow]:
        rows = self._con.execute(
            "SELECT membership_id, observation_id, entity_id, basis_decision_id, valid_from_seq "
            "FROM memberships WHERE entity_id = ? AND valid_to_seq IS NULL",
            [entity_id],
        ).fetchall()
        return [MembershipRow(*r) for r in rows]

    def load_observation_type(self, observation_id: str) -> str | None:
        row = _first(
            self._con.execute(
                "SELECT entity_type FROM observations WHERE observation_id = ?", [observation_id]
            )
        )
        return None if row is None else row[0]

    def relationship_is_open(self, relationship_id: str) -> bool | None:
        row = _first(
            self._con.execute(
                "SELECT valid_to_seq IS NULL FROM relationships WHERE relationship_id = ?",
                [relationship_id],
            )
        )
        return None if row is None else bool(row[0])

    def review_status(self, review_id: str) -> str | None:
        row = _first(
            self._con.execute("SELECT status FROM review_items WHERE review_id = ?", [review_id])
        )
        return None if row is None else row[0]

    def run_status(self, run_id: str) -> str | None:
        row = _first(self._con.execute("SELECT status FROM runs WHERE run_id = ?", [run_id]))
        return None if row is None else row[0]

    def constraint_is_open(self, constraint_id: str) -> bool | None:
        row = _first(
            self._con.execute(
                "SELECT valid_to_seq IS NULL FROM pair_constraints WHERE constraint_id = ?",
                [constraint_id],
            )
        )
        return None if row is None else bool(row[0])

    def load_cannot_links(self, observation_ids: list[str]) -> list[tuple[str, str, str]]:
        """Open cannot-links whose two observations are both in ``observation_ids``."""
        return [
            tuple(r)
            for r in self._by_ids(
                "SELECT constraint_id, left_id, right_id FROM pair_constraints "
                "WHERE kind = 'cannot_link' AND valid_to_seq IS NULL "
                "AND left_id IN (SELECT id FROM _plan_ids) AND right_id IN (SELECT id FROM _plan_ids)",
                observation_ids,
            )
        ]
