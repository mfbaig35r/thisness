"""A digest of every table in a store, for checking that a change leaves a run's output
exactly as it was (design 10 §3.1).

Evaluation runs are deterministic (seeded ids, a fixed clock), so two runs of the same
benchmark by code that should behave the same must give identical digests.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def store_digest(path: str | Path) -> dict[str, tuple[int, str]]:
    """Table -> (row count, hash of its rows in sorted order). Values are compared as
    text, so timestamps read the same whatever the Python timezone libraries."""
    import duckdb

    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' ORDER BY 1"
        ).fetchall()]  # fmt: skip
        out = {}
        for table in tables:
            columns = [r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ? ORDER BY ordinal_position",
                [table],
            ).fetchall()]  # fmt: skip
            select = ", ".join(f'CAST("{c}" AS VARCHAR)' for c in columns)
            digest = hashlib.sha256()
            count = 0
            for row in con.execute(f'SELECT {select} FROM "{table}" ORDER BY ALL').fetchall():
                digest.update(repr(row).encode())
                count += 1
            out[table] = (count, digest.hexdigest()[:16])
        return out
    finally:
        con.close()


def differences(a: str | Path, b: str | Path) -> list[str]:
    """The tables whose rows differ between two stores, with their row counts."""
    left, right = store_digest(a), store_digest(b)
    return [f"{t}: {left.get(t, (0, '-'))[0]} rows vs {right.get(t, (0, '-'))[0]}"
            for t in sorted(set(left) | set(right)) if left.get(t) != right.get(t)]  # fmt: skip
