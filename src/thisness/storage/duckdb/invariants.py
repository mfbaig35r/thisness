"""The invariant suite (design 02 §11).

``check(store)`` returns every violation found; an empty list means the store is
consistent. The test suite runs it after every commit. The central check is
replay equivalence: memberships, entities, relationships and review items
rebuilt from the ledger alone must equal the materialized tables.
"""

from __future__ import annotations

from dataclasses import dataclass

from thisness.storage.duckdb.store import DuckDBStore

_IDENTITY_EVENTS = (
    "'ENTITY_CREATED', 'ENTITY_PROMOTED', 'ENTITY_MERGED', 'ENTITY_SPLIT', "
    "'ENTITY_REACTIVATED', 'MEMBERSHIP_ACCEPTED', 'MEMBERSHIP_ENDED', 'ENTITY_LABELED', "
    "'ENTITY_LABEL_CLEARED'"
)

REPLAY = {
    "memberships": (
        """
        WITH acc AS (
            SELECT payload->>'membership_id' AS membership_id, observation_id, entity_id,
                   decision_id AS basis_decision_id, seq AS valid_from_seq
            FROM ledger_events WHERE event_type = 'MEMBERSHIP_ACCEPTED'),
        ended AS (
            SELECT payload->>'membership_id' AS membership_id, seq AS valid_to_seq
            FROM ledger_events WHERE event_type = 'MEMBERSHIP_ENDED')
        SELECT acc.*, ended.valid_to_seq FROM acc LEFT JOIN ended USING (membership_id)
        """,
        "membership_id, observation_id, entity_id, basis_decision_id, valid_from_seq, valid_to_seq",
    ),
    "entities": (
        f"""
        WITH created AS (
            SELECT entity_id, payload->>'entity_type' AS entity_type,
                   payload->>'state' AS state, seq AS created_seq
            FROM ledger_events WHERE event_type = 'ENTITY_CREATED'),
        last_change AS (
            SELECT entity_id, event_type, related_entity_id, seq FROM ledger_events
            WHERE event_type IN ('ENTITY_CREATED', 'ENTITY_MERGED', 'ENTITY_REACTIVATED',
                                 'ENTITY_PROMOTED')
            QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY seq DESC) = 1),
        touches AS (
            SELECT entity_id AS id, commit_id FROM ledger_events
            WHERE event_type IN ({_IDENTITY_EVENTS}) AND entity_id IS NOT NULL
            UNION
            SELECT related_entity_id, commit_id FROM ledger_events
            WHERE event_type IN ({_IDENTITY_EVENTS}) AND related_entity_id IS NOT NULL),
        versions AS (
            SELECT id AS entity_id, count(DISTINCT commit_id)::INTEGER AS version
            FROM touches GROUP BY id)
        SELECT c.entity_id, c.entity_type,
               CASE l.event_type WHEN 'ENTITY_CREATED' THEN c.state
                                 WHEN 'ENTITY_MERGED' THEN 'retired' ELSE 'active' END AS state,
               v.version, c.created_seq,
               CASE WHEN l.event_type = 'ENTITY_MERGED' THEN l.seq END AS retired_seq,
               CASE WHEN l.event_type = 'ENTITY_MERGED' THEN l.related_entity_id END
                   AS merged_into
        FROM created c JOIN last_change l USING (entity_id) JOIN versions v USING (entity_id)
        """,
        "entity_id, entity_type, state, version, created_seq, retired_seq, merged_into",
    ),
    "relationships": (
        """
        WITH created AS (
            SELECT payload->>'relationship_id' AS relationship_id,
                   payload->>'relation' AS relation, entity_id AS subject_entity_id,
                   related_entity_id AS object_entity_id, decision_id AS basis_decision_id,
                   seq AS valid_from_seq
            FROM ledger_events WHERE event_type = 'RELATIONSHIP_CREATED'),
        revoked AS (
            SELECT payload->>'relationship_id' AS relationship_id, seq AS valid_to_seq
            FROM ledger_events WHERE event_type = 'RELATIONSHIP_REVOKED')
        SELECT created.*, revoked.valid_to_seq FROM created LEFT JOIN revoked
            USING (relationship_id)
        """,
        "relationship_id, relation, subject_entity_id, object_entity_id, basis_decision_id, "
        "valid_from_seq, valid_to_seq",
    ),
    "pair_constraints": (
        """
        WITH added AS (
            SELECT payload->>'constraint_id' AS constraint_id, payload->>'kind' AS kind,
                   payload->>'left_id' AS left_id, payload->>'right_id' AS right_id,
                   decision_id AS basis_decision_id, seq AS valid_from_seq
            FROM ledger_events WHERE event_type = 'CONSTRAINT_ADDED'),
        revoked AS (
            SELECT payload->>'constraint_id' AS constraint_id, seq AS valid_to_seq
            FROM ledger_events WHERE event_type = 'CONSTRAINT_REVOKED')
        SELECT added.*, revoked.valid_to_seq FROM added LEFT JOIN revoked USING (constraint_id)
        """,
        "constraint_id, kind, left_id, right_id, basis_decision_id, valid_from_seq, valid_to_seq",
    ),
    "review_items": (
        """
        WITH opened AS (
            SELECT payload->>'review_id' AS review_id, payload->>'kind' AS kind,
                   decision_id, seq AS opened_seq
            FROM ledger_events WHERE event_type = 'REVIEW_OPENED'),
        closed AS (
            SELECT payload->>'review_id' AS review_id, payload->>'status' AS status,
                   seq AS closed_seq, decision_id AS resolution_decision_id
            FROM ledger_events WHERE event_type = 'REVIEW_CLOSED')
        SELECT o.review_id, o.kind, o.decision_id, coalesce(c.status, 'open') AS status,
               o.opened_seq, c.closed_seq, c.resolution_decision_id
        FROM opened o LEFT JOIN closed c USING (review_id)
        """,
        "review_id, kind, decision_id, status, opened_seq, closed_seq, resolution_decision_id",
    ),
}

CHECKS = {
    "ledger sequence is gapless from 1": """
        SELECT 'head ' || max(seq) || ', events ' || count(*) FROM ledger_events
        HAVING count(*) > 0 AND (min(seq) <> 1 OR max(seq) <> count(*))
    """,
    "each commit's events are contiguous": """
        SELECT commit_id FROM ledger_events GROUP BY commit_id
        HAVING max(seq) - min(seq) + 1 <> count(*)
    """,
    "at most one open membership per observation": """
        SELECT observation_id FROM memberships WHERE valid_to_seq IS NULL
        GROUP BY observation_id HAVING count(*) > 1
    """,
    "memberships belong to observations of the entity's type": """
        SELECT m.membership_id FROM memberships m
        JOIN observations o USING (observation_id) JOIN entities e USING (entity_id)
        WHERE o.entity_type <> e.entity_type
    """,
    "every membership, relationship and review cites a recorded decision": """
        SELECT x.id FROM (
            SELECT basis_decision_id AS id FROM memberships
            UNION ALL SELECT basis_decision_id FROM relationships
            UNION ALL SELECT decision_id FROM review_items
            UNION ALL SELECT resolution_decision_id FROM review_items
                WHERE resolution_decision_id IS NOT NULL
            UNION ALL SELECT basis_decision_id FROM pair_constraints) x
        LEFT JOIN decisions d ON d.decision_id = x.id WHERE d.decision_id IS NULL
    """,
    "retired entities hold no open memberships": """
        SELECT m.membership_id FROM memberships m JOIN entities e USING (entity_id)
        WHERE e.state = 'retired' AND m.valid_to_seq IS NULL
    """,
    "open memberships point at live entities": """
        SELECT m.membership_id FROM memberships m LEFT JOIN entities e USING (entity_id)
        WHERE m.valid_to_seq IS NULL AND (e.entity_id IS NULL OR e.state = 'retired')
    """,
    "live entities have a snapshot at their current version": """
        SELECT e.entity_id FROM entities e
        LEFT JOIN entity_snapshots s ON s.entity_id = e.entity_id AND s.version = e.version
        WHERE e.state <> 'retired' AND s.entity_id IS NULL
    """,
    "no snapshot is ahead of its entity": """
        SELECT s.entity_id FROM entity_snapshots s JOIN entities e USING (entity_id)
        WHERE s.version > e.version
    """,
    "a live entity's current snapshot lists its current members": """
        WITH current AS (
            SELECT entity_id, list_sort(list(observation_id)) AS members FROM memberships
            WHERE valid_to_seq IS NULL GROUP BY entity_id)
        SELECT e.entity_id FROM entities e
        JOIN entity_snapshots s ON s.entity_id = e.entity_id AND s.version = e.version
        LEFT JOIN current c ON c.entity_id = e.entity_id
        WHERE e.state <> 'retired'
          AND from_json(s.derived_from, '["VARCHAR"]') <> coalesce(c.members, [])
    """,
    "no relationship is identity": """
        SELECT relationship_id FROM relationships
        WHERE relation IN ('same_entity', 'same_as') OR subject_entity_id = object_entity_id
    """,
    "every cited component is registered": """
        SELECT x.ref FROM (
            SELECT canonicalizer_ref AS ref FROM entity_snapshots
            UNION SELECT profile_ref FROM runs
            UNION SELECT producer_ref FROM stages
            UNION SELECT method_ref FROM evidence
            UNION SELECT unnest(from_json(component_refs, '["VARCHAR"]')) FROM decisions) x
        LEFT JOIN components c ON c.component_ref = x.ref WHERE c.component_ref IS NULL
    """,
    "no live entity holds both sides of an open cannot-link": """
        SELECT c.constraint_id FROM pair_constraints c
        JOIN memberships ml ON ml.observation_id = c.left_id AND ml.valid_to_seq IS NULL
        JOIN memberships mr ON mr.observation_id = c.right_id AND mr.valid_to_seq IS NULL
        WHERE c.kind = 'cannot_link' AND c.valid_to_seq IS NULL AND ml.entity_id = mr.entity_id
    """,
    "constraints anchor on existing observations": """
        SELECT c.constraint_id FROM pair_constraints c
        LEFT JOIN observations l ON l.observation_id = c.left_id
        LEFT JOIN observations r ON r.observation_id = c.right_id
        WHERE l.observation_id IS NULL OR r.observation_id IS NULL
    """,
    "every cited model invocation is recorded": """
        SELECT x.id FROM (
            SELECT json_extract_string(body, '$.invocation_id') AS id FROM decisions
            UNION SELECT invocation_id FROM proposals
            UNION SELECT cached_from FROM model_invocations) x
        LEFT JOIN model_invocations i ON i.invocation_id = x.id
        WHERE x.id IS NOT NULL AND i.invocation_id IS NULL
    """,
    "redacted observations carry no attributes": """
        SELECT observation_id FROM observations
        WHERE (redacted_at IS NULL) = (attributes IS NULL)
    """,
}


@dataclass(frozen=True)
class Violation:
    check: str
    examples: tuple[str, ...]


def check(store: DuckDBStore, *, limit: int = 5) -> list[Violation]:
    con = store._con
    violations = []
    with store._lock:
        for table, (replay, columns) in REPLAY.items():
            for label, left, right in (
                (
                    "in the ledger but not the table",
                    f"({replay})",
                    f"SELECT {columns} FROM {table}",
                ),
                (
                    "in the table but not the ledger",
                    f"SELECT {columns} FROM {table}",
                    f"({replay})",
                ),
            ):
                rows = con.execute(
                    f"SELECT * FROM ({left} EXCEPT {right}) LIMIT {limit}"
                ).fetchall()
                if rows:
                    violations.append(
                        Violation(f"replay {table}: rows {label}", tuple(str(r) for r in rows))
                    )
        for name, sql in CHECKS.items():
            rows = con.execute(f"SELECT * FROM ({sql}) LIMIT {limit}").fetchall()
            if rows:
                violations.append(Violation(name, tuple(str(r[0]) for r in rows)))
    return violations
