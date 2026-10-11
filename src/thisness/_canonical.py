"""Canonical JSON and content digests.

Fingerprints and component config hashes must not depend on dict key order or
on how a value was spelled in Python. Sets are rejected because their order is
not defined.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError("naive datetimes are ambiguous; use timezone-aware datetimes")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        raise TypeError("sets have no defined order; use a sorted list")
    raise TypeError(f"{type(value).__name__} is not JSON-serializable")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_default,
    )


def digest(value: Any) -> str:
    """SHA-256 hex digest of the canonical JSON of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
