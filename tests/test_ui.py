from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from thisness.benchmark.evaluate import run_benchmark
from thisness.benchmark.organizations import generate
from thisness.storage.duckdb.invariants import check
from thisness.ui import create_app


@pytest.fixture
def engine():
    _, engine, _ = run_benchmark(generate(n_orgs=300, seed=11), random_pairs=60_000)
    yield engine
    assert check(engine.store) == []


@pytest.fixture
def client(engine):
    return TestClient(create_app(engine, store_name="bench.duckdb"), follow_redirects=False)


def biggest(engine) -> str:
    return engine.entities("organization", limit=1).rows[0].entity_id


def test_the_workbench_uses_only_the_public_api():
    # Invariant 13: the UI reads and writes through Thisness, never the store.
    source = (Path(__file__).parent.parent / "src/thisness/ui/app.py").read_text()
    assert not re.search(r"\.store\b", source)


def test_static_assets_are_bundled(client):
    for path in ("/static/theme.css", "/static/app.css", "/static/app.js",
                 "/static/vendor/htmx.min.js", "/static/vendor/Sortable.min.js",
                 "/static/vendor/inter-latin-wght-normal.woff2", "/static/icons/building.svg"):  # fmt: skip
        assert client.get(path).status_code == 200, path
    assert (
        "MIT"
        in (Path(__file__).parent.parent / "src/thisness/ui/static/THIRD_PARTY.md").read_text()
    )


def test_entity_pages_render(client, engine):
    home = client.get("/", follow_redirects=True)
    assert home.status_code == 200 and "Organizations" in home.text
    eid = biggest(engine)
    name = engine.dossier(eid).label
    assert name in client.get("/entities/organization").text
    filtered = client.get("/entities/organization", params={"q": name[:5].lower()})
    assert name in filtered.text
    assert "No organizations" in client.get("/entities/organization", params={"q": "zzzz"}).text
    page = client.get(f"/entity/{eid}")
    assert page.status_code == 200 and "Fields" in page.text and name in page.text
    for tab in ("values", "members", "relationships", "history", "context"):
        r = client.get(f"/entity/{eid}/tab/{tab}")
        assert r.status_code == 200, tab
    assert f"# {name}" in client.get(f"/entity/{eid}/tab/context", params={"budget": 1000}).text
    assert client.get(f"/panel/entity/{eid}").status_code == 200


def test_taxon_pages_say_which_authority_said_what(tmp_path, monkeypatch):
    import thisness.cli as cli
    from thisness.benchmark import taxa
    from thisness.engine import Thisness

    monkeypatch.setattr(cli, "_generator", lambda entity_type: lambda n, seed, **kw:
                        taxa.from_fixture(n, seed, **kw))  # fmt: skip
    store = str(tmp_path / "taxa.duckdb")
    assert cli.main(["--store", store, "demo", "--type", "taxon", "--size", "40",
                     "--reference"]) == 0  # fmt: skip
    taxa_engine = Thisness(store, profiles=[cli.PROFILES["taxon"]()])
    taxon_client = TestClient(create_app(taxa_engine))
    page = taxon_client.get("/entities/taxon")
    assert page.status_code == 200 and "Taxa" in page.text and "Taxons" not in page.text
    rows = taxa_engine.entities("taxon", limit=200).rows
    eid = next(r.entity_id for r in rows if taxa_engine.dossier(r.entity_id).authorities)
    html = taxon_client.get(f"/entity/{eid}").text
    assert "Authorities" in html and "WFO" in html and "reference" in html
    assert "Authorities" in taxon_client.get(f"/panel/entity/{eid}").text
    review = next(iter(taxa_engine.reviews()), None)
    if review is not None:
        page = taxon_client.get(f"/reviews/{review.review.review_id}")
        assert page.status_code == 200 and ("synonym of" in page.text or "accepted" in page.text)


def test_panels_explain_observations_and_decisions(client, engine):
    eid = biggest(engine)
    obs = engine.members(eid)[0]
    panel = client.get(f"/panel/observation/{obs}")
    assert panel.status_code == 200 and eid in panel.text
    decision = next(e.decision_id for e in engine.history(entity_id=eid) if e.decision_id)
    explained = client.get(f"/panel/decision/{decision}")
    assert explained.status_code == 200 and "decision" in explained.text.lower()


def test_unknown_records_are_404(client):
    assert client.get("/entity/org_00000000000000000000000000").status_code == 404


def test_reviews_render_and_can_be_answered(client, engine):
    queue = client.get("/reviews")
    assert queue.status_code == 200
    first, second = (s.review.review_id for s in engine.reviews()[:2])
    board = client.get(f"/reviews/{first}")
    assert board.status_code == 200 and "node-card" in board.text
    # No reason: refused, nothing changes.
    r = client.post(f"/reviews/{first}/resolve", data={"resolution": "same", "reason": " "},
                    headers={"HX-Request": "true"})  # fmt: skip
    assert "Give a reason" in r.text and engine.review_case(first).review.status == "open"
    # A partition that leaves members out: refused with the engine's message.
    r = client.post(f"/reviews/{first}/resolve", headers={"HX-Request": "true"},
                    data={"resolution": "groups", "reason": "x", "groups": json.dumps([[]])})  # fmt: skip
    assert "form-error" in r.text
    r = client.post(f"/reviews/{first}/resolve", headers={"HX-Request": "true"},
                    data={"resolution": "different", "reason": "two companies"})  # fmt: skip
    assert r.status_code == 204 and r.headers["HX-Redirect"] == f"/reviews/{second}"
    assert engine.review_case(first).review.status == "resolved"
    assert "This review is closed" in client.get(f"/reviews/{first}").text


def test_groups_from_the_board_are_saved(client, engine):
    summary = next(s for s in engine.reviews() if s.review.kind == "cluster")
    case = engine.review_case(summary.review.review_id)
    members = sorted(case.review.members)
    groups = [members[:1], members[1:]]
    r = client.post(f"/reviews/{summary.review.review_id}/resolve",
                    data={"resolution": "groups", "reason": "split", "groups": json.dumps(groups)})  # fmt: skip
    assert r.status_code == 303
    assert engine.entity_of(members[0]) != engine.entity_of(members[1])


def test_candidates_can_be_related_or_merged(client, engine):
    assert client.get("/relationships").status_code == 200
    first, second = engine.relationship_candidates()[:2]
    panel = client.get("/panel/candidate", params={"left": first.left_entity,
                                                   "right": first.right_entity})  # fmt: skip
    assert panel.status_code == 200 and "Relate" in panel.text
    r = client.post("/relationships/decide", data={
        "left": first.left_entity, "right": first.right_entity, "action": "relate",
        "relation": "sibling_of", "reason": "same group", "evidence": ",".join(first.evidence_ids)})  # fmt: skip
    assert r.status_code == 303
    assert [x.relation for x in engine.store.relationships(first.left_entity)] == ["sibling_of"]
    a, b = engine.members(second.left_entity)[0], engine.members(second.right_entity)[0]
    r = client.post("/relationships/decide", data={
        "left": second.left_entity, "right": second.right_entity, "action": "merge",
        "reason": "one company"})  # fmt: skip
    assert r.status_code == 303
    assert engine.entity_of(a) == engine.entity_of(b)


def test_store_pages_and_search(client, engine):
    assert "committed" in client.get("/runs").text
    model = client.get("/model/organization")
    assert model.status_code == 200 and "brand_only" in model.text and "capped" in model.text
    eid = biggest(engine)
    name = engine.dossier(eid).label
    assert name in client.get("/search", params={"q": name[:6]}).text
    obs = engine.members(eid)[0]
    assert "observation" in client.get("/search", params={"q": obs}).text
    assert "Nothing matches" in client.get("/search", params={"q": "zzzz"}).text


def test_people_render_in_the_workbench(tmp_path):
    from thisness.cli import main

    store = str(tmp_path / "people.duckdb")
    assert main(["--store", store, "demo", "--type", "person", "--size", "120"]) == 0
    from thisness.cli import _engine

    engine = _engine(store)
    client = TestClient(create_app(engine), follow_redirects=False)
    page = client.get("/entities/person")
    assert page.status_code == 200 and "People" in page.text
    eid = engine.entities("person", limit=1).rows[0].entity_id
    assert client.get(f"/entity/{eid}").status_code == 200
    review = engine.reviews()[0].review.review_id
    board = client.get(f"/reviews/{review}")
    assert board.status_code == 200 and "node-card" in board.text
    assert "Person model" in client.get("/model/person").text


def test_possible_relationships_come_a_page_at_a_time(client, engine, monkeypatch):
    """66,978 candidates in one page took 32 minutes to render (design 07 §13.4)."""
    import thisness.ui.app as app

    hoods = {c.neighborhood for c in engine.relationship_candidates()}
    assert len(hoods) > 1
    monkeypatch.setattr(app, "HOODS_PER_PAGE", 1)
    first = client.get("/relationships").text
    assert "Next" in first and "Previous" not in first and "showing 1 to 1" in first
    second = client.get("/relationships", params={"offset": 1}).text
    assert "Previous" in second
    names = engine.names([c.left_entity for c in engine.relationship_candidates()][:5])
    assert all(isinstance(v, str) and v for v in names.values())
