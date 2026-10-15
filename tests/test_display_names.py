"""Display names (design 08 §5.2): suggested by a model, accepted by a person as a
reversible commit, kept through later membership changes."""

import json

import pytest
from fastapi.testclient import TestClient

from thisness import Thisness
from thisness.cli import main
from thisness.display_names import LabelAssistant, needs_name
from thisness.llm import LLMClient, LLMPolicy
from thisness.llm.providers.function import FunctionProvider
from thisness.profiles.organization import organization
from thisness.storage.duckdb.invariants import check
from thisness.storage.errors import ThisnessError
from thisness.ui import create_app

COMPANIES = {
    "HALEON UK FINANCE LIMITED": 3,
    "BARCLAYS BANK PLC": 2,
    "THE GYM GROUP PLC": 2,
    "Hiscox Insurance Company Limited": 2,  # already readable: nothing to suggest
}


@pytest.fixture
def engine():
    engine = Thisness(":memory:", profiles=[organization()])
    engine.ingest([engine.observe("organization", {"name": name}, {"type": "t", "id": f"{name}{i}"})
                   for name, n in COMPANIES.items() for i in range(n)])  # fmt: skip
    engine.resolve_many("organization")
    yield engine
    assert check(engine.store) == []


def entity_named(engine, name):
    return next(e for e, n in engine.names(
        [r.entity_id for r in engine.entities("organization").rows]).items() if n == name)  # fmt: skip


def titled(bad: str | None = None):
    """A model that title-cases the first spelling, and renames ``bad`` to something else."""

    def answer(request):
        payload = json.loads(request.messages[0].content)
        out = []
        for item in payload["items"]:
            first = item["spellings"][0]
            name = "Some Other Company Ltd" if first == bad else first.title().replace("Plc", "PLC")
            out.append({"ref": item["ref"], "name": name})
        return {"names": out}

    return FunctionProvider(answer, name="oracle")


def assistant(engine, provider):
    client = LLMClient(engine.store, provider, ids=engine.ids, clock=engine.clock,
                       policy=LLMPolicy(concurrency=2))  # fmt: skip
    return LabelAssistant(client, model="oracle")


def test_needs_name():
    assert needs_name("HALEON UK FINANCE LIMITED") and needs_name("acme widgets ltd")
    assert (
        not needs_name("Haleon UK Finance Limited")
        and not needs_name("IBM")
        and not needs_name(None)
    )


def test_a_label_is_a_commit_and_can_be_cleared(engine):
    haleon = entity_named(engine, "HALEON UK FINANCE LIMITED")
    version = engine.get_entity(haleon)[0].version
    engine.label(haleon, "Haleon UK Finance Limited", reason="readable")
    assert engine.names([haleon])[haleon] == "Haleon UK Finance Limited"
    assert engine.display_labels([haleon]) == {haleon: "Haleon UK Finance Limited"}
    assert engine.get_entity(haleon)[0].version == version + 1
    engine.clear_label(haleon, reason="undo")
    assert engine.names([haleon])[haleon] == "HALEON UK FINANCE LIMITED"
    assert engine.display_labels([haleon]) == {}
    kinds = [e.event_type.value for e in engine.history(entity_id=haleon)]
    assert kinds[-2:] == ["ENTITY_LABELED", "ENTITY_LABEL_CLEARED"]


def test_a_label_survives_membership_changes_and_stays_with_its_entity(engine):
    haleon = entity_named(engine, "HALEON UK FINANCE LIMITED")
    gym = entity_named(engine, "THE GYM GROUP PLC")
    engine.label(haleon, "Haleon UK Finance Limited", reason="readable")
    engine.label(gym, "The Gym Group PLC", reason="readable")
    engine.merge_entities(haleon, [gym], reason="test")  # survivor keeps its own label
    assert engine.names([haleon])[haleon] == "Haleon UK Finance Limited"
    part = engine.members(haleon)[:1]
    engine.split_entity(haleon, part, reason="test")
    assert engine.names([haleon])[haleon] == "Haleon UK Finance Limited"
    split_off = engine.entity_of(part[0])
    assert split_off != haleon and engine.display_labels([split_off]) == {}
    with pytest.raises(ThisnessError):
        engine.label(gym, "Gym", reason="retired")


def test_suggestions_change_nothing_until_accepted(engine):
    result = engine.suggest_labels("organization", assistant=assistant(engine, titled()))
    assert result == {"asked": 3, "suggested": 3, "rejected": 0, "unanswered": 0}
    haleon = entity_named(engine, "HALEON UK FINANCE LIMITED")
    suggestion = engine.label_suggestions([haleon])[haleon]
    assert suggestion.name == "Haleon Uk Finance Limited" and suggestion.invocation_id
    assert engine.names([haleon])[haleon] == "HALEON UK FINANCE LIMITED"
    engine.accept_label_suggestion(suggestion.decision_id)
    assert engine.names([haleon])[haleon] == "Haleon Uk Finance Limited"
    labeled = [
        e for e in engine.history(entity_id=haleon) if e.event_type.value == "ENTITY_LABELED"
    ]
    assert labeled[0].payload["suggestion_id"] == suggestion.decision_id
    # Asked once: entities with a suggestion or a name are not asked again.
    again = engine.suggest_labels("organization", assistant=assistant(engine, titled()))
    assert again["asked"] == 0


def test_a_suggestion_must_be_a_spelling_of_the_entity_s_names(engine):
    result = engine.suggest_labels(
        "organization", assistant=assistant(engine, titled("BARCLAYS BANK PLC"))
    )
    assert result["rejected"] == 1 and result["suggested"] == 2
    barclays = entity_named(engine, "BARCLAYS BANK PLC")
    assert barclays not in engine.label_suggestions([barclays])


def test_accepting_after_a_merge_names_the_survivor(engine):
    engine.suggest_labels("organization", assistant=assistant(engine, titled()))
    gym, haleon = (
        entity_named(engine, "THE GYM GROUP PLC"),
        entity_named(engine, "HALEON UK FINANCE LIMITED"),
    )
    suggestion = engine.label_suggestions([gym])[gym]
    engine.merge_entities(haleon, [gym], reason="test")
    engine.accept_label_suggestion(suggestion.decision_id)
    assert engine.names([haleon])[haleon] == "The Gym Group PLC"


def test_the_mapping_shows_the_name(engine):
    haleon = entity_named(engine, "HALEON UK FINANCE LIMITED")
    engine.label(haleon, "Haleon UK Finance Limited", reason="readable")
    rows = [r for r in engine.mapping("organization").to_pylist() if r["entity_id"] == haleon]
    assert {r["entity_name"] for r in rows} == {"Haleon UK Finance Limited"}


def test_labels_command(tmp_path, capsys):
    store = str(tmp_path / "s.duckdb")
    engine = Thisness(store, profiles=[organization()])
    engine.ingest([engine.observe("organization", {"name": n}, {"type": "t", "id": str(i)})
                   for i, n in enumerate(["BARCLAYS BANK PLC", "BARCLAYS BANK PLC"])])  # fmt: skip
    engine.resolve_many("organization")
    engine.suggest_labels("organization", assistant=assistant(engine, titled()))
    engine.store.close()
    assert main(["--store", store, "labels", "list"]) == 0
    assert "'BARCLAYS BANK PLC' -> 'Barclays Bank PLC'" in capsys.readouterr().out
    assert main(["--store", store, "labels", "accept"]) == 1
    assert main(["--store", store, "labels", "accept", "--all"]) == 0
    reopened = Thisness(store, profiles=[organization()])
    entity = reopened.entities("organization").rows[0].entity_id
    assert reopened.names([entity])[entity] == "Barclays Bank PLC"
    reopened.store.close()
    assert main(["--store", store, "labels", "clear", entity]) == 0
    reopened = Thisness(store, profiles=[organization()])
    assert reopened.names([entity])[entity] == "BARCLAYS BANK PLC"
    reopened.store.close()


def test_workbench_accept_and_clear(engine):
    engine.suggest_labels("organization", assistant=assistant(engine, titled()))
    haleon = entity_named(engine, "HALEON UK FINANCE LIMITED")
    suggestion = engine.label_suggestions([haleon])[haleon]
    client = TestClient(create_app(engine, store_name="t.duckdb"), follow_redirects=False)
    page = client.get(f"/entity/{haleon}").text
    assert "Suggested name" in page and suggestion.decision_id in page
    response = client.post(
        f"/entity/{haleon}/label", data={"action": "accept", "suggestion": suggestion.decision_id}
    )
    assert response.status_code in (302, 303)
    page = client.get(f"/entity/{haleon}").text
    assert "Name set by a person" in page and "Suggested name" not in page
    assert "Named “Haleon Uk Finance Limited”" in client.get(f"/entity/{haleon}/tab/history").text
    client.post(f"/entity/{haleon}/label", data={"action": "clear"})
    assert engine.names([haleon])[haleon] == "HALEON UK FINANCE LIMITED"
