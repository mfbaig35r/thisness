"""Check for docs/design/02-core-model-and-storage.md.

Not Thisness code. Loads 02_schema.sql into an in-memory DuckDB and plays
identity operations through a minimal commit() that writes ledger events and
materialized state in one transaction. Checks:

- point-in-time membership ("which entity held obs X at ledger seq S")
- resolution of retired ids, derived from memberships (not a stored redirect)
- unmerge by reactivating the retired entity, so its id stays meaningful
- partial split leaves a retired id ambiguous, and says so
- optimistic version check on merge
- replaying the ledger alone reproduces materialized memberships and entities
- membership invariants
- blocking self-join with a block-size cap

    uv run --with duckdb python docs/design/sketches/02_schema_check.py
"""

from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime
from itertools import count

import duckdb

SCHEMA = pathlib.Path(__file__).with_name("02_schema.sql").read_text()
NOW = datetime(2026, 10, 7, tzinfo=UTC)
_ids = count(1)


def new_id(prefix: str) -> str:
    return f"{prefix}_{next(_ids):04d}"  # stands in for prefixed ULIDs


class StaleVersion(Exception):
    pass


# The commit in progress: its id, and the entities its events touched.
_commit: dict = {"id": None, "touched": set()}


# ---------------------------------------------------------------------------
# Minimal commit: ledger event + state change in one transaction
# ---------------------------------------------------------------------------


def event(
    db,
    event_type,
    *,
    actor=("system", "sketch"),
    observation_id=None,
    entity_id=None,
    related_entity_id=None,
    decision_id=None,
    payload=None,
) -> int:
    if entity_id:
        _commit["touched"].add(entity_id)
    return db.execute(
        """INSERT INTO ledger_events (event_id, commit_id, event_type, actor_kind, actor_id,
               observation_id, entity_id, related_entity_id, decision_id, payload, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING seq""",
        [
            new_id("evt"),
            _commit["id"],
            event_type,
            actor[0],
            actor[1],
            observation_id,
            entity_id,
            related_entity_id,
            decision_id,
            json.dumps(payload or {}),
            NOW,
        ],
    ).fetchone()[0]


def decision(db, decision_type, outcome, *, actor=("system", "sketch"), reason=None) -> str:
    did = new_id("dec")
    db.execute(
        """INSERT INTO decisions (decision_id, decision_type, outcome, actor_kind, actor_id,
               reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [did, decision_type, outcome, actor[0], actor[1], reason, NOW],
    )
    return did


def create_entity(db, entity_id, state, decision_id):
    seq = event(
        db,
        "ENTITY_CREATED",
        entity_id=entity_id,
        decision_id=decision_id,
        payload={"entity_type": "organization", "state": state},
    )
    db.execute(
        "INSERT INTO entities VALUES (?, 'organization', ?, 0, ?, NULL, NULL)",
        [entity_id, state, seq],
    )


def accept(db, observation_id, entity_id, decision_id):
    mid = new_id("mem")
    seq = event(
        db,
        "MEMBERSHIP_ACCEPTED",
        observation_id=observation_id,
        entity_id=entity_id,
        decision_id=decision_id,
        payload={"membership_id": mid},
    )
    db.execute(
        "INSERT INTO memberships VALUES (?, ?, ?, ?, ?, NULL)",
        [mid, observation_id, entity_id, decision_id, seq],
    )


def end_membership(db, observation_id, decision_id):
    mid, entity_id = db.execute(
        "SELECT membership_id, entity_id FROM memberships "
        "WHERE observation_id = ? AND valid_to_seq IS NULL",
        [observation_id],
    ).fetchone()
    seq = event(
        db,
        "MEMBERSHIP_ENDED",
        observation_id=observation_id,
        entity_id=entity_id,
        decision_id=decision_id,
        payload={"membership_id": mid},
    )
    db.execute("UPDATE memberships SET valid_to_seq = ? WHERE membership_id = ?", [seq, mid])


def move(db, observation_id, entity_id, decision_id):
    end_membership(db, observation_id, decision_id)
    accept(db, observation_id, entity_id, decision_id)


def members(db, entity_id) -> list[str]:
    return [
        r[0]
        for r in db.execute(
            "SELECT observation_id FROM memberships WHERE entity_id = ? AND valid_to_seq IS NULL "
            "ORDER BY observation_id",
            [entity_id],
        ).fetchall()
    ]


def version(db, entity_id) -> int:
    return db.execute("SELECT version FROM entities WHERE entity_id = ?", [entity_id]).fetchone()[0]


def merge(db, survivor, loser, decision_id, *, expected=None):
    """Move memberships to the survivor, retire the loser, record its final members."""
    if expected and (version(db, survivor), version(db, loser)) != expected:
        raise StaleVersion(
            f"expected {expected}, found {(version(db, survivor), version(db, loser))}"
        )
    final_members = members(db, loser)
    for obs in final_members:
        move(db, obs, survivor, decision_id)
    seq = event(
        db,
        "ENTITY_MERGED",
        entity_id=loser,
        related_entity_id=survivor,
        decision_id=decision_id,
        payload={"members_at_retirement": final_members},
    )
    db.execute(
        "UPDATE entities SET state = 'retired', retired_seq = ?, merged_into = ? "
        "WHERE entity_id = ?",
        [seq, survivor, loser],
    )


def split(db, source, moves: dict[str, list[str]], decision_id):
    """Move observations out of `source`. A target that is a retired entity is reactivated
    (an unmerge); any other target is created."""
    event(
        db,
        "ENTITY_SPLIT",
        entity_id=source,
        decision_id=decision_id,
        payload={"targets": sorted(moves)},
    )
    for target, observations in sorted(moves.items()):
        row = db.execute("SELECT state FROM entities WHERE entity_id = ?", [target]).fetchone()
        if row is None:
            create_entity(db, target, "active", decision_id)
        elif row[0] == "retired":
            event(db, "ENTITY_REACTIVATED", entity_id=target, decision_id=decision_id)
            db.execute(
                "UPDATE entities SET state = 'active', retired_seq = NULL, "
                "merged_into = NULL WHERE entity_id = ?",
                [target],
            )
        for obs in observations:
            move(db, obs, target, decision_id)


def commit(db, fn, *args, **kwargs) -> int:
    _commit["id"], _commit["touched"] = new_id("cmt"), set()
    db.execute("BEGIN TRANSACTION")
    try:
        fn(db, *args, **kwargs)
        # One version bump per entity per commit, so snapshot versions never skip.
        for entity_id in sorted(_commit["touched"]):
            db.execute("UPDATE entities SET version = version + 1 WHERE entity_id = ?", [entity_id])
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return db.execute("SELECT max(seq) FROM ledger_events").fetchone()[0]


# ---------------------------------------------------------------------------
# Queries the design depends on
# ---------------------------------------------------------------------------


def entity_of(db, observation_id, as_of_seq=None):
    if as_of_seq is None:
        sql = "SELECT entity_id FROM memberships WHERE observation_id = ? AND valid_to_seq IS NULL"
        args = [observation_id]
    else:
        sql = (
            "SELECT entity_id FROM memberships WHERE observation_id = ? "
            "AND valid_from_seq <= ? AND (valid_to_seq IS NULL OR valid_to_seq > ?)"
        )
        args = [observation_id, as_of_seq, as_of_seq]
    row = db.execute(sql, args).fetchone()
    return row[0] if row else None


RESOLVE = """
    -- A live id resolves to itself. A retired id resolves to wherever its final
    -- members live now: one entity, or several (ambiguous after a partial split).
    WITH target AS (SELECT entity_id, state FROM entities WHERE entity_id = $id),
    last_merge AS (
        SELECT payload FROM ledger_events
        WHERE event_type = 'ENTITY_MERGED' AND entity_id = $id
        ORDER BY seq DESC LIMIT 1),
    final_members AS (
        SELECT unnest(from_json(payload->'members_at_retirement', '["VARCHAR"]')) AS observation_id
        FROM last_merge)
    SELECT entity_id FROM target WHERE state <> 'retired'
    UNION
    SELECT m.entity_id FROM final_members f
    JOIN memberships m ON m.observation_id = f.observation_id AND m.valid_to_seq IS NULL
    WHERE (SELECT state FROM target) = 'retired'
    ORDER BY entity_id
"""


def resolve(db, entity_id) -> list[str]:
    return [r[0] for r in db.execute(RESOLVE, {"id": entity_id}).fetchall()]


REPLAY_MEMBERSHIPS = """
    WITH acc AS (
        SELECT payload->>'membership_id' AS membership_id, observation_id, entity_id,
               decision_id AS basis_decision_id, seq AS valid_from_seq
        FROM ledger_events WHERE event_type = 'MEMBERSHIP_ACCEPTED'),
    ended AS (
        SELECT payload->>'membership_id' AS membership_id, seq AS valid_to_seq
        FROM ledger_events WHERE event_type = 'MEMBERSHIP_ENDED')
    SELECT acc.*, ended.valid_to_seq FROM acc LEFT JOIN ended USING (membership_id)
"""

# Entity state is the last state-changing event per entity (merges and unmerges can repeat).
REPLAY_ENTITIES = """
    WITH created AS (
        SELECT entity_id, payload->>'entity_type' AS entity_type, payload->>'state' AS state,
               seq AS created_seq
        FROM ledger_events WHERE event_type = 'ENTITY_CREATED'),
    last_change AS (
        SELECT entity_id, event_type, related_entity_id, seq FROM ledger_events
        WHERE event_type IN ('ENTITY_CREATED', 'ENTITY_MERGED', 'ENTITY_REACTIVATED',
                             'ENTITY_PROMOTED')
        QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY seq DESC) = 1),
    versions AS (
        SELECT entity_id, count(DISTINCT commit_id) AS version FROM ledger_events
        WHERE entity_id IS NOT NULL GROUP BY entity_id)
    SELECT c.entity_id, c.entity_type,
           CASE l.event_type WHEN 'ENTITY_CREATED' THEN c.state
                             WHEN 'ENTITY_MERGED' THEN 'retired' ELSE 'active' END AS state,
           v.version,
           c.created_seq,
           CASE WHEN l.event_type = 'ENTITY_MERGED' THEN l.seq END AS retired_seq,
           CASE WHEN l.event_type = 'ENTITY_MERGED' THEN l.related_entity_id END AS merged_into
    FROM created c JOIN last_change l USING (entity_id) LEFT JOIN versions v USING (entity_id)
"""

INVARIANTS = {
    "at most one open membership per observation": "SELECT observation_id FROM memberships WHERE valid_to_seq IS NULL "
    "GROUP BY observation_id HAVING count(*) > 1",
    "every membership cites an existing decision": "SELECT m.membership_id FROM memberships m "
    "LEFT JOIN decisions d ON d.decision_id = m.basis_decision_id WHERE d.decision_id IS NULL",
    "retired entities hold no open memberships": "SELECT m.membership_id FROM memberships m JOIN entities e USING (entity_id) "
    "WHERE e.state = 'retired' AND m.valid_to_seq IS NULL",
    "open memberships point at live entities": "SELECT m.membership_id FROM memberships m LEFT JOIN entities e USING (entity_id) "
    "WHERE m.valid_to_seq IS NULL AND (e.entity_id IS NULL OR e.state = 'retired')",
}

BLOCKING = """
    WITH scoped AS (
        SELECT f.* FROM observation_features f
        JOIN run_scope s ON s.observation_id = f.observation_id AND s.run_id = ?
        WHERE f.feature = ?),
    blocks AS (
        SELECT value FROM scoped GROUP BY value HAVING count(*) BETWEEN 2 AND ?)
    SELECT DISTINCT a.observation_id AS left_id, b.observation_id AS right_id, a.value
    FROM scoped a JOIN scoped b ON a.value = b.value AND a.observation_id < b.observation_id
    WHERE a.value IN (SELECT value FROM blocks)
    ORDER BY left_id, right_id, a.value
"""

NAMES = {
    "o1": "IBM",
    "o2": "IBM Corp.",
    "o3": "International Business Machines",
    "o4": "I.B.M.",
    "o5": "Intl Business Machines Corp",
    "o6": "Acme Ltd",
}
KEYS = {
    "o1": "IBM",
    "o2": "IBM",
    "o3": "INTERNATIONALBUSINESSMACHINES",
    "o4": "IBM",
    "o5": "INTLBUSINESSMACHINES",
    "o6": "ACME",
}


def base(db):
    """Ingest, run 1, provisional merge, stale merge attempt, operator merge of B into A."""
    db.execute(SCHEMA)
    for o, name in NAMES.items():
        db.execute(
            "INSERT INTO observations (observation_id, entity_type, fingerprint, "
            "source_type, source_id, attributes, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [o, "organization", f"fp-{o}", "csv", "suppliers.csv", json.dumps({"name": name}), NOW],
        )
        db.execute(
            "INSERT INTO observation_features VALUES (?, 'name.key', 0, ?, NULL, ?)",
            [o, KEYS[o], "normalizer:org_name_key@1#abc"],
        )
        for i, tok in enumerate(sorted(set(name.upper().replace(".", "").split()))):
            db.execute(
                "INSERT INTO observation_features VALUES (?, 'name.token', ?, ?, NULL, ?)",
                [o, i, tok, "normalizer:org_name_tokens@1#abc"],
            )
    db.execute("INSERT INTO run_scope SELECT 'run_1', observation_id FROM observations")

    def run1(db):
        d = decision(db, "consensus", "CREATE_ENTITY")
        for ent, state, obs in [
            ("org_A", "active", ["o1", "o2"]),
            ("org_B", "active", ["o3", "o5"]),
            ("org_C", "provisional", ["o4"]),
        ]:
            create_entity(db, ent, state, d)
            for o in obs:
                accept(db, o, ent, d)

    seqs = {"run1": commit(db, run1)}
    seqs["provisional merge"] = commit(
        db, lambda db: merge(db, "org_A", "org_C", decision(db, "consensus", "MERGE_PROVISIONAL"))
    )

    before = db.execute("SELECT count(*) FROM ledger_events").fetchone()[0]
    try:
        commit(
            db,
            lambda db: merge(
                db,
                "org_A",
                "org_B",
                decision(db, "operator", "MERGE", actor=("human", "fb")),
                expected=(3, 2),
            ),
        )
        stale = "NOT rejected"
    except StaleVersion as e:
        stale = (
            f"rejected ({e}); ledger unchanged: "
            f"{db.execute('SELECT count(*) FROM ledger_events').fetchone()[0] == before}"
        )

    expected = (version(db, "org_A"), version(db, "org_B"))
    seqs["operator merge B->A"] = commit(
        db,
        lambda db: merge(
            db,
            "org_A",
            "org_B",
            decision(db, "operator", "MERGE", actor=("human", "fb")),
            expected=expected,
        ),
    )
    return seqs, stale


def check_replay_and_invariants(db):
    cols = {
        "memberships": "membership_id, observation_id, entity_id, basis_decision_id, "
        "valid_from_seq, valid_to_seq",
        "entities": "entity_id, entity_type, state, version, created_seq, retired_seq, merged_into",
    }
    for table, replay in (("memberships", REPLAY_MEMBERSHIPS), ("entities", REPLAY_ENTITIES)):
        a = db.execute(
            f"SELECT count(*) FROM (({replay}) EXCEPT (SELECT {cols[table]} FROM {table}))"
        ).fetchone()[0]
        b = db.execute(
            f"SELECT count(*) FROM ((SELECT {cols[table]} FROM {table}) EXCEPT ({replay}))"
        ).fetchone()[0]
        assert a == b == 0, f"replay mismatch on {table}: {a}, {b}"
    for name, sql in INVARIANTS.items():
        assert not db.execute(sql).fetchall(), name
    return "replay identical for memberships and entities; 4 invariants hold"


def show_state(db):
    for ent, state, ver in db.execute(
        "SELECT entity_id, state, version FROM entities ORDER BY entity_id"
    ).fetchall():
        detail = (
            f"members={members(db, ent)}"
            if state != "retired"
            else f"resolves to {resolve(db, ent)}"
        )
        print(f"  {ent} {state:<11} v{ver:<2} {detail}")


def main():
    # Branch 1: unmerge. Reviewer moves B's former members back into B.
    db = duckdb.connect(":memory:")
    seqs, stale = base(db)
    seqs["unmerge (reactivate B)"] = commit(
        db,
        lambda db: split(
            db,
            "org_A",
            {"org_B": ["o3", "o5"]},
            decision(db, "operator", "SPLIT", actor=("human", "fb"), reason="merge was wrong"),
        ),
    )

    print("=== Ledger position after each commit")
    for label, s in seqs.items():
        print(f"  {s:>3}  {label}")
    print(f"\n=== Optimistic version check\n  stale merge {stale}")

    print("\n=== Point-in-time membership (branch 1)")
    for obs in ("o3", "o4"):
        hist = ", ".join(f"@{s}->{entity_of(db, obs, s)}" for s in seqs.values())
        print(f"  {obs} {NAMES[obs]!r}: {hist}")

    print("\n=== Branch 1: unmerge reactivates org_B")
    show_state(db)
    print(f"  {check_replay_and_invariants(db)}")

    # Branch 2: partial split. Only o3 leaves A, into a new entity D.
    db2 = duckdb.connect(":memory:")
    base(db2)
    commit(
        db2,
        lambda db: split(
            db,
            "org_A",
            {"org_D": ["o3"]},
            decision(db, "operator", "SPLIT", actor=("human", "fb"), reason="different legal name"),
        ),
    )
    print("\n=== Branch 2: partial split leaves org_B ambiguous")
    show_state(db2)
    print(f"  {check_replay_and_invariants(db2)}")

    print("\n=== Blocking (run_1)")
    for feature, cap in (("name.key", 3), ("name.token", 2)):
        pairs = db.execute(BLOCKING, ["run_1", feature, cap]).fetchall()
        skipped = db.execute(
            "SELECT value, count(*) FROM observation_features WHERE feature = ? "
            "GROUP BY value HAVING count(*) > ? ORDER BY value",
            [feature, cap],
        ).fetchall()
        print(f"  {feature} (cap {cap}): {[(a, b, v) for a, b, v in pairs]}")
        print(f"      skipped oversize blocks: {skipped}")


if __name__ == "__main__":
    main()
