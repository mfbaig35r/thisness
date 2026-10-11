# Design 02: Core Model and Storage

Status: accepted 2026-10-07, including the five decisions in §15.
Builds on: design 01 (runs, proposals, consensus, decisions D1 to D6).
Sketches: `docs/design/sketches/02_schema.sql` (DuckDB DDL) and `02_schema_check.py` (plays merges, an unmerge and a partial split through a minimal commit; output in `02_schema_check.out`).

---

## 1. Scope

This document fixes what M1 persists and how it is shaped: identifiers, versioned components, profiles and attributes, every domain object, the write boundary, the storage protocol, the DuckDB schema, the queries that must stay cheap, and the invariants the test suite enforces.

It does not cover how candidates, evidence and decisions are *computed*. That is design 03 (normalizers, candidate generators, evidence providers, the rules resolver, Fellegi-Sunter with EM and term-frequency adjustment, and policy).

## 2. Summary

1. **Two kinds of writes.** *Facts* (observations, run artifacts, evidence, proposals, decisions) are immutable and can be appended at any time. *Identity state* (entities, memberships, snapshots, relationships, review items) changes only through `commit(ChangeSet)`, which writes ledger events and state in one transaction. There is no other write path, so "a model may propose identity but may not bypass policy" is enforced by the storage interface, not by convention.
2. **The ledger is sufficient.** Current memberships and entities can be rebuilt from ledger events alone. The sketch checks this after every scenario.
3. **Retired IDs are resolved, not redirected.** A merged-away entity resolves to wherever its final members live *now*. After a partial split that is two entities, and the API says so instead of pointing at the wrong one.
4. **Unmerge reactivates.** Splitting a retired entity's members back out reactivates its original ID, so references held downstream become correct again.
5. **Merges move memberships.** Current state is always one hop (observation to live entity). History lives in ended memberships and the ledger.
6. **Versions count commits.** An entity's version increments once per commit that touches it; snapshot versions match entity versions and never skip.
7. **Time is the ledger sequence.** Membership and relationship validity are recorded as ledger positions, so "as of" queries are exact. Wall-clock timestamps are kept for people.
8. **One storage implementation in M1.** DuckDB, file-backed or `:memory:`. The protocol exists so PostgreSQL can follow.

---

## 3. Identifiers

**Format.** `{prefix}_{ULID}`, for example `obs_01K9ZQ4T7M3X0B2V8N6C5D1E2F`. ULIDs are time-ordered, so IDs sort by creation. Thisness generates them itself (48-bit millisecond time + 80 random bits, Crockford base32); no dependency.

**Prefixes.**

| Object | Prefix |
|---|---|
| Observation | `obs` |
| Entity | profile-declared, defaults to the entity type (`org`, `person`, `species`) |
| Membership | `mem` |
| Evidence | `ev` |
| Decision (all kinds) | `dec` |
| Run / stage / proposal | `run` / `stg` / `prop` |
| Relationship | `rel` |
| Review item | `rev` |
| Ledger event / commit | `evt` / `cmt` |

The non-entity prefixes are reserved. A profile whose `id_prefix` collides with a reserved prefix, or with another profile's, is rejected at registration.

**Node references.** Design 01's `NodeRef` (observation or entity) is just the ID string: the prefix says which. Evidence, decisions and proposals store `left_id`, `right_id`, `node_id` as plain strings.

**Determinism.** An `IdFactory` is injectable. Tests use a seeded factory, so whole runs are reproducible byte-for-byte.

---

## 4. Versioned components

Every pluggable part (profile, normalizer, candidate generator, evidence provider, resolver, edge policy, consensus strategy, policy, canonicalizer) has a `ComponentRef`:

```text
{kind}:{name}@{version}#{config_hash12}      resolver:fellegi_sunter@1.0#3fa91c0d22be
```

`config_hash` is a stable hash of the component's configuration, so the same code with different thresholds is a different component. Components are registered in the `components` table on first use. Every stage, evidence item and decision records the refs it used, which is what makes explanations and shadow comparisons reproducible (PRD §42, invariant 4).

---

## 5. Profiles and attributes

Shape only; behavior is design 03.

```python
class AttributeDefinition(BaseModel, frozen=True):
    name: str
    data_type: Literal["string", "integer", "number", "boolean", "date", "datetime", "json"]
    cardinality: Literal["single", "multi"] = "single"
    nullable: bool = True
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    log_policy: Literal["allow", "hash", "redact"] = "allow"           # evidence inputs, logs
    external_model_policy: Literal["allow", "redact", "hash", "local_only", "forbidden"] = "allow"
    identity_strength: Literal["weak", "moderate", "strong"] | None = None  # documentation only
    temporal: bool = False
    display: str | None = None

class ResolutionProfile(BaseModel, frozen=True):
    entity_type: str
    id_prefix: str
    version: str
    attributes: dict[str, AttributeDefinition]
    normalizers: list[Normalizer]                    # produce observation features
    candidate_generators: list[CandidateGenerator]
    evidence_providers: list[EvidenceProvider]
    constraints: list[Constraint]
    resolver: Resolver
    edge_policy: EdgePolicy
    consensus: ConsensusStrategy
    policy: ResolutionPolicy
    canonicalizer: Canonicalizer
    relationships: list[RelationshipDefinition]
    persist_candidates: bool = False
```

`identity_strength` never sets a weight (PRD §7); weights come from the resolver. A `vector` data type is deliberately absent until the third test domain is chosen; `json` holds anything structured in the meantime.

**One active profile per entity type.** Different identity semantics are different entity types. If the same source function must be resolved both as "same implementation" and "functionally equivalent" (PRD §4.6), it is ingested twice, once per entity type. See §15, decision 2.

---

## 6. Domain objects

All are Pydantic v2 models with `frozen=True`. They are the API and serialization contract (the future MCP layer serializes them as-is). Bulk paths do not construct a model per row: batches move as Arrow tables (§8).

### 6.1 Facts: observations

```python
class SourceRef(BaseModel, frozen=True):
    type: str                       # "document", "csv", "crm", ...
    id: str
    locator: dict[str, Any] = {}    # {"page": 3}, {"row": 1182}
    version: str | None = None      # source hash or revision

class Observation(BaseModel, frozen=True):
    observation_id: str
    entity_type: str
    attributes: dict[str, Any] | None     # None once redacted
    source: SourceRef
    context: dict[str, Any] = {}
    observed_at: datetime | None = None   # when the source says it was true
    ingested_at: datetime
    fingerprint: str
    supersedes: str | None = None         # correction of an earlier observation
    redacted_at: datetime | None = None
```

- **Idempotent ingest.** `fingerprint` hashes entity type, source ref and canonical-JSON attributes. Re-ingesting the same source record returns the existing ID. The same text in two documents has two source refs, so it is two observations, as intended.
- **Corrections** are new observations with `supersedes`, never edits.
- **Redaction** is the one sanctioned mutation (research report: immutability is a logical rule, not a legal one). Attributes become `None`, an `OBSERVATION_REDACTED` event is written, the ID, source ref and memberships remain. Evidence `inputs` for redacted observations are cleared by the same commit.
- **Features** (normalized values used for blocking and comparison) live in `observation_features`, one row per (observation, feature, ordinal), tagged with the normalizer ref. They are a projection: dropping and rebuilding them never changes identity state.

### 6.2 Facts: run artifacts

From design 01, persisted as written:

| Object | Notes |
|---|---|
| `ResolutionRun` | `base_seq`, profile ref, status, plan; scope in `run_scope` |
| `StageExecution` | kind, producer ref, family, view (`raw`/`derived`), `conditioned_on` |
| `Candidate` | stored only if the profile sets `persist_candidates` |
| `EvidenceItem` | below |
| `GroupProposal` | `proposals` + `proposal_members` (`shown`, `group_no`, `label_out`, `uncertain`) |
| `Decision` | below |

```python
class Score(BaseModel, frozen=True):
    kind: str                         # "fellegi_sunter_weight", "jaro_winkler", "calibrated_match_probability"
    value: float | str
    calibration_ref: str | None = None
    model_ref: str | None = None

class EvidenceItem(BaseModel, frozen=True):
    evidence_id: str
    left_id: str                      # observation or entity
    right_id: str
    kind: str                         # "normalized_name_match", "identifier_conflict", ...
    attribute: str | None
    level: str | None                 # comparison level label; Fellegi-Sunter consumes this
    direction: Literal["supports", "contradicts", "neutral"]
    strength: Literal["weak", "moderate", "strong", "decisive"] | None
    value: Any
    score: Score | None
    method_ref: str
    inputs: dict[str, Any]            # filtered by each attribute's log_policy
    run_id: str | None
    stage_id: str | None
```

**Decisions** share one table so that every `basis_decision_id` resolves in one place. `decision_type` is the discriminator:

| Type | Produced by | Key fields |
|---|---|---|
| `pair` | resolvers (design 01's `PairJudgment`) | `left_id`, `right_id`, `verdict`, `scores`, `evidence_ids` |
| `consensus` | consensus (design 01 `ConsensusDecision`) | `outcome`, `members`, `target_entity_id`, `flags` |
| `operator` | explicit API calls: merge, split, promote, relate | `outcome`, `reason`, actor |
| `review` | a human resolving a review item (M2) | `verdict`, `reason`, actor |

Every decision carries `component_refs` and an `Actor(kind, id)` with kind `resolver | human | system | agent`.

### 6.3 Identity state

```python
class Entity(BaseModel, frozen=True):
    entity_id: str
    entity_type: str
    state: Literal["active", "provisional", "retired"]
    version: int                      # +1 per commit that touches it
    created_seq: int
    retired_seq: int | None
    merged_into: str | None           # historical; resolution is derived (6.5)

class EntitySnapshot(BaseModel, frozen=True):
    entity_id: str
    version: int                      # equals the entity version it was built at
    attributes: dict[str, Any]
    derived_from: list[str]           # observation ids
    canonicalizer_ref: str
    created_seq: int

class IdentityMembership(BaseModel, frozen=True):
    membership_id: str
    observation_id: str
    entity_id: str
    basis_decision_id: str
    valid_from_seq: int
    valid_to_seq: int | None          # None = current

class RelationshipAssertion(BaseModel, frozen=True):
    relationship_id: str
    relation: str                     # never "same_entity"
    subject_entity_id: str
    object_entity_id: str
    basis_decision_id: str
    evidence_ids: list[str]
    effective_from: date | None       # domain time: when it was true
    effective_to: date | None
    valid_from_seq: int               # system time: when we believed it
    valid_to_seq: int | None

class ReviewItem(BaseModel, frozen=True):
    review_id: str
    kind: Literal["cluster", "boundary", "merge", "split"]
    decision_id: str                  # the ConsensusDecision that opened it
    members: list[str]                # node ids under review
    status: Literal["open", "resolved", "dismissed"]
    opened_seq: int
    closed_seq: int | None
    resolution_decision_id: str | None
```

- **Snapshots are kept for every version.** A pair decision against an entity cites the snapshot version it saw; `explain()` must be able to show that snapshot later. One snapshot per touched entity per commit, so a batch run writes one version per entity, not one per membership.
- **Relationships are bitemporal in shape from M1** (columns exist), but M1 exposes no temporal query API. Relationships never touch memberships (invariant 6). `same_entity` is a reserved relation name and is rejected.
- **Review items persist in M1** because consensus produces review outcomes in M1. The queue UX is M2.
- **Aliases, external identifier assertions and resolution memory** are not in M1. They will be assertion tables with a basis decision and revocation, shaped like relationships. Nothing in this schema prevents them.

### 6.4 Ledger

```python
class LedgerEvent(BaseModel, frozen=True):
    seq: int                          # global, gapless within a store, monotonic
    event_id: str
    commit_id: str                    # groups the events of one atomic commit
    event_type: str
    run_id: str | None
    actor: Actor
    observation_id: str | None
    entity_id: str | None
    related_entity_id: str | None
    decision_id: str | None
    payload: dict[str, Any]
    recorded_at: datetime
```

M1 event types: `OBSERVATION_INGESTED`, `OBSERVATION_REDACTED`, `RUN_STARTED`, `RUN_PROPOSED`, `RUN_COMMITTED`, `RUN_ABANDONED`, `ENTITY_CREATED` (payload carries `state`, so provisional creation is this event), `ENTITY_PROMOTED`, `ENTITY_MERGED` (payload: `members_at_retirement`), `ENTITY_SPLIT`, `ENTITY_REACTIVATED`, `MEMBERSHIP_ACCEPTED`, `MEMBERSHIP_ENDED`, `RELATIONSHIP_CREATED`, `RELATIONSHIP_REVOKED`, `REVIEW_OPENED`, `REVIEW_CLOSED`, `SPLIT_PROPOSED`.

### 6.5 Merge, split, unmerge, resolution

**Merge** (survivor S, loser L): end each of L's open memberships and accept it on S, all citing the merge decision; write `ENTITY_MERGED` with `members_at_retirement`; set L to `retired` with `merged_into = S`. L's ID is never reused.

**Split** (source X, targets): for each target, if it does not exist, create it; if it is a retired entity, **reactivate** it (`ENTITY_REACTIVATED`); then move the listed observations. An unmerge is a split whose target is the retired entity.

**Resolution of an ID.** `resolve_id(id)` returns a list:

- live entity: itself;
- retired entity: the current entities of its `members_at_retirement`. One entity normally; several after a partial split.

The sketch shows why this is derived rather than stored. After merging B into A and splitting only one of B's two observations into a new D, a stored redirect would still send `org_B` to A. Derived resolution returns `[org_A, org_D]`, which is the truth. After a full unmerge, B is active again and resolves to itself.

Callers that need exactly one answer get an explicit `AmbiguousIdentity` error carrying the candidates, never a silent pick.

---

## 7. The write boundary

```text
facts (append any time)                    identity state (commit only)
-----------------------------------        ------------------------------------
observations, observation_features         entities, entity_snapshots
runs, run_scope, stages, candidates        memberships, relationships
evidence, proposals, proposal_members      review_items
decisions, components                      ledger_events
```

- Writing a fact never changes what any observation refers to.
- `commit(ChangeSet)` is the only operation that writes the right-hand column. It validates that every op cites a decision that exists, checks conflicts against `base_seq` and the read set (design 01 §8), checks `expected_version` for operator merges and splits, then writes events and state in one transaction and returns the new head sequence.
- A rejected commit writes nothing (sketch: a stale merge leaves the ledger unchanged).
- Decisions are facts, so a decision can exist without ever being committed: that is what a proposal is.

---

## 8. Storage protocol

```python
class Store(Protocol):
    # lifecycle
    def migrate(self) -> None: ...
    def head_seq(self) -> int: ...

    # facts
    def ingest(self, observations: Iterable[Observation]) -> IngestResult: ...      # idempotent
    def write_features(self, batch: pa.Table) -> None: ...                         # projection
    def drop_features(self, normalizer_ref: str) -> None: ...
    def record_run(self, run: ResolutionRun, scope: Sequence[str]) -> None: ...
    def record_stage(self, stage: StageExecution) -> None: ...
    def record_candidates(self, batch: pa.Table) -> None: ...
    def record_evidence(self, batch: pa.Table) -> None: ...
    def record_proposals(self, proposals: Sequence[GroupProposal]) -> None: ...
    def record_decisions(self, decisions: Sequence[Decision]) -> None: ...
    def register_component(self, ref: ComponentRef, config: dict) -> None: ...

    # identity state: the only path
    def commit(self, changes: ChangeSet) -> CommitResult: ...

    # reads
    def observations(self, ids: Sequence[str]) -> list[Observation]: ...
    def entity(self, entity_id: str) -> Entity: ...
    def resolve_id(self, entity_id: str) -> list[str]: ...
    def snapshot(self, entity_id: str, version: int | None = None) -> EntitySnapshot: ...
    def members(self, entity_id: str, as_of_seq: int | None = None) -> list[str]: ...
    def entity_of(self, observation_id: str, as_of_seq: int | None = None) -> str | None: ...
    def decisions(self, ids: Sequence[str]) -> list[Decision]: ...
    def evidence(self, ids: Sequence[str]) -> list[EvidenceItem]: ...
    def proposals(self, run_id: str) -> list[GroupProposal]: ...
    def ledger(self, *, since_seq: int = 0, entity_id: str | None = None,
               observation_id: str | None = None) -> Iterator[LedgerEvent]: ...
    def open_reviews(self, entity_type: str | None = None) -> list[ReviewItem]: ...

    # set-oriented work pushed down to the engine
    def blocking_pairs(self, run_id: str, feature: str, max_block: int) -> pa.Table: ...
    def feature_frequencies(self, feature: str) -> pa.Table: ...   # term frequencies for FS (design 03)
```

**Bulk data moves as Arrow.** Features, candidates and evidence are written as `pyarrow` tables, which DuckDB ingests without a per-row round trip. Row-at-a-time `executemany` is orders of magnitude slower for this volume. `resolve_many` also accepts Arrow tables, which is how the "dataframes without making pandas mandatory" requirement (PRD §25) is met.

**Set-oriented methods.** Blocking self-joins and term-frequency counts run inside the store as SQL. Candidate generators call these methods instead of issuing SQL themselves, so a future PostgreSQL store implements the same methods in its own dialect.

---

## 9. DuckDB schema

The full DDL is `docs/design/sketches/02_schema.sql`; it becomes the M1 migration `0001`. Tables, grouped by §7: 10 fact tables, 5 identity-state tables, the ledger, plus `meta` (schema version) and `components`: 18 in all.

Choices forced by DuckDB:

- **No foreign keys.** DuckDB restricts updates on rows referenced by foreign keys, and memberships and entities are updated in place. Referential integrity is checked by the invariant suite (§11) instead.
- **No partial unique indexes.** "At most one open membership per observation" is enforced by `commit()` and checked by the suite.
- **Few indexes.** Primary keys, `observation_features(feature, value)`, and memberships by observation and by entity. DuckDB joins mostly by hashing; extra ART indexes slow bulk inserts.
- **JSON for nested payloads, native columns for anything filtered or joined.** Keeps the PostgreSQL port mechanical (`JSON` to `JSONB`).
- **One writer per database file.** DuckDB allows one read-write process or several read-only processes, not both. M1 is a library in one process, so this is fine. It matters for the M2 UI: the UI must be served from the process that owns the store, not open the file separately (§14).

The sketch runs identically on DuckDB 1.3 and 1.5.

---

## 10. Queries that must stay cheap

All four are exercised by the sketch:

| Query | Shape |
|---|---|
| Entity of an observation, now or as of seq S | one indexed lookup on `memberships` with `valid_from_seq <= S < coalesce(valid_to_seq, ∞)` |
| Current members of an entity | indexed lookup, `valid_to_seq IS NULL` |
| Resolve a retired ID | last `ENTITY_MERGED` event for the ID, unnest `members_at_retirement`, join current memberships |
| Blocking pairs in a run | self-join of `observation_features` on (feature, value) restricted to `run_scope`, skipping blocks larger than a cap |

`explain(decision_id)` assembles from `decisions`, `evidence`, `entity_snapshots` (at the cited version), `proposals` and `ledger_events`, all by primary key. No LLM is involved (PRD §32).

---

## 11. Invariants the test suite enforces

Run after every test that commits, and available as `thisness check` later:

1. **Replay equivalence:** memberships and entities rebuilt from the ledger alone equal the materialized tables (including versions).
2. At most one open membership per observation.
3. Every membership, relationship and review item cites a decision that exists.
4. Retired entities hold no open memberships.
5. Open memberships point at live (active or provisional) entities.
6. No relationship has relation `same_entity`, and relationship operations never create or end memberships (relationships cannot create identity).
7. Every snapshot version equals an entity version that existed, and versions are gapless per entity.
8. Every component ref cited anywhere is registered.
9. Redacted observations have no attribute payload and no evidence `inputs`.

The sketch implements 1, 2, 3 (memberships), 4 and 5.

---

## 12. Engine surface for M1

```python
from thisness import Thisness
from thisness.profiles import organization

engine = Thisness(store="identity.duckdb", profiles=[organization()])  # default store ":memory:"

engine.ingest(observations)                        # facts only
result = engine.resolve(entity_type="organization", attributes={...}, source={...})
results = engine.resolve_many(observations)        # one run (design 01)

engine.get_entity(entity_id)                       # Entity + current snapshot
engine.resolve_id(entity_id)                       # list; raises AmbiguousIdentity on request
engine.members(entity_id, as_of_seq=None)
engine.history(entity_id=..., observation_id=...)  # ledger events
engine.explain(decision_id)

engine.merge_entities(survivor, other, reason=..., expected_versions=(17, 9))
engine.split_entity(entity_id, {"new": [...], "org_B": [...]}, reason=..., expected_version=21)
engine.promote(entity_id, reason=...)
engine.relate(subject, relation, object, reason=..., evidence_ids=[...])
```

Operator calls create an `operator` decision and commit it in one step. The advanced, stage-by-stage API from design 01 sits underneath and uses the same store calls.

## 13. M1 package layout

```text
src/thisness/
  __init__.py            Thisness, public models
  ids.py                 prefixed ULIDs, IdFactory
  core/                  observation, evidence (Score, EvidenceItem), decisions,
                         identity (Entity, Snapshot, Membership, Relationship),
                         run (ResolutionRun, StageExecution, GroupProposal, ChangeSet),
                         ledger, review, components
  profiles/              base (ResolutionProfile, AttributeDefinition), organization
  normalize/  candidates/  evidence/  resolvers/  consensus/  policy/  canonicalize/
  storage/               protocol, duckdb/ (store, migrations/0001.sql)
  engine.py
  eval/
tests/
```

Core dependencies: `pydantic`, `duckdb`, `pyarrow`, `rapidfuzz`. Python ≥ 3.12.

---

## 14. Tradeoffs

- **`pyarrow` becomes a core dependency** (about 40 MB installed). The alternative is slow bulk writes or a pandas dependency. The PRD's "keep core light" list targets ML and search stacks, not Arrow, and Arrow is already the batch interchange format.
- **No foreign keys** means integrity depends on `commit()` and the invariant suite. The suite runs in every test, so a violation fails CI rather than corrupting a user's store silently.
- **Every snapshot version is kept.** Storage grows with commits per entity. Compaction (keeping only versions cited by decisions) can come later without schema change.
- **Merges move memberships**, so a merge writes two membership events per moved observation. The alternative (leave memberships on the loser, follow redirects on read) makes merges cheap and every read expensive. Reads win.
- **Single writer.** The M2 UI must run inside the engine's process or against a read-only copy.

## 15. Decisions (accepted 2026-10-07)

| # | Question | Decision |
|---|---|---|
| 1 | Observations in a `REVIEW_CLUSTER` with no existing entity: what ID do they get? | Each gets its own provisional entity (coverage, per D5), **plus** an auto-merge hold: nodes under an open review item are excluded from automatic `MERGE_PROVISIONAL` until it closes. Without the hold, the next run would merge exactly what was sent to review. |
| 2 | Different identity semantics for the same source thing (PRD §4.6) | Separate entity types, one active profile each; ingest the source once per type |
| 3 | `resolve_id` on a retired entity whose members split | Return the list; single-answer callers get `AmbiguousIdentity`. Never auto-pick. |
| 4 | Unmerge behavior when a split's partition matches a retired entity's final members | Engine suggests reactivation; the operator call names the retired ID as the target to do it. Not automatic. |
| 5 | Fingerprint of a restricted attribute is a plain hash | Fine for M1. Before privacy work, switch to a keyed hash (HMAC with a store secret), since a plain hash of a short identifier can be brute-forced. |

## 16. Implementation notes (M1 step 2)

Where the DuckDB store, as built, differs from or sharpens this document:

- **Ledger sequence numbers are assigned by `commit()`**, gapless from the store's head, instead of a database sequence. Commits are serialized per store, and this lets a commit be written in a few Arrow bulk inserts. A 10,000-entity create commits in about 0.7 s.
- **`commit()` prefetches** every entity, observation and membership the change set names in four queries before applying any op.
- **Decisions keep their full validated body** in a `body` JSON column, alongside the queryable columns, so every decision type reads back exactly. Evidence stores its `Score` whole as JSON.
- **Ingest and run start write bookkeeping events** (`OBSERVATION_INGESTED`, `RUN_STARTED`) so the ledger is a complete timeline. They carry no identity change and never advance an entity's version.
- **Split targets must be new or retired.** Moving observations into a live entity is a merge, not a split. A split may not empty its source. A reactivated entity comes back `active`.
- **Versions count identity events only.** An entity's version advances once per commit containing an `ENTITY_*` or `MEMBERSHIP_*` event that names it (as subject or related entity). Relationship and review events never advance it, so snapshots are only rebuilt when membership or state changes.
- **The run check runs first.** A change set for an unknown, committed or abandoned run is rejected before any op is considered.
- **Reads go through Arrow**, which returns timezone-aware datetimes without a `pytz` dependency and works across DuckDB 1.3 to 1.5, whose Arrow fetch method changed name.
- **The invariant suite** (`thisness.storage.duckdb.invariants.check`) replays memberships, entities (including versions), relationships and review items from the ledger, and adds checks for gapless sequence numbers, contiguous commits, membership types, and snapshot contents. The test fixture runs it after every store test, and a randomized test runs it after each of 200 mixed operations.

## 17. Next

Design 03: normalizers and features, candidate generators (exact, blocking, fuzzy), evidence providers and comparison levels, the deterministic rules resolver, Fellegi-Sunter (EM-fitted m/u, term-frequency adjustment), the `FellegiSunterEdgePolicy` that feeds design 01's consensus, and the resolution policy.
