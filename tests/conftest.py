from __future__ import annotations

from datetime import UTC, datetime

import pytest

from thisness.ids import IdFactory
from thisness.storage.duckdb import DuckDBStore
from thisness.storage.duckdb.invariants import check

from .storage_helpers import World


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    """The model client's retry backoff sleeps for seconds; tests run it at once."""
    monkeypatch.setattr("thisness.llm.client._sleep", lambda seconds: None)


@pytest.fixture
def ids() -> IdFactory:
    return IdFactory.seeded(7)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(ids, now):
    """A fresh in-memory store. Every test that uses it ends with the full invariant
    suite: a test that leaves the store inconsistent fails here."""
    s = DuckDBStore(ids=ids, clock=lambda: now)
    yield s
    violations = check(s)
    s.close()
    assert violations == [], violations


@pytest.fixture
def world(store, ids, now) -> World:
    return World(store, ids, now)
