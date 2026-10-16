"""Real people from SEC EDGAR (design 09).

Section 16 insiders (directors, officers, holders of more than 10%) file under a CIK,
which is the truth: one CIK, one person, apart from the defects the audit counts. The
people are staged from a local edgar-persons-pool store, which has already crawled the
filings; Thisness does not fetch EDGAR itself.

Staging keeps only what the benchmark needs (design 09 §7): names, former names, CIKs,
issuers, titles, role flags, filing dates, city and state. Street addresses and postal
codes are not kept.
"""

from __future__ import annotations

import random as _random
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

CACHE = Path.home() / ".cache" / "thisness" / "edgar"
MIN_CONFIDENCE = 0.85  # edgar-persons-pool's individual confidence
SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV"})  # a lone "V" is an initial

_STAGE = """
CREATE OR REPLACE TABLE people AS
SELECT person_cik AS cik, canonical_name AS name, individual_confidence AS confidence
FROM src.persons
WHERE is_individual AND individual_confidence >= $min_confidence AND len(roles) > 0;

CREATE OR REPLACE TABLE names AS
SELECT DISTINCT r.person_cik AS cik, r.source_dataset AS source, r.name
FROM src.person_source_records r JOIN people p ON p.cik = r.person_cik
WHERE r.name IS NOT NULL AND trim(r.name) <> '';

CREATE OR REPLACE TABLE former_names AS
SELECT cik, f.name, f."from" AS from_date, f."to" AS to_date FROM (
  SELECT p.cik, unnest(from_json(sp.former_names,
         '[{"name": "VARCHAR", "from": "VARCHAR", "to": "VARCHAR"}]')) AS f
  FROM people p JOIN src.persons sp ON sp.person_cik = p.cik
  WHERE sp.former_names IS NOT NULL AND sp.former_names <> '[]')
WHERE f.name IS NOT NULL;

CREATE OR REPLACE TABLE roles AS
SELECT cik, r.issuer_cik, r.issuer_name, r.is_director, r.is_officer,
       r.is_ten_percent_owner, r.is_other, r.officer_title, r.first_date, r.last_date
FROM (SELECT p.cik, unnest(sp.roles) AS r
      FROM people p JOIN src.persons sp ON sp.person_cik = p.cik);

-- City and state only: a business address's street and postal code are not kept.
CREATE OR REPLACE TABLE places AS
SELECT cik, upper(trim(a.city)) AS city, upper(trim(a.state_or_country)) AS state,
       count(*) AS filings
FROM (SELECT r.person_cik AS cik, unnest(from_json(r.addresses,
             '[{"city": "VARCHAR", "state_or_country": "VARCHAR"}]')) AS a
      FROM src.person_source_records r JOIN people p ON p.cik = r.person_cik
      WHERE r.source_dataset = 'section16' AND r.addresses IS NOT NULL)
WHERE a.city IS NOT NULL AND trim(a.city) <> ''
GROUP BY 1, 2, 3;
"""


def stage(store: Path, db: Path = CACHE / "edgar.duckdb", *,
          min_confidence: float = MIN_CONFIDENCE) -> Path:  # fmt: skip
    """Project the people of an edgar-persons-pool store into the benchmark's own file."""
    import duckdb

    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    try:
        con.execute(f"ATTACH '{Path(store).resolve()}' AS src (READ_ONLY)")
        con.execute(_STAGE.replace("$min_confidence", str(float(min_confidence))))
        version = con.execute(
            "SELECT value FROM src.pipeline_state WHERE key = 'classifier_version'"
        ).fetchone()
        con.execute("CREATE OR REPLACE TABLE meta (key VARCHAR, value VARCHAR)")
        con.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("source_store", str(Path(store).resolve())),
            ("staged_at", datetime.now(UTC).isoformat(timespec="seconds")),
            ("min_confidence", str(min_confidence)),
            ("classifier_version", version[0] if version else "1"),
        ])  # fmt: skip
        con.execute("DETACH src")
    finally:
        con.close()
    return db


def _strip_marker(name: str) -> str:
    """EDGAR sometimes tells namesakes apart with a slash: "MCDONALD JOHN D/MO", "/CA/"."""
    return name.split("/", 1)[0]


def spelling(name: str) -> str:
    """A spelling, ignoring case, punctuation and spacing (cik_lookup writes capitals)."""
    return re.sub(r"[^A-Z]", "", _strip_marker(name).upper())


def name_tokens(name: str) -> list[str]:
    """Upper-case word tokens of a name, without one-letter initials or suffixes."""
    words = re.sub(r"[^A-Za-z ]", " ", _strip_marker(name)).upper().split()
    return [w for w in words if len(w) > 1 and w not in SUFFIXES]


def name_key(name: str) -> str:
    """Order-free key: EDGAR writes "Smith John", others "John Smith"."""
    return " ".join(sorted(name_tokens(name)))


def _load(db: Path) -> dict[str, Any]:
    import duckdb

    con = duckdb.connect(str(db), read_only=True)
    try:
        people = dict(con.execute("SELECT cik, name FROM people").fetchall())
        names: dict[str, set[str]] = defaultdict(set)
        for cik, name in con.execute("SELECT cik, name FROM names").fetchall():
            names[cik].add(name)
        issuers: dict[str, set[str]] = defaultdict(set)
        roles: dict[str, list[tuple]] = defaultdict(list)
        for row in con.execute(
            "SELECT cik, issuer_cik, issuer_name, officer_title, is_director, first_date, "
            "last_date FROM roles"
        ).fetchall():
            issuers[row[0]].add(row[1])
            roles[row[0]].append(row[1:])
        places: dict[str, set[tuple[str, str]]] = defaultdict(set)
        for cik, city, state in con.execute("SELECT cik, city, state FROM places").fetchall():
            places[cik].add((city, state))
        former = dict(con.execute(
            "SELECT cik, list(name) FROM former_names GROUP BY cik").fetchall())  # fmt: skip
    finally:
        con.close()
    return {"people": people, "names": names, "issuers": issuers, "roles": roles,
            "places": places, "former": former}  # fmt: skip


def _mixed(names: set[str]) -> bool:
    """A CIK's current names share no word: two people under one CIK (design 09 §5)."""
    sets = [set(name_tokens(n)) for n in names]
    sets = [s for s in sets if s]
    return any(not (a & b) for i, a in enumerate(sets) for b in sets[i + 1 :])


def _parts(name: str) -> tuple[frozenset[str], frozenset[str]]:
    """A name's one-letter initials and generational suffixes."""
    words = re.sub(r"[^A-Za-z ]", " ", _strip_marker(name)).upper().split()
    return (frozenset(w for w in words if len(w) == 1),
            frozenset(w for w in words if w in SUFFIXES and len(w) > 1))  # fmt: skip


def compatible(a: str | set[str], b: str | set[str]) -> bool:
    """Two people with one name key may be one person: the same suffix, and initials that
    do not conflict ("Smith John A" and "Smith John" may be; "Smith John A" and "Smith
    John B" are not, nor "Smith John" and "Smith John Jr"). Given a set of names (a
    CIK's spellings), the initials and suffixes of all of them count."""

    def parts(names: str | set[str]) -> tuple[frozenset[str], frozenset[str]]:
        found = [_parts(n) for n in ([names] if isinstance(names, str) else names)]
        return (frozenset().union(*(i for i, _ in found)),
                frozenset().union(*(s for _, s in found)))  # fmt: skip

    (ia, sa), (ib, sb) = parts(a), parts(b)
    return sa == sb and (ia <= ib or ib <= ia)


def _groups(people: dict[str, str]) -> dict[str, list[str]]:
    by_key: dict[str, list[str]] = defaultdict(list)
    for cik, name in people.items():
        if key := name_key(name):
            by_key[key].append(cik)
    return {k: sorted(v) for k, v in by_key.items() if len(v) > 1}


# Entities the edgar-persons-pool classifier (v2) lets through: Danish ApS, Norwegian AS,
# French SAS and SCS, mutual holding companies, grantor trusts, SPAC sponsors.
_NOT_A_PERSON = re.compile(
    r"\d|\b(APS|AS|SAS|SCS|MHC|GRAT|SPONSOR|FCPR|UA|INVESTORS|L P)\b", re.IGNORECASE
)
# A former name can be a trust or company the CIK once filed as.
_NOT_A_NAME = re.compile(
    r"\b(TRUST|TRUSTEE|LLC|INC|CORP|LP|LTD|FUND|PARTNERS|CAPITAL|HOLDINGS|BANCSHARES)\b",
    re.IGNORECASE,
)


def _pairs(d: dict[str, Any]) -> dict[str, list[tuple[str, str]]]:
    """Every pair of people with one name key, by what the audit found such pairs to be
    (design 09 §11): conflicting names are different people; compatible names at a shared
    issuer are one person with two CIKs; at a shared city only, either; otherwise
    different people."""
    people, names, issuers, places = d["people"], d["names"], d["issuers"], d["places"]
    out: dict[str, list[tuple[str, str]]] = {k: [] for k in
                                             ("conflicting", "issuer", "city", "neither")}  # fmt: skip
    for ciks in _groups(people).values():
        for i, a in enumerate(ciks):
            for b in ciks[i + 1 :]:
                if not compatible(names.get(a) or {people[a]}, names.get(b) or {people[b]}):
                    out["conflicting"].append((a, b))
                elif issuers[a] & issuers[b]:
                    out["issuer"].append((a, b))
                elif places[a] & places[b]:
                    out["city"].append((a, b))
                else:
                    out["neither"].append((a, b))
    return out


@dataclass(frozen=True)
class Truth:
    """Who is who: each kept CIK's person (the smallest CIK of the people merged into
    one), and each left-out CIK with its reason."""

    person_of: dict[str, str]
    excluded: dict[str, str]


def truth(db: Path = CACHE / "edgar.duckdb", *, data: dict[str, Any] | None = None) -> Truth:
    """The CIK as truth, with the audit's corrections (design 09 §5, decision 6):
    left out are names that are not people, CIKs whose names share no word (two people,
    or one whose names cannot be told from two), and pairs of namesakes whose only link
    is a city; merged are compatible names at a shared issuer."""
    d = data or _load(db)
    people, names = d["people"], d["names"]
    excluded: dict[str, str] = {}
    for cik, name in people.items():
        if _NOT_A_PERSON.search(name):
            excluded[cik] = "not_a_person"
        elif _mixed(names.get(cik, set())):
            excluded[cik] = "unrelated_names"
    pairs = _pairs(d)
    for a, b in pairs["city"]:
        for cik in (a, b):
            excluded.setdefault(cik, "ambiguous_namesake")
    parent = {cik: cik for cik in people if cik not in excluded}

    def find(c: str) -> str:
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    for a, b in pairs["issuer"]:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            parent[max(ra, rb)] = min(ra, rb)
    return Truth({cik: find(cik) for cik in parent}, excluded)


def truth_report(db: Path = CACHE / "edgar.duckdb") -> dict[str, Any]:
    """How good the CIK is as truth (design 09 §5): counted, not assumed."""
    d = _load(db)
    people, names, issuers, places = d["people"], d["names"], d["issuers"], d["places"]
    spellings: dict[int, int] = defaultdict(int)
    for cik in people:
        spellings[min(len({spelling(n) for n in names.get(cik, ())}), 3)] += 1
    groups = _groups(people)
    pairs = _pairs(d)
    t = truth(data=d)
    reasons: dict[str, int] = defaultdict(int)
    for reason in t.excluded.values():
        reasons[reason] += 1
    return {
        "ciks": len(people),
        "spellings_per_cik": {
            ("3+" if k == 3 else str(k)): v for k, v in sorted(spellings.items())
        },
        "with_former_names": len(d["former"]),
        "issuers_per_cik": _histogram([len(issuers[c]) for c in people], cap=5),
        "with_a_city": sum(1 for c in people if places[c]),
        "with_generational_suffix": sum(1 for c in people if _parts(people[c])[1]),
        "shared_name_groups": len(groups),
        "ciks_in_shared_names": sum(len(v) for v in groups.values()),
        "shared_name_pairs": {k: len(v) for k, v in pairs.items()},
        "excluded": dict(sorted(reasons.items())),
        "ciks_kept": len(t.person_of),
        "people": len(set(t.person_of.values())),
    }


def _histogram(values: list[int], cap: int) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for v in values:
        out[f"{cap}+" if v >= cap else str(v)] += 1
    return dict(sorted(out.items()))


def audit(db: Path = CACHE / "edgar.duckdb", *, n: int = 40, seed: int = 1) -> dict[str, Any]:
    """Samples to read (design 09 §5), one list per kind of doubt about the truth."""
    d = _load(db)
    people, names, roles, places = d["people"], d["names"], d["roles"], d["places"]
    rng = _random.Random(seed)

    def describe(cik: str) -> dict[str, Any]:
        return {"cik": cik, "names": sorted(names.get(cik, ())),
                "former": d["former"].get(cik, []),
                "issuers": sorted({r[1] or "" for r in roles[cik]})[:6],
                "titles": sorted({r[2] for r in roles[cik] if r[2]})[:4],
                "places": sorted(places[cik], key=str)[:4]}  # fmt: skip

    def sample(items: list) -> list:
        return rng.sample(items, min(n, len(items)))

    mixed = sorted(c for c in people if _mixed(names.get(c, set())))
    not_people = sorted(c for c, name in people.items() if _NOT_A_PERSON.search(name))
    pairs = _pairs(d)
    out: dict[str, Any] = {
        "unrelated_names": [describe(c) for c in sample(mixed)],
        "not_a_person": [people[c] for c in sample(not_people)],
    }
    for kind in ("issuer", "city", "neither", "conflicting"):
        out[f"pairs_{kind}"] = [{"a": describe(a), "b": describe(b)}
                                for a, b in sample(pairs[kind])]  # fmt: skip
    return out


# -- the benchmark (design 09 §4) -------------------------------------------------------

GENERATOR_VERSION = "1"
MODES = ("hidden", "partial")
US_STATES = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "DC",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "PR",
    ]
)


@dataclass(frozen=True)
class Record:
    record_id: str
    person_id: str
    attributes: dict
    source: str


@dataclass
class Benchmark:
    seed: int
    records: list[Record]
    people: dict[str, dict]  # person id -> kind, ciks, name
    mode: str = "hidden"
    entity_type: str = "person"
    kind: dict[str, str] = field(init=False)
    truth: dict[str, str] = field(init=False)  # record id -> person id
    group: dict[str, str | None] = field(init=False)

    def __post_init__(self) -> None:
        self.kind = {p: info["kind"] for p, info in self.people.items()}
        self.group = {p: None for p in self.people}
        self.truth = {r.record_id: r.person_id for r in self.records}

    @property
    def config(self) -> dict:
        from collections import Counter

        return {"generator": "edgar", "version": GENERATOR_VERSION, "seed": self.seed,
                "mode": self.mode, "people": len(self.people), "records": len(self.records),
                "kinds": dict(Counter(self.kind.values())),
                "sources": dict(Counter(r.source for r in self.records))}  # fmt: skip


def given_first(name: str) -> str:
    """An EDGAR name as most other systems write it: "SMITH JOHN A JR" -> "John A Smith
    Jr". Compound family names come out wrong ("Pineyro Sublett Carla" -> "Sublett Carla
    Pineyro"), as they do when a person retypes them; the profile has to cope."""
    words = _strip_marker(name).split()
    suffixes = [w for w in words[1:] if w.upper().strip(".") in SUFFIXES]
    rest = [w for w in words if w not in suffixes]
    if len(rest) < 2:
        return " ".join(words)
    ordered = [w.capitalize() if w.isupper() and len(w) > 2 else w for w in rest[1:] + rest[:1]]
    return " ".join(ordered + suffixes)


def _kind(cik: str, d: dict[str, Any], keys: set[str]) -> str:
    if name_key(d["people"][cik]) in keys:
        return "namesake"
    if d["former"].get(cik):
        return "renamed"
    if _parts(d["people"][cik])[1]:
        return "suffix"
    return "multi_issuer" if len(d["issuers"][cik]) > 1 else "plain"


def generate(db: Path = CACHE / "edgar.duckdb", n: int = 1000, seed: int = 20261016, *,
             mode: str = "hidden") -> Benchmark:  # fmt: skip
    """About ``n`` people (design 09 §4): half from shared-name groups with every
    namesake, an eighth renamed, an eighth with a generational suffix, the rest at
    random. Each person has their EDGAR records (every spelling and former name, family
    first) and one record from another system, given first."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    d = _load(db)
    t = truth(data=d)
    rng = _random.Random(seed)
    members: dict[str, list[str]] = defaultdict(list)
    for cik, person in sorted(t.person_of.items()):
        members[person].append(cik)
    groups = defaultdict(set)
    for person in members:
        if key := name_key(d["people"][person]):
            groups[key].add(person)
    shared = {k: sorted(v) for k, v in groups.items() if len(v) > 1}
    chosen: list[str] = []
    seen: set[str] = set()

    def take(people: list[str], limit: int) -> None:
        for p in people:
            if len(chosen) >= limit:
                return
            if p not in seen:
                seen.add(p)
                chosen.append(p)

    for key in rng.sample(sorted(shared), len(shared)):
        if len(chosen) >= n // 2:
            break
        take(shared[key], n)
    pool = sorted(members)
    rng.shuffle(pool)
    take([p for p in pool if any(d["former"].get(c) for c in members[p])], len(chosen) + n // 8)
    take([p for p in pool if _parts(d["people"][p])[1]], len(chosen) + n // 8)
    take(pool, n)

    keys = set(shared)
    records: list[Record] = []
    people: dict[str, dict] = {}
    for person in chosen:
        ciks = members[person]
        people[person] = {"kind": _kind(person, d, keys), "ciks": ciks,
                          "name": d["people"][person]}  # fmt: skip
        for cik in ciks:
            roles = sorted(d["roles"][cik], key=lambda r: r[5] or "")  # by last date
            cities = sorted(d["places"][cik], key=str)
            spellings: dict[str, str] = {}
            for name in sorted(d["names"].get(cik, ()), key=lambda s: (s.isupper(), s)):
                spellings.setdefault(spelling(name), name)
            for name in d["former"].get(cik, []):
                if not _NOT_A_NAME.search(name) and not _NOT_A_PERSON.search(name):
                    spellings.setdefault(spelling(name), name)
            for i, name in enumerate(spellings.values()):
                attrs: dict[str, Any] = {"name": _strip_marker(name).strip(),
                                         "name_order": "family_first"}  # fmt: skip
                if roles:
                    attrs["employer"] = roles[-1][1]
                if cities:
                    city, state = rng.choice(cities)
                    attrs["city"] = city.strip(" ,").title()
                    if state in US_STATES:
                        attrs["country"] = "US"
                if mode == "partial":
                    attrs["identifiers"] = [f"CIK:{cik}"]
                records.append(Record(f"edgar-{cik}-{i}", person, attrs, "edgar"))
        cik = rng.choice(ciks)
        roles = d["roles"][cik]
        attrs = {"name": given_first(d["people"][cik])}
        if roles and rng.random() < 0.8:
            attrs["employer"] = rng.choice(roles)[1]
        if d["places"][cik] and rng.random() < 0.6:
            attrs["city"] = rng.choice(sorted(d["places"][cik], key=str))[0].strip(" ,").title()
        records.append(Record(f"other-{person}", person, attrs, "other"))
    rng.shuffle(records)
    return Benchmark(seed, records, people, mode)


def standard(n: int = 1000, seed: int = 20261016, *, mode: str = "hidden") -> Benchmark:
    return generate(CACHE / "edgar.duckdb", n, seed, mode=mode)
