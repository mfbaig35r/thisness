from __future__ import annotations

from types import SimpleNamespace

import pytest

from thisness.cli import main, read_csv
from thisness.profiles.organization import organization

CSV = """name,website,registration_ids,country,city,account_manager
Acme Logistics LLC,https://acme-logistics.com,duns:150483782; lei:junk,US,Phoenix,Dana
ACME LOGISTICS L.L.C.,https://www.acme-logistics.com/,duns:150483782,United States,Phoenix,
Brightwater Foods,,,,Leeds,Sam
,,,,,
"""


def test_csv_rows_map_to_attributes_and_context(tmp_path):
    path = tmp_path / "vendors.csv"
    path.write_text(CSV)
    rows = read_csv(path, organization())
    assert len(rows) == 3  # the blank row is skipped
    attributes, context = rows[0]
    assert attributes["registration_ids"] == ["duns:150483782", "lei:junk"]
    assert attributes["country"] == "US" and context == {"account_manager": "Dana"}
    assert rows[1][1] == {}


def test_ingest_resolve_and_inspect(tmp_path, capsys):
    path = tmp_path / "vendors.csv"
    path.write_text(CSV)
    store = str(tmp_path / "id.duckdb")
    assert main(["--store", store, "ingest", str(path), "--resolve"]) == 0
    out = capsys.readouterr().out
    assert "3 rows, 3 new observations" in out and "organization: 3 observations resolved" in out
    # Ingesting the same file again stores nothing new.
    assert main(["--store", store, "ingest", str(path)]) == 0
    assert "0 new observations, 3 already stored" in capsys.readouterr().out
    assert main(["--store", store, "check"]) == 0
    assert "all invariants hold" in capsys.readouterr().out

    from thisness.cli import _engine

    engine = _engine(store)
    eid = engine.entities("organization", query="acme").rows[0].entity_id
    engine.store.close()
    assert main(["--store", store, "dossier", eid, "--chars", "800"]) == 0
    assert "# Acme Logistics LLC" in capsys.readouterr().out
    assert main(["--store", store, "history", eid]) == 0
    assert "ENTITY_CREATED" in capsys.readouterr().out
    assert main(["--store", store, "reviews"]) == 0


def test_demo_builds_a_store_once(tmp_path, capsys):
    store = str(tmp_path / "demo.duckdb")
    assert main(["--store", store, "demo", "--orgs", "40"]) == 0
    assert "synthetic organization records" in capsys.readouterr().out
    assert main(["--store", store, "demo", "--orgs", "40"]) == 1
    assert "exists" in capsys.readouterr().err


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    for command in ("ingest", "resolve", "ui", "demo", "history", "dossier", "reviews", "check",
                    "eval"):  # fmt: skip
        assert command in out


def test_demo_and_eval_take_a_type(tmp_path, capsys):
    store = str(tmp_path / "people.duckdb")
    assert main(["--store", store, "demo", "--type", "person", "--size", "60"]) == 0
    out = capsys.readouterr().out
    assert "synthetic person records" in out and "person:" in out
    assert main(["--store", store, "check"]) == 0


def test_taxon_demo_with_the_reference_is_labelled_by_wfo(tmp_path, capsys, monkeypatch):
    import thisness.cli as cli
    from thisness.benchmark import taxa
    from thisness.engine import Thisness

    # The shipped fixture, not a download.
    monkeypatch.setattr(cli, "_generator", lambda entity_type: lambda n, seed, **kw:
                        taxa.from_fixture(n, seed, **kw))  # fmt: skip
    store = str(tmp_path / "taxa.duckdb")
    assert main(["--store", store, "demo", "--type", "person", "--reference"]) == 1
    assert main(["--store", store, "demo", "--type", "person", "--col"]) == 1
    assert main(["--store", store, "demo", "--type", "taxon", "--size", "40", "--reference"]) == 0
    assert "synthetic taxon records" in capsys.readouterr().out
    engine = Thisness(store, profiles=[cli.PROFILES["taxon"]()])
    rows = engine.entities("taxon", limit=500).rows
    snapshots = [engine.dossier(r.entity_id).snapshot for r in rows]
    assert any(s and s.attributes.get("accepted_according_to") == "wfo" for s in snapshots)
    assert main(["--store", store, "check"]) == 0


def test_eval_can_adjudicate_with_the_oracle(capsys):
    assert main(["eval", "--size", "120", "--adjudicator", "oracle", "--mode", "decide",
                 "--max-pairs", "50", "--concurrency", "1"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert '"mode": "decide"' in out and '"answered":' in out


def test_eval_replays_a_recording_into_a_saved_store(tmp_path, capsys):
    """A recorded run answered again from its cassette, free and offline, and kept in a
    store the Workbench can open."""
    import json as _json

    from thisness.engine import Thisness

    cassette, saved = tmp_path / "calls.jsonl", tmp_path / "run.duckdb"
    common = ["eval", "--size", "60", "--mode", "shadow", "--max-pairs", "30",
              "--concurrency", "1", "--model", "m"]  # fmt: skip
    assert main([*common, "--adjudicator", "oracle", "--record", str(cassette)]) == 0
    first = capsys.readouterr().out
    assert main([*common, "--adjudicator", "openai", "--replay", str(cassette),
                 "--save", str(saved)]) == 0  # fmt: skip
    replayed = capsys.readouterr().out
    assert f"saved to {saved}" in replayed

    def decisions(out: str) -> dict:
        return _json.loads(out.split("adjudication:", 1)[1].splitlines()[0])["decisions"]

    assert decisions(replayed) == decisions(first)
    engine = Thisness(str(saved), profiles=[organization()])
    calls = engine.invocations()
    assert calls and {c.provider for c in calls} == {"openai_compatible"}
    # Guards: one run per file, no overwriting, replay only of a real provider's recording.
    assert main([*common, "--adjudicator", "openai", "--replay", str(cassette),
                 "--save", str(saved)]) == 1  # fmt: skip
    assert main([*common, "--adjudicator", "oracle", "--replay", str(cassette)]) == 1
    assert main([*common, "--seeds", "1", "2", "--save", str(tmp_path / "x.duckdb")]) == 1


def test_resolve_gives_the_model_each_profiles_role(tmp_path, capsys, monkeypatch):
    """With a model configured, organizations let it decide contested pairs and people
    have it suggest answers to their reviews (design 06 §10.11)."""
    import json as _json

    import thisness.cli as cli
    from thisness.benchmark import organizations, people
    from thisness.engine import Thisness
    from thisness.llm.providers import FunctionProvider
    from thisness.profiles.person import person

    def answer(request):
        payload = _json.loads(request.messages[0].content)
        if "pairs" in payload:
            return {
                "answers": [{"pair": p["pair"], "decision": "unsure"} for p in payload["pairs"]]
            }
        return {"resolution": "unsure", "rationale": "a fake model"}

    monkeypatch.setattr(cli, "_provider", lambda args: FunctionProvider(answer, name="fake"))
    store = str(tmp_path / "both.duckdb")
    engine = Thisness(store, profiles=[organization(), person()])
    for bench in (organizations.generate(120, seed=3), people.generate(150, seed=3)):
        engine.ingest([engine.observe(bench.entity_type, r.attributes,
                                      {"type": "csv", "id": r.record_id}) for r in bench.records])  # fmt: skip
    engine.store.close()
    assert main(["--store", store, "resolve", "--provider", "openai", "--model", "m"]) == 0
    out = capsys.readouterr().out
    assert "model (decide)" in out and "model (suggest)" in out
    assert cli._model_role(SimpleNamespace(model_role="auto"), organization()) == "decide"
    assert cli._model_role(SimpleNamespace(model_role="shadow"), organization()) == "shadow"
    assert cli._model_role(SimpleNamespace(model_role="auto"), person()) == "suggest"


def test_the_uk_source_is_for_organizations():
    assert main(["eval", "--source", "uk", "--type", "person", "--size", "10"]) == 1


def test_eval_scores_the_review_assistant_against_the_truth(capsys, monkeypatch):
    import json as _json

    import thisness.benchmark.edgar as edgar

    monkeypatch.setattr(edgar, "standard", lambda n, seed, mode: edgar.from_fixture(n, seed,
                                                                                mode=mode))  # fmt: skip
    assert main(["eval", "--type", "person", "--source", "edgar", "--size", "150",
                 "--assistant", "oracle", "--concurrency", "1"]) == 0  # fmt: skip
    line = next(x for x in capsys.readouterr().out.splitlines() if "assistant:" in x)
    scored = _json.loads(line.split("assistant:", 1)[1])
    assert scored["reviews"] > 0 and scored["accuracy"] == 1.0
    assert scored["would_merge_different"] == scored["would_leave_apart"] == 0
