"""Real UK organizations, step 1: the data and its truth (design 07)."""

from __future__ import annotations

import duckdb
import pytest

from thisness.benchmark import uk


@pytest.mark.parametrize(
    ("raw", "number"),
    [
        ("8209948", "08209948"),  # GLEIF often drops the zero padding
        ("08209948", "08209948"),
        ("SC 123456", "SC123456"),
        ("sc123456", "SC123456"),
        ("NI012345", "NI012345"),
        ("OC301234", "OC301234"),
        ("R0000123", "R0000123"),
        ("IP12345R", "IP12345R"),
        ("12345678901", None),
        ("", None),
        (None, None),
    ],
)
def test_company_numbers_normalize_alike_in_python_and_sql(raw, number):
    assert uk.company_number(raw) == number
    con = duckdb.connect()
    con.execute(uk.COMPANY_NUMBER_SQL)
    assert con.execute("select company_number(?)", [raw]).fetchone()[0] == number


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return uk.fixture_db(tmp_path_factory.mktemp("uk") / "fixture.duckdb")


def test_the_fixture_joins_by_company_number(db):
    report = uk.truth_report(db)
    assert report["registered_with_companies_house"] >= 90
    assert report["joined_to_a_live_company"] >= 80
    # LEIs of dissolved companies have no live record: the right answer is no match.
    assert report["not_joined_by_entity_status"].get("INACTIVE", 0) >= 10
    assert report["parent_pairs_both_joined"].get("IS_DIRECTLY_CONSOLIDATED_BY", 0) >= 10
    # Live near-namesakes are the fixture's distractors.
    assert report["companies_house_live"] > 3 * report["joined_to_a_live_company"]


def test_the_join_reads_as_one_organization(db):
    """GLEIF validates UK records against Companies House: the names agree."""
    audit = uk.audit(db, n=200)
    assert audit["sampled"] >= 80
    assert audit["names"].get("differ", 0) == 0
    assert audit["postcodes"].get("same", 0) >= 0.8 * audit["sampled"]


def test_near_namesakes_are_real_and_distinct(db):
    con = duckdb.connect(str(db), read_only=True)
    groups = con.execute(f"""
        select {uk.STEM_SQL} as k, count(distinct company_number) as n from ch
        group by k having count(distinct company_number) > 1""").fetchall()
    assert len(groups) >= 20  # companies sharing a name stem, each its own company


def test_the_fixture_carries_its_attribution():
    readme = (uk.FIXTURE / "README.md").read_text(encoding="utf-8")
    assert "CC0" in readme and "Open Government Licence v3.0" in readme


def test_cached_files_need_all_three(tmp_path):
    assert uk.cached_files(tmp_path) is None
    for name in ("20261009-0800-gleif-goldencopy-lei2-golden-copy.csv.zip",
                 "20261009-0800-gleif-goldencopy-rr-golden-copy.csv.zip"):  # fmt: skip
        (tmp_path / name).write_bytes(b"")
    assert uk.cached_files(tmp_path) is None
    (tmp_path / "BasicCompanyDataAsOneFile-2026-10-01.zip").write_bytes(b"")
    assert set(uk.cached_files(tmp_path)) == {"lei2", "rr", "companies_house"}


# -- the benchmark (design 07 §13.2) ----------------------------------------------------------


@pytest.fixture(scope="module")
def bench():
    return uk.from_fixture(60, seed=7)


def test_the_benchmark_is_deterministic_and_real_where_it_counts(bench):
    again = uk.from_fixture(60, seed=7)
    assert [r.attributes for r in bench.records] == [r.attributes for r in again.records]
    sources = bench.config["sources"]
    assert sources["companies_house"] > 0 and sources["gleif"] > 0
    assert sum(sources.get(s, 0) for s in uk.FILE_SOURCES) > 0
    assert {"group", "namesake", "dissolved"} <= set(bench.kind.values())
    # Truth is the company number, or the LEI of a company no longer live.
    assert all(o.startswith("lei:") or uk.company_number(o) == o for o in bench.orgs)


def test_identifiers_follow_the_mode(bench):
    assert not any("registration_ids" in r.attributes for r in bench.records)
    visible = uk.from_fixture(60, seed=7, mode="visible")
    assert all(r.attributes.get("registration_ids") for r in visible.records)
    with pytest.raises(ValueError):
        uk.from_fixture(10, mode="sometimes")


def test_no_generated_name_reads_as_another_companys(bench):
    """A supplier-file name that normalizes to another sampled company's own name would
    be a record nobody could place; the generator writes it another way. Two real
    companies can share a name key (PAMIA LIMITED and the assurance company PAMIA): a
    record carrying a company's own name is real ambiguity, not an invented one."""
    from thisness.normalize.organization import name_parts

    def key(name):
        return tuple(name_parts(name)[0])

    own = {}
    for org, info in bench.orgs.items():
        own.setdefault(key(info["name"]), set()).add(org)
    for r in bench.records:
        if r.source in uk.FILE_SOURCES and key(r.attributes["name"]) != key(
            bench.orgs[r.org_id]["name"]
        ):
            assert own.get(key(r.attributes["name"]), {r.org_id}) <= {r.org_id}, r


def test_uk_benchmark_regression(bench):
    """The organization profile on real UK companies and their near-namesakes."""
    from thisness.benchmark.evaluate import run_benchmark

    report, _, _ = run_benchmark(uk.from_fixture(60), random_pairs=60_000)
    c = report.clusters
    assert c.b3_precision >= 0.99  # 1.000 when written
    assert c.false_merge_clusters <= 1  # 0
    assert c.b3_recall >= 0.6  # 0.674: 142 organizations are few to fit weights on
