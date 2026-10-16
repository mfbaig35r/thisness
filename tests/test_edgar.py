"""Real people from SEC EDGAR, step 1: staging and the truth (design 09)."""

from __future__ import annotations

import json

import duckdb
import pytest

from thisness.benchmark import edgar

ROLE = "STRUCT(issuer_cik VARCHAR, issuer_name VARCHAR, is_director BOOLEAN, is_officer BOOLEAN, is_ten_percent_owner BOOLEAN, is_other BOOLEAN, officer_title VARCHAR, first_date VARCHAR, last_date VARCHAR, source_forms VARCHAR[])"


def role(issuer: str, title: str | None = None) -> dict:
    return {"issuer_cik": issuer, "issuer_name": f"Issuer {issuer}", "is_director": title is None,
            "is_officer": title is not None, "is_ten_percent_owner": False, "is_other": False,
            "officer_title": title, "first_date": "2021-01-04", "last_date": "2024-05-01",
            "source_forms": ["4"]}  # fmt: skip


def address(city: str, state: str) -> str:
    return json.dumps([{"kind": "reporting_owner", "street1": "1 MAIN ST", "street2": None,
                        "city": city, "state_or_country": state, "zip_code": "10001"}])  # fmt: skip


# cik, current name, other spellings, former names, roles, (city, state), individual
PEOPLE = [
    ("01", "Smith John A", ["SMITH JOHN A"], [], [role("900", "CFO")], ("NEW YORK", "NY"), True),
    # One person, two CIKs: compatible names at one issuer.
    ("02", "Smith John", [], [], [role("900")], ("BOSTON", "MA"), True),
    # A namesake: conflicting initial.
    ("03", "Smith John B", [], [], [role("901")], ("NEW YORK", "NY"), True),
    # Father and son at one issuer: the suffix tells them apart.
    ("04", "Smith John A Jr", [], [], [role("900")], ("NEW YORK", "NY"), True),
    # Compatible, a city in common, nothing else: left out as ambiguous.
    ("05", "Jones Mary", [], [], [role("910")], ("DENVER", "CO"), True),
    ("06", "Jones Mary K", [], [], [role("911")], ("DENVER", "CO"), True),
    # Compatible, nothing in common: different people.
    ("07", "Brown Ann", [], [], [role("920")], ("RENO", "NV"), True),
    ("08", "Brown Ann C", [], [], [role("921")], ("TAMPA", "FL"), True),
    # A CIK reassigned to another person.
    (
        "09",
        "Counts Travis",
        ["MORICE ROSEMARY"],
        ["Morice Rosemary"],
        [role("930")],
        ("DALLAS", "TX"),
        True,
    ),
    # A name change that shares a word: kept.
    (
        "10",
        "Cross Maegan Adams",
        ["RUHLMAN MAEGAN ADAMS"],
        ["Ruhlman Maegan Adams"],
        [role("940")],
        ("AUSTIN", "TX"),
        True,
    ),
    # Entities the classifier let through.
    ("11", "Seven2 SAS", [], [], [role("950")], ("PARIS", "I0"), True),
    ("12", "RF 2018 GRAT", [], [], [role("951")], ("MIAMI", "FL"), True),
    # Not individual, or no Section 16 role: never staged.
    ("13", "Acme Holdings LLC", [], [], [role("960")], ("ERIE", "PA"), False),
    ("14", "Green Paul", [], [], [], ("ERIE", "PA"), True),
]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    root = tmp_path_factory.mktemp("edgar")
    store = root / "persons.duckdb"
    con = duckdb.connect(str(store))
    con.execute(f"""CREATE TABLE persons (person_cik VARCHAR, canonical_name VARCHAR,
        former_names VARCHAR, roles {ROLE}[], is_individual BOOLEAN,
        individual_confidence DOUBLE)""")
    con.execute("""CREATE TABLE person_source_records (source_dataset VARCHAR,
        person_cik VARCHAR, name VARCHAR, addresses VARCHAR)""")
    con.execute("CREATE TABLE pipeline_state (key VARCHAR, value VARCHAR, updated_at VARCHAR)")
    con.execute("INSERT INTO pipeline_state VALUES ('classifier_version', '2', '')")
    for cik, name, others, former, roles, (city, state), individual in PEOPLE:
        con.execute(f"INSERT INTO persons VALUES (?, ?, ?, ?::{ROLE}[], ?, ?)", [
            cik, name, json.dumps([{"name": f, "from": "2019-01-01", "to": "2020-01-01"}
                                   for f in former]),
            roles, individual, 0.95 if individual else 0.1])  # fmt: skip
        con.execute("INSERT INTO person_source_records VALUES ('section16', ?, ?, ?)",
                    [cik, name, address(city, state)])  # fmt: skip
        con.execute("INSERT INTO person_source_records VALUES ('submissions', ?, ?, NULL)",
                    [cik, name])  # fmt: skip
        for other in others:
            con.execute("INSERT INTO person_source_records VALUES ('cik_lookup', ?, ?, NULL)",
                        [cik, other])  # fmt: skip
    con.close()
    return edgar.stage(store, root / "edgar.duckdb")


def test_staging_keeps_individuals_with_roles_and_no_street(db):
    con = duckdb.connect(str(db), read_only=True)
    ciks = {r[0] for r in con.execute("SELECT cik FROM people").fetchall()}
    assert ciks == {p[0] for p in PEOPLE} - {"13", "14"}
    columns = {r[0] for t in ("people", "names", "roles", "places", "former_names")
               for r in con.execute(f"DESCRIBE {t}").fetchall()}  # fmt: skip
    assert not columns & {"street1", "street2", "zip_code", "addresses"}
    assert con.execute("SELECT city, state FROM places WHERE cik = '01'").fetchall() == [
        ("NEW YORK", "NY")
    ]
    assert con.execute("SELECT value FROM meta WHERE key = 'classifier_version'").fetchone() == (
        "2",
    )


def test_spellings_ignore_case_and_slash_markers():
    assert edgar.spelling("SMITH JOHN A.") == edgar.spelling("Smith John A")
    assert edgar.name_key("MCDONALD JOHN D/MO") == edgar.name_key("McDonald John D")
    assert edgar.name_key("Smith John Jr") == edgar.name_key("John Smith")


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Smith John A", "Smith John", True),
        ("Smith John A", "Smith John B", False),
        ("Smith John", "Smith John Jr", False),
        ("Rollins Randall R", "Rollins R Randall", True),
    ],
)
def test_compatible_names(a, b, same):
    assert edgar.compatible(a, b) is same


def test_truth_merges_excludes_and_keeps_namesakes(db):
    t = edgar.truth(db)
    assert t.person_of["02"] == t.person_of["01"] == "01"
    assert len({t.person_of[c] for c in ("01", "03", "04", "07", "08")}) == 5
    assert t.excluded == {
        "05": "ambiguous_namesake",
        "06": "ambiguous_namesake",
        "09": "unrelated_names",
        "11": "not_a_person",
        "12": "not_a_person",
    }
    assert "10" in t.person_of


def test_truth_report_counts(db):
    report = edgar.truth_report(db)
    assert report["ciks"] == 12
    assert report["shared_name_pairs"]["issuer"] == 1
    assert report["shared_name_pairs"]["city"] == 1
    # Brown Ann and Brown Ann C; Smith John and Smith John B.
    assert report["shared_name_pairs"]["neither"] == 2
    assert report["ciks_kept"] == 7 and report["people"] == 6
    assert report["with_former_names"] == 2


def test_audit_samples_each_doubt(db):
    audit = edgar.audit(db, n=5)
    assert [d["cik"] for d in audit["unrelated_names"]] == ["09"]
    assert sorted(audit["not_a_person"]) == ["RF 2018 GRAT", "Seven2 SAS"]
    assert {(p["a"]["cik"], p["b"]["cik"]) for p in audit["pairs_issuer"]} == {("01", "02")}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("SMITH JOHN A JR", "John A Smith JR"),
        ("DAY II ROWLAND W", "Rowland W Day II"),
        ("Maridakis Helen V", "Helen V Maridakis"),  # a lone V is an initial
        ("MCDONALD JOHN D/MO", "John D Mcdonald"),
    ],
)
def test_other_systems_write_names_given_first(name, expected):
    assert edgar.given_first(name) == expected


def test_the_benchmark_follows_the_truth(db):
    bench = edgar.generate(db, n=20, seed=3)
    kept = set(edgar.truth(db).person_of.values())
    assert set(bench.people) == kept
    assert bench.people["01"]["ciks"] == ["01", "02"]  # one person, two CIKs
    by_person: dict[str, list] = {}
    for r in bench.records:
        by_person.setdefault(r.person_id, []).append(r)
    for records in by_person.values():
        sources = [r.source for r in records]
        assert 1 <= len(sources) - sources.count("edgar") <= 3 and "edgar" in sources
        for r in records:
            assert (r.attributes.get("name_order") == "family_first") == (r.source == "edgar")
            assert "identifiers" not in r.attributes
    # Every EDGAR spelling and former name is a record; a renamed person's former name too.
    names = {edgar.spelling(r.attributes["name"]) for r in by_person["10"]}
    assert {"CROSSMAEGANADAMS", "RUHLMANMAEGANADAMS"} <= names
    assert bench.kind["10"] == "renamed" and bench.kind["04"] == "namesake"
    assert {r.record_id for r in bench.records} == set(bench.truth)


def test_partial_identifiers_are_ciks_on_edgar_records(db):
    bench = edgar.generate(db, n=20, seed=3, mode="partial")
    for r in bench.records:
        if r.source == "edgar":
            assert r.attributes["identifiers"][0].startswith("CIK:")
        else:
            assert "identifiers" not in r.attributes
    with pytest.raises(ValueError):
        edgar.generate(db, n=20, mode="visible")


def test_the_eval_command_wants_people_for_edgar(capsys):
    from thisness.cli import main

    assert main(["eval", "--source", "edgar"]) == 1
    assert "--type person" in capsys.readouterr().err


def test_other_systems_vary_the_name_but_keep_it_given_first():
    import random

    rng = random.Random(5)
    names = [edgar._other_name("SHUFORD ROBERT F JR", rng) for _ in range(200)]
    assert all("," in n or n.upper().split()[-1] in ("SHUFORD", "JR") or "Shuford" not in n
               for n in names)  # fmt: skip
    assert any(n.startswith(("Bob", "Rob", "BOB", "ROB")) for n in names)  # nicknames
    assert any(n.endswith("Shuford") for n in names)  # the suffix dropped
    assert any(" F. " in n for n in names) and any(n == n.upper() for n in names)
    rng = random.Random(1)
    written = {edgar._employer_as_written("COMERICA INC /NEW/", rng) for _ in range(50)}
    assert "COMERICA INC /NEW/" not in written and {"COMERICA", "Comerica"} & written


@pytest.fixture(scope="module")
def shipped(tmp_path_factory):
    return edgar.fixture_db(tmp_path_factory.mktemp("edgar_fixture") / "fixture.duckdb")


def test_the_shipped_fixture_holds_the_hard_cases(shipped):
    report = edgar.truth_report(shipped)
    assert report["ciks"] >= 120 and not report["excluded"]
    assert report["shared_name_pairs"]["issuer"] >= 10  # one person, two CIKs
    assert report["shared_name_pairs"]["conflicting"] >= 20  # fathers and sons, namesakes
    assert report["with_former_names"] >= 20
    con = duckdb.connect(str(shipped), read_only=True)
    columns = {r[0] for t in ("people", "names", "roles", "places", "former_names")
               for r in con.execute(f"DESCRIBE {t}").fetchall()}  # fmt: skip
    assert not columns & {"street1", "street2", "zip_code", "addresses"}


def test_the_fixture_benchmark_resolves():
    from thisness.benchmark.evaluate import run_benchmark

    bench = edgar.from_fixture()
    assert {"edgar", "crm", "board", "screening"} == {r.source for r in bench.records}
    assert {"namesake", "renamed"} <= set(bench.kind.values())
    report, _, _ = run_benchmark(bench)
    assert report.clusters.b3_precision >= 0.98
    assert report.candidates["pair_recall"] >= 0.9


def test_suggestions_are_scored_as_if_accepted():
    from types import SimpleNamespace as NS

    from thisness.benchmark.evaluate import recommendation_accuracy

    truth = {"a1": "A", "a2": "A", "b1": "B", "c1": "C"}

    def rec(resolution, groups):
        return NS(status="ok", recommendation=NS(resolution=resolution, groups=groups))

    scored = recommendation_accuracy(None, truth, [
        rec("same", (("a1", "a2"),)),  # right
        rec("same", (("a1", "b1"),)),  # merges different people
        rec("different", (("a1",), ("a2",))),  # leaves a match apart
        rec("groups", (("a1", "a2"), ("b1", "c1"))),  # both right and wrong: wrong
        rec("unsure", None),
        NS(status="failed", recommendation=None),
    ])  # fmt: skip
    assert scored["decided"] == 4 and scored["right"] == 1 and scored["unsure"] == 1
    assert scored["would_merge_different"] == 2 and scored["would_leave_apart"] == 1
    assert scored["statuses"] == {"ok": 5, "failed": 1}
    assert scored["by_resolution"]["same"] == {"right": 1, "of": 2}
