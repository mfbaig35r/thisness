"""Real UK organizations (design 07): GLEIF and Companies House, joined by company number.

Three open sources, fetched on demand and cached under ``~/.cache/thisness/uk``:

- GLEIF Golden Copy, Level 1 (CC0): every LEI, with its registration authority and the
  entity's number there;
- GLEIF Golden Copy, Level 2 (CC0): direct and ultimate accounting parents;
- Companies House basic company data (Open Government Licence v3.0): every live UK
  company, its registered office, type, SIC codes and previous names.

Ground truth (design 07 §3): a Companies House record and an LEI name one organization
when the LEI's registration authority is Companies House and its number there is the
company number. ``stage`` loads what the benchmark needs into one DuckDB file;
``truth_report`` counts how good that join is before anything is measured on it.
"""

from __future__ import annotations

import json
import random as _random
import re
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from thisness.benchmark.organizations import _typo
from thisness.benchmark.taxa import _context
from thisness.storage.duckdb.store import _first

CACHE = Path.home() / ".cache" / "thisness" / "uk"
GLEIF_LATEST = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/latest"
CH_INDEX = "https://download.companieshouse.gov.uk/en_output.html"
CH_BASE = "https://download.companieshouse.gov.uk/"
# Companies House as a GLEIF registration authority.
CH_AUTHORITIES = {"RA000585": "England and Wales", "RA000586": "Northern Ireland",
                  "RA000587": "Scotland"}  # fmt: skip
ATTRIBUTION = (
    "Contains GLEIF Golden Copy data (CC0 1.0) and Companies House data, "
    "public sector information licensed under the Open Government Licence v3.0."
)
USER_AGENT = "thisness-benchmark/1"


# -- fetching ---------------------------------------------------------------------------------


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60, context=_context()) as response:
        return response.read()


def latest_files() -> dict[str, str]:
    """Download urls of the latest GLEIF Level 1 and 2 and Companies House files."""
    gleif = json.loads(_get(GLEIF_LATEST))["data"]
    index = _get(CH_INDEX).decode("utf-8", "replace")
    found = re.findall(r"BasicCompanyDataAsOneFile-\d{4}-\d{2}-\d{2}\.zip", index)
    if not found:
        raise RuntimeError("no BasicCompanyDataAsOneFile on the Companies House download page")
    return {"lei2": gleif["lei2"]["full_file"]["csv"]["url"],
            "rr": gleif["rr"]["full_file"]["csv"]["url"],
            "companies_house": CH_BASE + sorted(found)[-1]}  # fmt: skip


def download(url: str, raw: Path = CACHE / "raw") -> Path:
    """Stream ``url`` to ``raw`` once; a finished file is never fetched again."""
    raw.mkdir(parents=True, exist_ok=True)
    target = raw / url.rsplit("/", 1)[1]
    if target.exists():
        return target
    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120, context=_context()) as response, \
            partial.open("wb") as out:  # fmt: skip
        shutil.copyfileobj(response, out, length=1 << 20)
    partial.rename(target)
    return target


PATTERNS = {"lei2": "*-gleif-goldencopy-lei2-golden-copy.csv.zip",
            "rr": "*-gleif-goldencopy-rr-golden-copy.csv.zip",
            "companies_house": "BasicCompanyDataAsOneFile-*.zip"}  # fmt: skip


def cached_files(raw: Path = CACHE / "raw") -> dict[str, Path] | None:
    """The newest downloaded file of each kind, or None when one is missing. GLEIF
    publishes three times a day; a benchmark keeps to the files it was built from."""
    found = {k: sorted(raw.glob(p))[-1:] for k, p in PATTERNS.items()}
    return {k: v[0] for k, v in found.items()} if all(found.values()) else None


def _extract(archive: Path) -> Path:
    with zipfile.ZipFile(archive) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        target = archive.parent / name
        if not target.exists():
            z.extract(name, archive.parent)
    return target


# -- company numbers --------------------------------------------------------------------------


def company_number(raw: str | None) -> str | None:
    """A Companies House number as the register writes it: eight characters, digits
    zero-padded, a letter prefix kept ("SC", "NI", "OC", ...). GLEIF records often drop
    the padding ("8209948") or add spaces ("SC 123456")."""
    if not raw:
        return None
    text = re.sub(r"[^0-9A-Za-z]", "", raw).upper()
    if not text:
        return None
    if text.isdigit():
        return text.zfill(8) if len(text) <= 8 else None
    m = re.fullmatch(r"([A-Z]{1,2})(\d{1,6})", text)
    if m:
        return m.group(1) + m.group(2).zfill(8 - len(m.group(1)))
    return text if len(text) == 8 else None


# The same rule as a DuckDB macro, so staging needs no Python function (and no numpy).
COMPANY_NUMBER_SQL = """
create or replace macro company_number(raw) as (
  with t as (select upper(regexp_replace(coalesce(raw, ''), '[^0-9A-Za-z]', '', 'g')) as v)
  select case
    when v = '' then null
    when regexp_full_match(v, '[0-9]+') then
      case when length(v) <= 8 then lpad(v, 8, '0') end
    when regexp_full_match(v, '[A-Z]{1,2}[0-9]{1,6}') then
      regexp_extract(v, '^([A-Z]+)', 1)
        || lpad(regexp_extract(v, '([0-9]+)$', 1),
                CAST(8 - length(regexp_extract(v, '^([A-Z]+)', 1)) AS INTEGER), '0')
    when length(v) = 8 then v
  end from t
)
"""


# -- staging ----------------------------------------------------------------------------------


def _q(column: str) -> str:
    return '"' + column.replace('"', '""') + '"'


def stage(db: Path = CACHE / "uk.duckdb", *, raw: Path = CACHE / "raw",
          keep_csv: bool = False, refresh: bool = False) -> Path:  # fmt: skip
    """Load the three sources into ``db``: ``gleif`` (LEIs registered with Companies
    House, or with a UK legal or headquarters address), ``gleif_rr`` (relationships
    touching them), ``ch`` (every live company). Extracted CSVs are deleted afterwards
    unless ``keep_csv``; the downloaded zips stay in ``raw``."""
    import duckdb

    zips = None if refresh else cached_files(raw)
    if zips is None:
        zips = {k: download(u, raw) for k, u in latest_files().items()}
    files = {k: p.name for k, p in zips.items()}
    csvs = {k: _extract(p) for k, p in zips.items()}
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    try:
        con.execute(COMPANY_NUMBER_SQL)
        authorities = ", ".join(f"'{a}'" for a in CH_AUTHORITIES)
        other = [f"Entity.OtherEntityNames.OtherEntityName.{i}" for i in range(1, 6)] + \
            [f"Entity.TransliteratedOtherEntityNames.TransliteratedOtherEntityName.{i}"
             for i in range(1, 6)]  # fmt: skip
        other_types = [f"{c}.type" for c in other]
        con.execute(f"""
            create or replace table gleif as
            select "LEI" as lei, "Entity.LegalName" as legal_name,
                   list_filter([{", ".join(_q(c) for c in other)}], lambda x: x is not null and x <> '')
                       as other_names,
                   list_filter([{", ".join(_q(c) for c in other_types)}], lambda x: x is not null and x <> '')
                       as other_name_types,
                   "Entity.LegalAddress.FirstAddressLine" as legal_address_line,
                   "Entity.LegalAddress.City" as legal_city,
                   "Entity.LegalAddress.PostalCode" as legal_postcode,
                   "Entity.LegalAddress.Country" as legal_country,
                   "Entity.HeadquartersAddress.FirstAddressLine" as hq_address_line,
                   "Entity.HeadquartersAddress.City" as hq_city,
                   "Entity.HeadquartersAddress.PostalCode" as hq_postcode,
                   "Entity.HeadquartersAddress.Country" as hq_country,
                   "Entity.RegistrationAuthority.RegistrationAuthorityID" as ra_id,
                   "Entity.RegistrationAuthority.RegistrationAuthorityEntityID" as ra_entity_id,
                   company_number("Entity.RegistrationAuthority.RegistrationAuthorityEntityID")
                       as company_number,
                   "Entity.LegalJurisdiction" as jurisdiction,
                   "Entity.EntityCategory" as category,
                   "Entity.LegalForm.EntityLegalFormCode" as legal_form_code,
                   "Entity.LegalForm.OtherLegalForm" as other_legal_form,
                   "Entity.EntityStatus" as entity_status,
                   "Entity.EntityCreationDate" as creation_date,
                   "Registration.RegistrationStatus" as registration_status
            from read_csv('{csvs["lei2"]}', all_varchar=true, header=true)
            where "Entity.RegistrationAuthority.RegistrationAuthorityID" in ({authorities})
               or "Entity.LegalAddress.Country" = 'GB'
               or "Entity.HeadquartersAddress.Country" = 'GB'
        """)
        con.execute(f"""
            create or replace table gleif_rr as
            select "Relationship.StartNode.NodeID" as child,
                   "Relationship.EndNode.NodeID" as parent,
                   "Relationship.RelationshipType" as type,
                   "Relationship.RelationshipStatus" as status,
                   "Registration.RegistrationStatus" as registration_status
            from read_csv('{csvs["rr"]}', all_varchar=true, header=true)
            where "Relationship.StartNode.NodeIDType" = 'LEI'
              and "Relationship.EndNode.NodeIDType" = 'LEI'
              and ("Relationship.StartNode.NodeID" in (select lei from gleif)
                   or "Relationship.EndNode.NodeID" in (select lei from gleif))
        """)
        previous = ", ".join(f'"PreviousName_{i}.CompanyName"' for i in range(1, 11))
        sic = ", ".join(f'"SICCode.SicText_{i}"' for i in range(1, 5))
        con.execute(f"""
            create or replace table ch as
            select "CompanyName" as name, trim("CompanyNumber") as company_number,
                   "RegAddress.CareOf" as care_of, "RegAddress.POBox" as po_box,
                   "RegAddress.AddressLine1" as address_line1,
                   "RegAddress.AddressLine2" as address_line2,
                   "RegAddress.PostTown" as post_town, "RegAddress.County" as county,
                   "RegAddress.Country" as country, "RegAddress.PostCode" as postcode,
                   "CompanyCategory" as category, "CompanyStatus" as status,
                   "CountryOfOrigin" as country_of_origin,
                   "IncorporationDate" as incorporation_date,
                   list_filter([{sic}], lambda x: x is not null and x <> '') as sic,
                   list_filter([{previous}], lambda x: x is not null and x <> '') as previous_names
            from read_csv('{csvs["companies_house"]}', all_varchar=true, header=true,
                          normalize_names=false)
        """)
        con.execute("create or replace table meta as select * from (values "
                    + ", ".join(f"('{k}', '{u}')" for k, u in files.items())
                    + ") t(source, file)")  # fmt: skip
    finally:
        con.close()
    if not keep_csv:
        for path in csvs.values():
            path.unlink(missing_ok=True)
    return db


# -- the truth join ---------------------------------------------------------------------------


def truth_report(db: Path = CACHE / "uk.duckdb") -> dict[str, Any]:
    """How good the company-number join is (design 07 §3): counted, not assumed."""
    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    try:
        authorities = ", ".join(f"'{a}'" for a in CH_AUTHORITIES)

        def one(sql: str) -> Any:
            return _first(con.execute(sql))[0]

        def rows(sql: str) -> list[tuple]:
            return con.execute(sql).fetchall()

        registered = f"from gleif where ra_id in ({authorities})"
        out: dict[str, Any] = {
            "sources": dict(rows("select source, file from meta")),
            "gleif_uk_related": one("select count(*) from gleif"),
            "registered_with_companies_house": one(f"select count(*) {registered}"),
            "by_authority": dict(rows(f"select ra_id, count(*) {registered} group by 1")),
            "by_registration_status": dict(rows(
                f"select registration_status, count(*) {registered} group by 1 order by 2 desc")),
            "by_entity_status": dict(rows(
                f"select entity_status, count(*) {registered} group by 1 order by 2 desc")),
            "number_unusable": one(f"select count(*) {registered} and company_number is null"),
            "companies_house_live": one("select count(*) from ch"),
        }  # fmt: skip
        out["joined_to_a_live_company"] = one(f"""
            select count(*) {registered} and company_number in (select company_number from ch)""")
        out["joined_issued_and_active"] = one(f"""
            select count(*) {registered} and registration_status = 'ISSUED'
              and entity_status = 'ACTIVE'
              and company_number in (select company_number from ch)""")
        out["not_joined_by_entity_status"] = dict(
            rows(f"""
            select entity_status, count(*) {registered} and company_number is not null
              and company_number not in (select company_number from ch) group by 1""")
        )
        out["numbers_with_several_leis"] = one(f"""
            select count(*) from (select company_number {registered}
              and company_number is not null group by 1 having count(*) > 1)""")
        out["parent_pairs_both_joined"] = dict(
            rows(f"""
            with j as (select lei {registered} and company_number in
                       (select company_number from ch))
            select type, count(*) from gleif_rr
            where status = 'ACTIVE' and child in (select lei from j)
              and parent in (select lei from j)
              and type in ('IS_DIRECTLY_CONSOLIDATED_BY', 'IS_ULTIMATELY_CONSOLIDATED_BY')
            group by 1""")
        )
        return out
    finally:
        con.close()


def _postcode(raw: str | None) -> str | None:
    text = re.sub(r"\s+", "", raw or "").upper()
    return text or None


def audit(db: Path = CACHE / "uk.duckdb", *, n: int = 400, seed: int = 1) -> dict[str, Any]:
    """Read a sample of the join (design 07 §3): do the two records look like one
    organization? Names compare as the Organization profile's tokens (legal form set
    aside); a name that is the other side's previous or alternative name counts as a
    rename. Disagreements are returned whole, to be read."""
    import random

    import duckdb

    from thisness.normalize.organization import name_parts

    authorities = ", ".join(f"'{a}'" for a in CH_AUTHORITIES)
    con = duckdb.connect(str(db), read_only=True)
    try:
        pairs = con.execute(f"""
            select g.lei, g.legal_name, g.other_names, g.legal_postcode, g.hq_postcode,
                   c.company_number, c.name, c.previous_names, c.postcode, c.category
            from gleif g join ch c using (company_number)
            where g.ra_id in ({authorities}) and g.registration_status = 'ISSUED'
              and g.entity_status = 'ACTIVE'
            order by hash(g.lei || '{seed}') limit {int(n)}""").fetchall()
    finally:
        con.close()

    def key(name: str | None) -> tuple[str, ...]:
        return tuple(name_parts(name)[0]) if name else ()

    names, postcodes = {}, {}
    differing = []
    for lei, g_name, g_other, g_post, g_hq_post, number, c_name, c_previous, c_post, cat in pairs:
        if key(g_name) == key(c_name):
            verdict = "same"
        elif key(g_name) in {key(p) for p in c_previous or []} or \
                key(c_name) in {key(o) for o in g_other or []}:  # fmt: skip
            verdict = "renamed"
        else:
            verdict = "differ"
            differing.append({"lei": lei, "gleif": g_name, "gleif_other": list(g_other or []),
                              "company_number": number, "companies_house": c_name,
                              "previous": list(c_previous or [])[:3], "category": cat})  # fmt: skip
        names[verdict] = names.get(verdict, 0) + 1
        post = {_postcode(g_post), _postcode(g_hq_post)} - {None}
        p = ("missing" if not post or not _postcode(c_post)
             else "same" if _postcode(c_post) in post else "different")  # fmt: skip
        postcodes[p] = postcodes.get(p, 0) + 1
    random.Random(seed).shuffle(differing)
    return {"sampled": len(pairs), "names": names, "postcodes": postcodes,
            "differing_names": differing}  # fmt: skip


# -- the shipped fixture ----------------------------------------------------------------------

FIXTURE = Path(__file__).parent / "data" / "uk"
TABLES = ("gleif", "gleif_rr", "ch")

# A rough name stem for finding near-namesakes: lowercase words with legal forms and
# common suffixes dropped, the first two kept ("Sports Direct International Limited"
# and "Sports Direct Holdings Limited" share "sports direct").
STEM_SQL = r"""array_to_string(list_slice(list_filter(string_split(trim(regexp_replace(
    regexp_replace(regexp_replace(lower(name), '[^a-z0-9 ]', ' ', 'g'),
    '\b(limited|ltd|plc|llp|lp|uk|the|holdings|group|services|company|co)\b', ' ', 'g'),
    '\s+', ' ', 'g')), ' '), lambda x: x <> ''), 1, 2), ' ')"""


def make_fixture(db: Path = CACHE / "uk.duckdb", out: Path = FIXTURE, *,
                 companies: int = 60, parents: int = 15, lapsed: int = 15) -> Path:  # fmt: skip
    """A small hard slice of the staged data to ship for tests (design 07 §2): joined
    companies with all their live near-namesakes, parent and subsidiary pairs with both
    sides joined, and LEIs whose company is no longer live. Parquet keeps the lists."""
    import duckdb

    authorities = ", ".join(f"'{a}'" for a in CH_AUTHORITIES)
    out.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db), read_only=True)
    try:
        con.execute(f"""
            create temp table joined as
            select g.lei, g.company_number from gleif g join ch c using (company_number)
            where g.ra_id in ({authorities}) and g.registration_status = 'ISSUED'
              and g.entity_status = 'ACTIVE'""")
        con.execute(f"create temp table stems as select company_number, {STEM_SQL} as k from ch")
        con.execute(f"""
            create temp table pick_lei as
            (select lei from joined j join stems using (company_number)
             where k in (select k from stems group by k having count(*) between 2 and 6)
             order by hash(lei) limit {int(companies)})
            union
            (select child from gleif_rr
             where type = 'IS_DIRECTLY_CONSOLIDATED_BY' and status = 'ACTIVE'
               and child in (select lei from joined) and parent in (select lei from joined)
             order by hash(child) limit {int(parents)})
            union
            (select parent from gleif_rr
             where type = 'IS_DIRECTLY_CONSOLIDATED_BY' and status = 'ACTIVE'
               and child in (select lei from joined) and parent in (select lei from joined)
             order by hash(child) limit {int(parents)})
            union
            (select lei from gleif where ra_id in ({authorities}) and entity_status = 'INACTIVE'
             order by hash(lei) limit {int(lapsed)})""")
        con.execute("""
            create temp table pick_number as
            select company_number from stems
            where k in (select k from stems join gleif g using (company_number)
                        where g.lei in (select lei from pick_lei) and k <> '')
            union select company_number from gleif where lei in (select lei from pick_lei)""")
        con.execute(
            f"copy (select * from gleif where lei in (select lei from pick_lei) "
            f"order by lei) to '{out / 'gleif.parquet'}' (format parquet)"
        )
        con.execute(
            f"copy (select * from gleif_rr where child in (select lei from pick_lei) "
            f"and parent in (select lei from pick_lei) order by child, parent, type) "
            f"to '{out / 'gleif_rr.parquet'}' (format parquet)"
        )
        con.execute(f"copy (select * from ch where company_number in "
                    f"(select company_number from pick_number) order by company_number) "
                    f"to '{out / 'ch.parquet'}' (format parquet)")  # fmt: skip
        sources = con.execute("select source, file from meta order by source").fetchall()
    finally:
        con.close()
    (out / "README.md").write_text(
        "# UK organizations fixture\n\n"
        "A small slice of real data for tests (design 07): GLEIF Golden Copy Level 1 and 2 "
        "records registered with Companies House, and the Companies House records of those "
        "companies and their live near-namesakes.\n\n"
        f"{ATTRIBUTION}\n\nBuilt from: "
        + ", ".join(f"{f} ({s})" for s, f in sources) + ".\n",
        encoding="utf-8",
    )  # fmt: skip
    return out


def fixture_db(path: Path, source: Path = FIXTURE) -> Path:
    """A DuckDB file holding the shipped fixture as the staged tables, so the truth report
    and the audit run on it unchanged."""
    import duckdb

    con = duckdb.connect(str(path))
    try:
        for table in TABLES:
            con.execute(f"create or replace table {table} as "
                        f"select * from read_parquet('{source / (table + '.parquet')}')")  # fmt: skip
        con.execute(
            "create or replace table meta as select * from "
            "(values ('fixture', 'thisness/benchmark/data/uk')) t(source, file)"
        )
    finally:
        con.close()
    return path


# -- the benchmark (design 07 §5, §13.2) ------------------------------------------------------
#
# Who the organizations are, how they are named and how they relate is real: companies
# with an LEI joined to Companies House, every live near-namesake of theirs, their
# parent and subsidiary links, LEIs of companies no longer live. What is generated is
# how a supplier or customer file writes them, as the taxon benchmark generates names
# from WFO's: shortened, abbreviated, renamed, mistyped, with partial addresses.

GENERATOR_VERSION = "1"
FILE_SOURCES = ("erp", "accounts_payable", "crm")
ABBREVIATIONS = {"international": "intl", "services": "svcs", "company": "co",
                 "management": "mgmt", "holdings": "hldgs", "associates": "assoc",
                 "manufacturing": "mfg", "properties": "props", "investments": "invs"}  # fmt: skip
FORM_VARIANTS = {"limited": ["LTD", "Ltd", "Ltd.", "Limited", ""],
                 "ltd": ["LIMITED", "Limited", "Ltd", ""],
                 "plc": ["PLC", "plc", "P.L.C.", "Public Limited Company"],
                 "llp": ["LLP", "L.L.P.", ""]}  # fmt: skip
MODES = ("hidden", "partial", "visible")


@dataclass(frozen=True)
class Record:
    record_id: str
    org_id: str
    attributes: dict
    source: str


@dataclass
class Benchmark:
    seed: int
    records: list[Record]
    orgs: dict[str, dict]  # org id -> kind, group, name
    files: dict[str, str]
    mode: str = "hidden"
    entity_type: str = "organization"
    variant: str = ""  # "names_only" when every attribute but the name is removed
    kind: dict[str, str] = field(init=False)
    truth: dict[str, str] = field(init=False)  # record id -> org id
    group: dict[str, str | None] = field(init=False)  # org id -> corporate group

    def __post_init__(self) -> None:
        self.kind = {o: info["kind"] for o, info in self.orgs.items()}
        self.group = {o: info["group"] for o, info in self.orgs.items()}
        self.truth = {r.record_id: r.org_id for r in self.records}

    @property
    def config(self) -> dict:
        from collections import Counter

        return {"generator": "uk", "version": GENERATOR_VERSION, "seed": self.seed,
                "mode": self.mode, "files": self.files,
                "organizations": len(self.orgs), "records": len(self.records),
                "kinds": dict(Counter(self.kind.values())),
                "sources": dict(Counter(r.source for r in self.records)),
                **({"variant": self.variant} if self.variant else {})}  # fmt: skip


def _universe(db: Path, n: int, seed: int) -> tuple[list[dict], list[tuple[str, str]], dict]:
    """The sampled organizations as rows, the parent links among them, and the files."""
    import duckdb

    authorities = ", ".join(f"'{a}'" for a in CH_AUTHORITIES)
    con = duckdb.connect(str(db), read_only=True)
    try:
        con.execute(f"""create temp table joined as
            select g.lei, g.company_number from gleif g join ch c using (company_number)
            where g.ra_id in ({authorities}) and g.registration_status = 'ISSUED'
              and g.entity_status = 'ACTIVE'""")
        con.execute(f"create temp table stems as select company_number, {STEM_SQL} as k from ch")
        con.execute("""create temp table groups as select k, count(*) as size from stems
                       where k <> '' group by k""")
        s = f"'{seed}'"
        n_namesake, n_pairs, n_plain, n_gone = n // 2, n // 8, n - n // 2 - n // 4, n // 10
        con.execute(f"""create temp table targets as
            (select j.company_number from joined j join stems using (company_number)
             join groups using (k) where size between 2 and 8
             order by hash(j.lei || {s}) limit {n_namesake})
            union
            (select company_number from joined order by hash(lei || {s} || 'plain')
             limit {n_plain})""")
        con.execute(f"""create temp table pairs as
            select r.child, r.parent from gleif_rr r
            where r.type = 'IS_DIRECTLY_CONSOLIDATED_BY' and r.status = 'ACTIVE'
              and r.child in (select lei from joined) and r.parent in (select lei from joined)
            order by hash(r.child || {s}) limit {n_pairs}""")
        con.execute("""insert into targets select company_number from joined
            where lei in (select child from pairs union select parent from pairs)
              and company_number not in (select company_number from targets)""")
        # Every live near-namesake of a target, at most six to a name.
        con.execute(f"""create temp table universe as
            select company_number from targets
            union
            select company_number from (
              select st.company_number, row_number() over (partition by st.k
                     order by hash(st.company_number || {s})) as rn
              from stems st where st.k in (select k from stems join targets using (company_number)
                                           where k <> '')) where rn <= 6""")
        rows = con.execute("""
            select c.company_number, c.name, c.address_line1, c.address_line2, c.post_town,
                   c.country, c.postcode, c.category, c.previous_names,
                   g.lei, g.legal_name, g.other_names, g.legal_address_line, g.legal_city,
                   g.legal_postcode, g.legal_country, g.hq_address_line, g.hq_city,
                   g.hq_postcode, st.k
            from universe u join ch c using (company_number) join stems st using (company_number)
            left join joined j using (company_number) left join gleif g on g.lei = j.lei
            order by c.company_number""").fetchall()
        cols = ["company_number", "name", "address_line1", "address_line2", "post_town",
                "country", "postcode", "category", "previous_names", "lei", "legal_name",
                "other_names", "legal_address_line", "legal_city", "legal_postcode",
                "legal_country", "hq_address_line", "hq_city", "hq_postcode", "stem"]  # fmt: skip
        out = [dict(zip(cols, r, strict=True)) for r in rows]
        # LEIs of companies no longer live: a GLEIF record whose right answer is no match.
        gone = con.execute(f"""
            select lei, legal_name, legal_address_line, legal_city, legal_postcode, legal_country
            from gleif where ra_id in ({authorities}) and entity_status = 'INACTIVE'
            order by hash(lei || {s}) limit {n_gone}""").fetchall()
        for lei, name, line, city, post, country in gone:
            out.append({"company_number": None, "lei": lei, "legal_name": name,
                        "legal_address_line": line, "legal_city": city,
                        "legal_postcode": post, "legal_country": country,
                        "gone": True})  # fmt: skip
        pairs = con.execute("select child, parent from pairs").fetchall()
        files = dict(con.execute("select source, file from meta").fetchall())
    finally:
        con.close()
    return out, pairs, files


def _register(number: str) -> str:
    prefix = re.match(r"[A-Z]*", number).group(0)
    return prefix or "UK"


def _same_registrations(rows: list[dict]) -> dict[str, str]:
    """Company numbers that are another registration of one legal entity, mapped to the
    number that stands for it: FC with OE, and AC, NF or SF with any registration, when
    the names are identical."""
    from thisness.normalize.organization import name_parts

    by_name: dict[tuple[str, ...], list[str]] = {}
    for r in rows:
        if r.get("company_number"):
            by_name.setdefault(tuple(name_parts(r["name"])[0]), []).append(r["company_number"])
    alias: dict[str, str] = {}
    for numbers in by_name.values():
        if len(numbers) < 2:
            continue
        registers = {n: _register(n) for n in numbers}
        joined = [n for n in numbers if registers[n] in ("AC", "NF", "SF")]
        overseas = [n for n in numbers if registers[n] in ("FC", "OE")]
        if {"FC", "OE"} <= {registers[n] for n in overseas}:
            joined += overseas
        if not joined:
            continue
        rest = [n for n in numbers if n not in joined]
        # The ordinary registration stands for the entity when there is one.
        keep = sorted(rest or joined, key=lambda n: (registers[n] != "UK", n))[0]
        for n in {*joined, keep}:
            alias[n] = keep
    return alias


def _file_name(name: str, rng: _random.Random) -> str:
    """How a supplier file might write a company name."""
    words = name.split()
    if words and words[-1].lower().strip(".") in FORM_VARIANTS and rng.random() < 0.7:
        form = rng.choice(FORM_VARIANTS[words[-1].lower().strip(".")])
        words = words[:-1] + ([form] if form else [])
    if rng.random() < 0.25:
        words = [ABBREVIATIONS.get(w.lower(), w) if rng.random() < 0.7 else w for w in words]
    if rng.random() < 0.15:
        words = [w for w in words if w.strip("()").upper() != "UK"]
    if rng.random() < 0.1:
        words = ["and" if w == "&" else "&" if w.lower() == "and" else w for w in words]
    if words and rng.random() < 0.05:
        i = rng.randrange(len(words))
        words[i] = _typo(words[i], rng)
    text = " ".join(w for w in words if w)
    r = rng.random()
    return text.title() if r < 0.4 else text.lower() if r < 0.5 else text


def _short_name(name: str) -> str:
    """The first two words a person would type: "Sports Direct" for Sports Direct
    International Limited. Shared by its near-namesakes, so genuinely ambiguous."""
    words = [w for w in re.findall(r"[A-Za-z0-9&']+", name)
             if w.lower() not in ("limited", "ltd", "plc", "llp", "the", "uk")]  # fmt: skip
    return " ".join(words[:2]).title()


def _postcode_variant(postcode: str, rng: _random.Random) -> str:
    compact = postcode.replace(" ", "")
    return rng.choice([postcode, compact, postcode.lower()])


def generate(db: Path = CACHE / "uk.duckdb", n: int = 400, seed: int = 20261009, *,
             mode: str = "hidden", partial_share: float = 0.3) -> Benchmark:  # fmt: skip
    """About ``n`` sampled companies with their namesakes and parents, each with its
    Companies House record, its GLEIF record when it has an LEI, and up to three
    supplier-file records. ``mode``: identifiers hidden, on ``partial_share`` of the
    records, or visible."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    rows, pairs, files = _universe(db, n, seed)
    rng = _random.Random(seed)
    # Corporate groups: parent links among the sampled LEIs, as connected sets.
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    for child, par in pairs:
        parent[find(child)] = find(par)
    in_group = {lei for pair in pairs for lei in pair}
    stem_count: dict[str, int] = {}
    for r in rows:
        if r.get("stem"):
            stem_count[r["stem"]] = stem_count.get(r["stem"], 0) + 1

    # One legal entity on two of Companies House's registers (design 07 §13.3): the same
    # foreign company as an overseas company (FC) and an overseas entity (OE); an insurer
    # on the assurance-company register (AC); a UK company's old Northern Irish or Scottish
    # overseas registration (NF, SF). Identically named, these are one organization. An
    # overseas entity beside a UK company of the same name usually is not (Cluny Estates
    # Limited in Leicestershire and in Jersey), and stays two.
    alias = _same_registrations(rows)

    records: list[Record] = []
    orgs: dict[str, dict] = {}
    # A generated name that reads as another sampled company's own name would be a
    # record nobody could place ("Lex Autolease" written for Lex Autolease (VC) Limited,
    # beside Lex Autolease Limited): it is generated again. Shortened names matching no
    # company exactly ("Sports Direct") stay: those are the honest hard cases.
    from thisness.normalize.organization import name_parts

    def key(name: str) -> tuple[str, ...]:
        return tuple(name_parts(name)[0])

    owner: dict[tuple[str, ...], set[str]] = {}
    for r in rows:
        name = r.get("name") or r["legal_name"]
        org_id = r["company_number"] or f"lei:{r['lei']}"
        owner.setdefault(key(name), set()).add(org_id)
        # Two companies shortened alike ("Galliford Try" for two Galliford Try companies
        # at one address) would give identical records: a shortening must be one's own.
        owner.setdefault(key(_short_name(name)), set()).add(org_id)
    rejected = 0

    def add(org: str, attributes: dict, source: str, ids: list[str]) -> None:
        show = mode == "visible" or (mode == "partial" and rng.random() < partial_share)
        attrs = {k: v for k, v in attributes.items() if v}
        if show and ids:
            attrs["registration_ids"] = ids
        records.append(Record(f"rec{len(records):06d}", org, attrs, source))

    for r in rows:
        number, lei = r["company_number"], r.get("lei")
        org = alias.get(number, number) if number else f"lei:{lei}"
        ids = ([f"GB-COH:{number}"] if number else []) + ([f"LEI:{lei}"] if lei else [])
        if r.get("gone"):
            kind = "dissolved"
        elif lei and lei in in_group:
            kind = "group"
        elif stem_count.get(r.get("stem") or "", 0) > 1:
            kind = "namesake"
        elif r.get("previous_names"):
            kind = "renamed"
        else:
            kind = "plain"
        group = find(lei) if lei and lei in in_group else None
        if (number and alias.get(number, number) != number) or org in orgs:
            kind = "registered_twice"
        orgs[org] = {"kind": kind, "group": group,
                     "name": orgs.get(org, {}).get("name") or r.get("name") or r["legal_name"]}  # fmt: skip
        if number:  # Companies House's own record
            line = ", ".join(x for x in (r["address_line1"], r["address_line2"]) if x)
            add(org, {"name": r["name"], "address_line": line, "city": r["post_town"],
                      "postal_code": r["postcode"], "country": r["country"]},
                "companies_house", ids)  # fmt: skip
        if lei:  # GLEIF's
            add(org, {"name": r["legal_name"], "address_line": r["legal_address_line"],
                      "city": r["legal_city"], "postal_code": r["legal_postcode"],
                      "country": r.get("legal_country")}, "gleif", ids)  # fmt: skip
        # Supplier-file records: most for the sampled companies, fewer for namesakes.
        base = r.get("name") or r["legal_name"]
        names = [base, *(r.get("previous_names") or [])]
        for _ in range(min(int(rng.expovariate(0.9)) + (1 if number else 0), 3)):
            for _attempt in range(4):
                x = rng.random()
                if x < 0.1:
                    name = _short_name(base)
                elif x < 0.25 and len(names) > 1:
                    name = _file_name(rng.choice(names[1:]), rng)
                else:
                    name = _file_name(base, rng)
                if owner.get(key(name), {org}) <= {org}:
                    break
                rejected += 1
            else:
                name = base
            post = r.get("postcode") or r.get("legal_postcode") or ""
            city = r.get("post_town") or r.get("legal_city")
            line = r.get("address_line1") or r.get("legal_address_line")
            y = rng.random()
            address = ({"address_line": line, "city": city,
                        "postal_code": _postcode_variant(post, rng)} if y < 0.5 and post else
                       {"city": city, "postal_code": _postcode_variant(post, rng)}
                       if y < 0.75 and post else {"city": city})  # fmt: skip
            add(org, {"name": name, **address,
                      "country": r.get("country") or r.get("legal_country")},
                rng.choice(FILE_SOURCES), ids)  # fmt: skip
    return Benchmark(seed, records, orgs, {**files, "names_regenerated": str(rejected)}, mode)


def standard(n: int = 400, seed: int = 20261009, *, mode: str = "hidden") -> Benchmark:
    """The UK benchmark from the staged data (``python -m thisness.benchmark uk-stage``)."""
    db = CACHE / "uk.duckdb"
    if not db.exists():
        raise FileNotFoundError(
            f"{db}: stage the UK data first (python -m thisness.benchmark uk-stage)"
        )
    return generate(db, n, seed, mode=mode)


def from_fixture(n: int = 60, seed: int = 20261009, *, mode: str = "hidden") -> Benchmark:
    """The UK benchmark from the shipped fixture, for tests."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        return generate(fixture_db(Path(tmp) / "fixture.duckdb"), n, seed, mode=mode)
