-- Thisness core schema, DuckDB dialect. Sketch for docs/design/02-core-model-and-storage.md.
--
-- Three groups:
--   facts           immutable, appended at any time, never change identity state
--   identity state  materialized current state, changed only by commit()
--   ledger          append-only; every identity-state change is an event here
--
-- No foreign keys: DuckDB restricts updates on rows referenced by foreign keys,
-- and referential integrity is checked by the invariant suite instead (§11).
-- No partial unique indexes in DuckDB: "one open membership per observation"
-- is enforced by commit() and checked by the invariant suite.

CREATE TABLE meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);

-- Versioned components: profiles, normalizers, generators, evidence providers,
-- resolvers, edge policies, consensus strategies, policies, canonicalizers.
CREATE TABLE components (
    component_ref VARCHAR PRIMARY KEY,      -- "{kind}:{name}@{version}#{config_hash12}"
    kind          VARCHAR NOT NULL,
    name          VARCHAR NOT NULL,
    version       VARCHAR NOT NULL,
    config_hash   VARCHAR NOT NULL,
    config        JSON,
    registered_at TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------------
-- Facts
-- ---------------------------------------------------------------------------

CREATE TABLE observations (
    observation_id VARCHAR PRIMARY KEY,
    entity_type    VARCHAR NOT NULL,
    fingerprint    VARCHAR NOT NULL UNIQUE, -- idempotent ingest
    source_type    VARCHAR NOT NULL,
    source_id      VARCHAR NOT NULL,
    source_locator JSON,
    source_version VARCHAR,
    attributes     JSON,                    -- NULL once redacted
    context        JSON,
    observed_at    TIMESTAMPTZ,
    ingested_at    TIMESTAMPTZ NOT NULL,
    supersedes     VARCHAR,                 -- correction of an earlier observation
    redacted_at    TIMESTAMPTZ
);

-- Rebuildable projection: normalized values used for blocking and comparison.
CREATE TABLE observation_features (
    observation_id VARCHAR NOT NULL,
    feature        VARCHAR NOT NULL,        -- e.g. "name.key", "name.token", "domain"
    ordinal        INTEGER NOT NULL,        -- position for multi-valued features
    value          VARCHAR,
    value_num      DOUBLE,
    normalizer_ref VARCHAR NOT NULL
);
CREATE INDEX observation_features_lookup ON observation_features (feature, value);

CREATE TABLE runs (
    run_id        VARCHAR PRIMARY KEY,
    entity_type   VARCHAR NOT NULL,
    profile_ref   VARCHAR NOT NULL,
    base_seq      BIGINT NOT NULL,
    status        VARCHAR NOT NULL,         -- planned | executing | proposed | committed | abandoned
    plan          JSON NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL,
    committed_seq BIGINT
);

CREATE TABLE run_scope (
    run_id         VARCHAR NOT NULL,
    observation_id VARCHAR NOT NULL,
    PRIMARY KEY (run_id, observation_id)
);

CREATE TABLE stages (
    stage_id       VARCHAR PRIMARY KEY,
    run_id         VARCHAR NOT NULL,
    kind           VARCHAR NOT NULL,        -- candidates | evidence | pair_resolver | group_proposer | verify | human
    producer_ref   VARCHAR NOT NULL,
    family         VARCHAR NOT NULL,
    view           VARCHAR NOT NULL,        -- raw | derived
    conditioned_on JSON,
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ
);

-- Optional persistence (profile setting). Node ids carry their kind in the prefix.
CREATE TABLE candidates (
    run_id        VARCHAR NOT NULL,
    stage_id      VARCHAR NOT NULL,
    left_id       VARCHAR NOT NULL,
    right_id      VARCHAR NOT NULL,
    generator_ref VARCHAR NOT NULL,
    rank          INTEGER,
    score_kind    VARCHAR,
    score_value   DOUBLE
);

CREATE TABLE evidence (
    evidence_id  VARCHAR PRIMARY KEY,
    run_id       VARCHAR,
    stage_id     VARCHAR,
    left_id      VARCHAR NOT NULL,
    right_id     VARCHAR NOT NULL,
    kind         VARCHAR NOT NULL,          -- e.g. normalized_name_match, identifier_conflict
    attribute    VARCHAR,
    level        VARCHAR,                   -- comparison level label (consumed by Fellegi-Sunter)
    direction    VARCHAR NOT NULL,          -- supports | contradicts | neutral
    strength     VARCHAR,                   -- weak | moderate | strong | decisive
    value        JSON,
    score_kind   VARCHAR,
    score_value  DOUBLE,
    method_ref   VARCHAR NOT NULL,
    inputs       JSON,                      -- subject to attribute log_policy
    created_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE proposals (
    proposal_id   VARCHAR PRIMARY KEY,
    run_id        VARCHAR NOT NULL,
    stage_id      VARCHAR NOT NULL,
    batch_id      VARCHAR NOT NULL,
    invocation_id VARCHAR
);

CREATE TABLE proposal_members (
    proposal_id VARCHAR NOT NULL,
    node_id     VARCHAR NOT NULL,
    shown       VARCHAR NOT NULL,           -- exactly what the producer saw
    group_no    INTEGER,                    -- NULL = singleton (kept apart)
    label_out   VARCHAR,
    uncertain   BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (proposal_id, node_id)
);

-- All decision kinds share one table so every basis_decision_id resolves here.
CREATE TABLE decisions (
    decision_id      VARCHAR PRIMARY KEY,
    decision_type    VARCHAR NOT NULL,      -- pair | consensus | operator | review
    run_id           VARCHAR,
    left_id          VARCHAR,               -- pair
    right_id         VARCHAR,               -- pair
    verdict          VARCHAR,               -- pair/review: MATCH | NO_MATCH | POSSIBLE_MATCH | REQUIRES_REVIEW
    outcome          VARCHAR,               -- consensus/operator: CREATE_ENTITY | ATTACH | MERGE | SPLIT | ...
    target_entity_id VARCHAR,
    members          JSON,
    scores           JSON,                  -- list of typed Score
    evidence_ids     JSON,
    flags            JSON,                  -- conflicts, bridges, articulations, suggested cut
    component_refs   JSON,                  -- resolver / edge policy / consensus / policy versions
    actor_kind       VARCHAR NOT NULL,      -- resolver | human | system | agent
    actor_id         VARCHAR NOT NULL,
    reason           VARCHAR,
    created_at       TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------------
-- Identity state (materialized; written only by commit)
-- ---------------------------------------------------------------------------

CREATE TABLE entities (
    entity_id   VARCHAR PRIMARY KEY,
    entity_type VARCHAR NOT NULL,
    state       VARCHAR NOT NULL,           -- active | provisional | retired
    version     INTEGER NOT NULL,           -- +1 per commit that touches the entity
    created_seq BIGINT NOT NULL,
    retired_seq BIGINT,
    merged_into VARCHAR                     -- historical: what the merge did. Resolution of a
                                            -- retired id is derived from memberships (§6.5).
);

CREATE TABLE entity_snapshots (
    entity_id         VARCHAR NOT NULL,
    version           INTEGER NOT NULL,
    attributes        JSON NOT NULL,
    derived_from      JSON NOT NULL,        -- observation ids
    canonicalizer_ref VARCHAR NOT NULL,
    created_seq       BIGINT NOT NULL,
    PRIMARY KEY (entity_id, version)
);

CREATE TABLE memberships (
    membership_id     VARCHAR PRIMARY KEY,
    observation_id    VARCHAR NOT NULL,
    entity_id         VARCHAR NOT NULL,
    basis_decision_id VARCHAR NOT NULL,
    valid_from_seq    BIGINT NOT NULL,
    valid_to_seq      BIGINT                -- NULL = current
);
CREATE INDEX memberships_by_observation ON memberships (observation_id);
CREATE INDEX memberships_by_entity ON memberships (entity_id);

CREATE TABLE relationships (
    relationship_id   VARCHAR PRIMARY KEY,
    relation          VARCHAR NOT NULL,     -- never "same_entity"
    subject_entity_id VARCHAR NOT NULL,
    object_entity_id  VARCHAR NOT NULL,
    basis_decision_id VARCHAR NOT NULL,
    evidence_ids      JSON,
    effective_from    DATE,                 -- domain time (when it was true)
    effective_to      DATE,
    valid_from_seq    BIGINT NOT NULL,      -- system time (when we believed it)
    valid_to_seq      BIGINT
);

CREATE TABLE review_items (
    review_id              VARCHAR PRIMARY KEY,
    kind                   VARCHAR NOT NULL, -- cluster | boundary | merge | split
    run_id                 VARCHAR,
    decision_id            VARCHAR NOT NULL, -- the ConsensusDecision that opened it
    members                JSON NOT NULL,    -- node ids under review (auto-merge hold)
    status                 VARCHAR NOT NULL, -- open | resolved | dismissed
    opened_seq             BIGINT NOT NULL,
    closed_seq             BIGINT,
    resolution_decision_id VARCHAR
);

-- ---------------------------------------------------------------------------
-- Ledger
-- ---------------------------------------------------------------------------

CREATE SEQUENCE ledger_seq START 1;

CREATE TABLE ledger_events (
    seq               BIGINT PRIMARY KEY DEFAULT nextval('ledger_seq'),
    event_id          VARCHAR NOT NULL UNIQUE,
    commit_id         VARCHAR NOT NULL,     -- groups the events of one atomic commit
    event_type        VARCHAR NOT NULL,
    run_id            VARCHAR,
    actor_kind        VARCHAR NOT NULL,
    actor_id          VARCHAR NOT NULL,
    observation_id    VARCHAR,
    entity_id         VARCHAR,
    related_entity_id VARCHAR,
    decision_id       VARCHAR,
    payload           JSON,
    recorded_at       TIMESTAMPTZ NOT NULL
);
