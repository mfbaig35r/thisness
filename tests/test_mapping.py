"""The mapping export (design 08 §6): every record, its entity and the entity's name,
now or as of any point in the ledger."""

import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from thisness.benchmark.evaluate import run_benchmark
from thisness.benchmark.organizations import generate
from thisness.cli import main
from thisness.storage.duckdb.invariants import check
from thisness.ui import create_app

COLUMNS = ["source_type", "source_id", "locator", "name", "observation_id", "entity_id",
           "entity_name", "status", "record_status", "since_seq", "decision_id"]  # fmt: skip


@pytest.fixture
def run():
    _, engine, truth = run_benchmark(generate(60, seed=3), random_pairs=20_000)
    yield engine, truth
    assert check(engine.store) == []


def test_one_row_per_record_with_its_current_entity(run):
    engine, truth = run
    table = engine.mapping("organization")
    assert table.column_names == COLUMNS
    rows = table.to_pylist()
    assert sorted(r["observation_id"] for r in rows) == sorted(truth)
    current = engine.store.entities_of(list(truth))
    names = engine.names(sorted(set(current.values())))
    for r in rows:
        assert r["entity_id"] == current.get(r["observation_id"])
        assert r["entity_name"] == names[r["entity_id"]]
        assert r["name"] == engine.observation(r["observation_id"]).attributes["name"]


def test_status_says_what_may_still_change(run):
    engine, _ = run
    rows = engine.mapping("organization").to_pylist()
    assert {r["status"] for r in rows} <= {"resolved", "in_review", "provisional", "unresolved"}
    held = {n for s in engine.reviews() for n in s.review.members}
    for r in rows:
        under_review = r["observation_id"] in held or r["entity_id"] in held
        assert (r["status"] == "in_review") == under_review
        if r["status"] == "provisional":
            assert engine.get_entity(r["entity_id"])[0].state == "provisional"
        if r["status"] == "resolved":
            assert engine.get_entity(r["entity_id"])[0].state == "active"


def test_as_of_a_point_before_resolution(run):
    engine, truth = run
    ingested = max(e.seq for o in truth for e in engine.history(observation_id=o)
                   if e.event_type.value == "OBSERVATION_INGESTED")  # fmt: skip
    rows = engine.mapping("organization", as_of_seq=ingested).to_pylist()
    assert len(rows) == len(truth)
    assert {r["status"] for r in rows} == {"unresolved"}
    assert all(r["entity_id"] is None for r in rows)
    assert engine.mapping("organization", as_of_seq=0).num_rows == 0


def test_a_merge_changes_the_mapping_from_its_seq_on(run):
    engine, _ = run
    resolved = [r for r in engine.mapping("organization").to_pylist() if r["status"] == "resolved"]
    a, b = resolved[0], next(r for r in resolved if r["entity_id"] != resolved[0]["entity_id"])
    before = engine.overview()["head_seq"]
    engine.merge_entities(a["entity_id"], [b["entity_id"]], reason="test")
    now = {r["observation_id"]: r for r in engine.mapping("organization").to_pylist()}
    then = {r["observation_id"]: r for r in
            engine.mapping("organization", as_of_seq=before).to_pylist()}  # fmt: skip
    assert now[b["observation_id"]]["entity_id"] == a["entity_id"]
    assert then[b["observation_id"]]["entity_id"] == b["entity_id"]
    assert then[b["observation_id"]]["entity_name"] == b["entity_name"]
    assert now[b["observation_id"]]["since_seq"] > before


def test_other_types_are_empty(run):
    engine, _ = run
    assert engine.mapping("person").num_rows == 0


def test_export_command(tmp_path):
    store = tmp_path / "s.duckdb"
    assert main(["--store", str(store), "demo", "--size", "20"]) == 0
    assert (
        main(["--store", str(store), "export", "mapping", "--output", str(tmp_path / "m.csv")]) == 0
    )
    table = pacsv.read_csv(tmp_path / "m.csv")
    assert table.column_names == COLUMNS and table.num_rows > 20
    out = tmp_path / "m.parquet"
    assert (
        main(
            [
                "--store",
                str(store),
                "export",
                "mapping",
                "--format",
                "parquet",
                "--output",
                str(out),
            ]
        )
        == 0
    )
    assert pq.read_table(out).num_rows == table.num_rows
    assert main(["--store", str(store), "export", "mapping", "--format", "parquet"]) == 1


def test_workbench_download(run):
    engine, truth = run
    client = TestClient(create_app(engine, store_name="bench.duckdb"))
    response = client.get("/export/mapping/organization.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "organization-mapping.csv" in response.headers["content-disposition"]
    assert response.text.count("\n") == len(truth) + 1
    assert client.get("/export/mapping/vessel.csv").status_code == 404
    assert 'href="/export/mapping/organization.csv"' in client.get("/entities/organization").text
