"""The taxonomy benchmark (design 06 §5): real plant names, real ground truth.

Names and their synonymy come from the World Flora Online Plant List (WFO, CC0),
read family by family from ChecklistBank, where WFO publishes it (dataset 2004,
"World Flora Online Plant List 2026-06"). Ground truth is WFO's: each name belongs to
the accepted taxon WFO lists it under. Nothing about which names go together is
generated here.

What is generated is how names appear in records: the accepted name or a synonym
(outdated names in current use are the commonest real error), with or without
authorship, the authorship spelled another way or its basionym author dropped, the
genus abbreviated, a typo, capitals, rank markers written variously. The traps are the
data's own: homonyms, synonyms from another genus, congeneric species, gender endings.

A small fixture of three families ships with Thisness for tests; other families are
fetched on demand and cached under ``~/.cache/thisness/wfo``.

A second authority (design 06 §3, step 3): the Catalogue of Life's latest release, also
read from ChecklistBank. Its plants come from World Plants, an independent checklist,
so where it and WFO disagree the disagreement is real. CoL is CC BY 4.0: it is fetched
on demand with its citation written beside the cache, and never shipped. Its rows are
aligned to WFO's names, and WFO stays the truth: a CoL row whose accepted name puts it
in another WFO taxon is dissent, which the engine should surface, not settle.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import random
import re
import ssl
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from thisness.benchmark.organizations import _typo
from thisness.normalize.taxon import parse

GENERATOR_VERSION = "1"
DATASET = 2004  # ChecklistBank: World Flora Online Plant List 2026-06, CC0
DATASET_TITLE = "World Flora Online Plant List 2026-06"
API = "https://api.checklistbank.org"
CACHE = Path.home() / ".cache" / "thisness" / "wfo"
COL = "3LR"  # ChecklistBank's alias for the latest Catalogue of Life release, CC BY 4.0
COL_CACHE = CACHE.parent / "col"
RANKS = ("species", "subspecies", "variety", "form")
ACCEPTED = ("accepted", "provisionally accepted")
SYNONYMS = ("synonym", "ambiguous synonym")
FIXTURE_FAMILIES = ("Pinaceae", "Betulaceae", "Juglandaceae")
BENCHMARK_FAMILIES = ("Fagaceae", "Sapindaceae", "Oleaceae", "Salicaceae", "Cupressaceae")
STARTER_FAMILIES = ("Ranunculaceae", "Ericaceae")
FIELDS = (
    "id",
    "name",
    "authorship",
    "rank",
    "status",
    "accepted_id",
    "family",
    "genus",
    "accepted_name",
)
RANK_MARKERS = {"subspecies": ["subsp.", "ssp.", "subsp"], "variety": ["var.", "var", "v."],
                "form": ["f.", "forma", "fo."]}  # fmt: skip
AUTHOR_SPELLINGS = {"L.": ["Linn.", "L", "Linnaeus"], "Mill.": ["Miller"],
                    "DC.": ["De Candolle"], "Sw.": ["Swartz"], "Willd.": ["Willdenow"]}  # fmt: skip
SOURCES = ("herbarium", "trade", "field", "literature")


@dataclass(frozen=True)
class NameRow:
    id: str
    name: str  # without authorship
    authorship: str
    rank: str
    status: str
    accepted_id: str  # the row's own id when accepted
    family: str
    genus: str
    accepted_name: str = ""  # with authorship; for a synonym, as its authority writes it


# -- fetching ---------------------------------------------------------------------------------


def _context() -> ssl.SSLContext:
    """certifi's roots when installed (some Python builds ship none), else the system's."""
    try:
        import certifi
    except ImportError:  # pragma: no cover - depends on the environment
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _get(path: str, params: dict) -> dict:
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": "thisness-benchmark/1"})
    with urllib.request.urlopen(request, timeout=60, context=_context()) as response:
        return json.load(response)


def fetch_family(family: str, *, dataset: int | str = DATASET, cache: Path = CACHE,
                 pause: float = 0.5) -> list[NameRow]:  # fmt: skip
    """Every species-level name in a family, accepted or synonym, cached as CSV."""
    path = cache / f"{family}.csv.gz"
    if path.exists():
        return read_rows(path)
    found = [r for r in _get(f"/dataset/{dataset}/nameusage/search",
                             {"q": family, "rank": "family", "limit": 10})["result"]
             if r["usage"]["name"]["scientificName"] == family
             and r["usage"].get("status") in ACCEPTED]  # fmt: skip
    if not found:
        raise ValueError(f"no family {family!r} in dataset {dataset}")
    family_id = found[0]["usage"]["id"]
    rows, offset = [], 0
    while True:
        page = _get(f"/dataset/{dataset}/nameusage/search",
                    {"TAXON_ID": family_id, "limit": 1000, "offset": offset})  # fmt: skip
        for result in page.get("result", []):
            usage, name = result["usage"], result["usage"]["name"]
            rank, status = name.get("rank"), usage.get("status")
            if rank not in RANKS or status not in ACCEPTED + SYNONYMS:
                continue
            accepted = (
                usage["id"] if status in ACCEPTED else (usage.get("accepted") or {}).get("id")
            )
            if not accepted:
                continue
            authorship = name.get("authorship") or ""
            if status in ACCEPTED:
                accepted_name = f"{name['scientificName']} {authorship}".strip()
            else:
                accepted_name = (usage.get("accepted") or {}).get("label") or ""
            rows.append(NameRow(id=usage["id"], name=name["scientificName"],
                                authorship=authorship, rank=rank,
                                status="accepted" if status in ACCEPTED else "synonym",
                                accepted_id=accepted, family=family,
                                genus=name.get("genus") or name["scientificName"].split()[0],
                                accepted_name=accepted_name))  # fmt: skip
        offset += 1000
        if offset >= page.get("total", 0) or not page.get("result"):
            break
        time.sleep(pause)
    write_rows(rows, path)
    return rows


def write_rows(rows: list[NameRow], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(FIELDS)
        writer.writerows([[getattr(r, k) for k in FIELDS] for r in rows])


def read_rows(source: Path | io.TextIOBase) -> list[NameRow]:
    if isinstance(source, Path):
        with gzip.open(source, "rt", encoding="utf-8") as f:
            return [NameRow(**row) for row in csv.DictReader(f)]
    return [NameRow(**row) for row in csv.DictReader(source)]


def fixture() -> list[NameRow]:
    """The shipped CC0 fixture: three small families, for tests."""
    resource = resources.files("thisness.benchmark") / "data" / "wfo_fixture.csv.gz"
    return read_rows(io.TextIOWrapper(gzip.open(resource.open("rb")), encoding="utf-8"))


def families(names: tuple[str, ...], **kwargs) -> list[NameRow]:
    return [row for family in names for row in fetch_family(family, **kwargs)]


_RELEASE: dict[str, str] = {}


def col_release() -> dict[str, str]:
    """The latest Catalogue of Life release: its dataset key, version and citation."""
    if not _RELEASE:
        d = _get(f"/dataset/{COL}", {})
        citation = re.sub(r"<[^>]+>", "", d.get("citation") or "")
        for entity, char in (("&aacute;", "á"), ("&ouml;", "ö"), ("&Ouml;", "Ö"), ("&amp;", "&")):
            citation = citation.replace(entity, char)
        _RELEASE.update(key=str(d["key"]), version=str(d.get("version")),
                        alias=str(d.get("alias") or d["key"]), citation=citation.strip(),
                        license=str(d.get("license")))  # fmt: skip
    return _RELEASE


def col_families(names: tuple[str, ...], *, cache: Path = COL_CACHE) -> list[NameRow]:
    """The Catalogue of Life's names for these families, cached per release with the
    citation CC BY requires. Never shipped (design 06 decision 6)."""
    release = col_release()
    target = cache / release["alias"]
    target.mkdir(parents=True, exist_ok=True)
    attribution = target / "ATTRIBUTION.txt"
    if not attribution.exists():
        attribution.write_text(f"{release['citation']}\nLicence: {release['license']}\n",
                               encoding="utf-8")  # fmt: skip
    return families(names, dataset=release["key"], cache=target)


def _name_key(name: str, authorship: str = "") -> tuple[str, str | None] | None:
    parsed = parse(f"{name} {authorship}".strip())
    return (parsed.canonical, parsed.authors) if parsed is not None else None


def align(rows: list[NameRow], reference: list[NameRow]) -> dict[str, NameRow]:
    """Each row's name in the reference: the same canonical name, and the same combining
    author when the reference has homonyms (a missing author matches only a unique
    name). Rows with no single match are left out."""
    index: dict[str, list[tuple[str | None, NameRow]]] = defaultdict(list)
    for r in reference:
        key = _name_key(r.name, r.authorship)
        if key is not None:
            index[key[0]].append((key[1], r))
    found: dict[str, NameRow] = {}
    for row in rows:
        match = _lookup(index, row.name, row.authorship)
        if match is not None:
            found[row.id] = match
    return found


def _lookup(index, name: str, authorship: str = "") -> NameRow | None:
    key = _name_key(name, authorship)
    if key is None:
        return None
    candidates = index.get(key[0], [])
    if len(candidates) > 1 and key[1]:
        candidates = [c for c in candidates if c[0] == key[1]]
    if len(candidates) != 1:
        return None
    authors, match = candidates[0]
    if not key[1] or not authors or authors == key[1]:
        return match
    # One name with that spelling: authors written differently ("Hook.f." and "Hook.",
    # "(Lindl.) Benth." and "Benth.") still name it when they share an author.
    return match if set(authors.split("&")) & set(key[1].split("&")) else None


def _species(name: str) -> str:
    return " ".join(name.replace("\u00d7", "").split()[:2]).casefold()


def _accepted_in(index, row: NameRow, own: NameRow) -> str | None:
    """The reference taxon a row's own authority puts it in: its own match when the
    authority accepts it, else the match of the accepted name it gives."""
    if row.status == "accepted":
        return own.accepted_id
    match = _lookup(index, row.accepted_name)
    return match.accepted_id if match is not None else None


# -- the benchmark ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    record_id: str
    taxon_id: str
    attributes: dict
    source: str = "benchmark"


@dataclass
class Benchmark:
    seed: int
    rows: list[NameRow]
    records: list[Record]
    families: tuple[str, ...]
    entity_type: str = "taxon"
    # Records whose authority places them in another reference taxon: "placement" (another
    # taxon) or "rank" (the same species at another rank, as an autonym variety).
    dissent: dict[str, str] = field(default_factory=dict)
    unresolved: int = 0  # authority rows whose accepted name has no single reference match
    authorities: tuple[str, ...] = ()
    col_release: dict[str, str] | None = None
    kind: dict[str, str] = field(init=False)
    truth: dict[str, str] = field(init=False)  # record id -> accepted taxon id
    group: dict[str, str | None] = field(init=False)  # taxon id -> genus (related: congeneric)

    def __post_init__(self) -> None:
        by_id = {r.id: r for r in self.rows}
        names: dict[str, set[str]] = defaultdict(set)
        for r in self.rows:
            names[r.accepted_id].add(r.name.casefold())
        owners = Counter(n for taxon_names in names.values() for n in taxon_names)
        self.truth = {r.record_id: r.taxon_id for r in self.records}
        taxa = set(self.truth.values())
        self.kind = {}
        disputed = {self.truth[r] for r in self.dissent}
        for t in taxa:
            accepted = by_id.get(t)
            if t in disputed:
                self.kind[t] = "disputed"  # a second authority places one of its names elsewhere
            elif any(owners[n] > 1 for n in names[t]):
                self.kind[t] = "homonym"  # one of its names also names another taxon
            elif accepted is not None and accepted.rank != "species":
                self.kind[t] = "infraspecific"
            elif len(names[t]) > 1:
                self.kind[t] = "synonymized"
            else:
                self.kind[t] = "plain"
        self.group = {t: by_id[t].genus if t in by_id else None for t in taxa}

    @property
    def config(self) -> dict:
        config = {"generator": "wfo", "version": GENERATOR_VERSION, "dataset": DATASET_TITLE,
                  "families": list(self.families), "seed": self.seed,
                  "taxa": len(set(self.truth.values())), "records": len(self.records)}  # fmt: skip
        if self.authorities:
            config["authorities"] = list(self.authorities)
        if self.col_release:
            config["col"] = {k: self.col_release[k] for k in ("key", "version", "license")}
            config["dissent"] = dict(Counter(self.dissent.values()))
            config["unresolved"] = self.unresolved
        return config


def _variant(row: NameRow, rng: random.Random) -> str:
    """One way a record might write this name."""
    parsed = parse(row.name)
    text = row.name
    if row.rank in RANK_MARKERS and rng.random() < 0.3:
        for marker in ("subsp.", "var.", "f."):
            if f" {marker} " in text:
                text = text.replace(f" {marker} ", f" {rng.choice(RANK_MARKERS[row.rank])} ", 1)
    words = text.split()
    if len(words) >= 2 and rng.random() < 0.06:
        i = rng.choice([1, len(words) - 1])
        words[i] = _typo(words[i], rng)
    if len(words) >= 2 and rng.random() < 0.02:
        words[0] = _typo(words[0], rng)
    if len(words) >= 2 and parsed is not None and rng.random() < 0.08:
        words[0] = words[0][0] + "."
    text = " ".join(words)
    r = rng.random()
    if row.authorship and r < 0.55:
        authorship = row.authorship
        if rng.random() < 0.15:
            for standard, spellings in AUTHOR_SPELLINGS.items():
                # Whole authors only: the "L." in "S.L.Tung" is an initial, not Linnaeus.
                pattern = rf"(?<![\w.]){re.escape(standard)}(?![\w.])"
                authorship = re.sub(pattern, rng.choice(spellings), authorship)
        if authorship.startswith("(") and rng.random() < 0.2:
            authorship = authorship.split(")", 1)[1].strip()  # the basionym author dropped
        text = f"{text} {authorship}"
    if rng.random() < 0.08:
        text = text.upper()
    elif rng.random() < 0.04:
        text = text.lower()
    return text


def generate(rows: list[NameRow], n_taxa: int = 800, seed: int = 20261008,
             families: tuple[str, ...] = (), *, reference: bool = False,
             col: list[NameRow] | None = None,
             col_release: dict[str, str] | None = None) -> Benchmark:  # fmt: skip
    """Records for about ``n_taxa`` accepted taxa, whole genera at a time so congeneric
    species meet each other. With ``reference``, WFO's own rows for those taxa are
    observations too, from source ``wfo``, as a store holding the backbone would have
    them (design 06 §5). With ``col``, so are the Catalogue of Life's rows for the same
    names, from source ``col``: each row's truth is the WFO taxon of its name, and a row
    CoL places in another WFO taxon is dissent."""
    index: dict[str, list[tuple[str | None, NameRow]]] = defaultdict(list)
    col_by_name: dict[str, list[NameRow]] = defaultdict(list)
    if col:
        for r in rows:
            key = _name_key(r.name, r.authorship)
            if key is not None:
                index[key[0]].append((key[1], r))
        for col_id, match in align(col, rows).items():
            col_by_name[match.id].append(next(c for c in col if c.id == col_id))
    rng = random.Random(seed)
    accepted = [r for r in rows if r.status == "accepted"]
    synonyms: dict[str, list[NameRow]] = defaultdict(list)
    for r in rows:
        if r.status == "synonym":
            synonyms[r.accepted_id].append(r)
    by_genus: dict[str, list[NameRow]] = defaultdict(list)
    for r in sorted(accepted, key=lambda r: r.id):
        by_genus[r.genus].append(r)
    genera = sorted(by_genus)
    rng.shuffle(genera)
    chosen: list[NameRow] = []
    for genus in genera:
        if len(chosen) >= n_taxa:
            break
        members = by_genus[genus]
        rng.shuffle(members)
        chosen += members[: max(1, n_taxa - len(chosen))]
    records: list[Record] = []
    dissent: dict[str, str] = {}
    unresolved = 0
    by_id = {r.id: r for r in rows}
    for taxon in sorted(chosen, key=lambda r: r.id):
        names = [taxon, *sorted(synonyms.get(taxon.id, []), key=lambda r: r.id)]
        n = min(1 + int(rng.expovariate(0.55)), 8)
        for _ in range(n):
            row = taxon if len(names) == 1 or rng.random() < 0.5 else rng.choice(names[1:])
            records.append(Record(f"rec{len(records):06d}", taxon.id,
                                  {"name": _variant(row, rng)}, rng.choice(SOURCES)))  # fmt: skip
        if reference:
            accepted_name = f"{taxon.name} {taxon.authorship}".strip()
            for row in names:
                attributes = {"name": row.name, "authorship": row.authorship,
                              "rank": row.rank, "family": row.family,
                              "taxonomic_status": row.status,
                              "identifiers": [f"WFO:{row.id}"]}  # fmt: skip
                if row.status == "synonym":
                    attributes["accepted_name"] = accepted_name
                records.append(Record(f"rec{len(records):06d}", taxon.id, attributes, "wfo"))
        for own in names:
            for row in sorted(col_by_name.get(own.id, []), key=lambda r: r.id):
                attributes = {"name": row.name, "authorship": row.authorship,
                              "rank": row.rank, "family": row.family,
                              "taxonomic_status": row.status,
                              "identifiers": [f"COL:{row.id}"]}  # fmt: skip
                if row.status == "synonym" and row.accepted_name:
                    attributes["accepted_name"] = row.accepted_name
                record_id = f"rec{len(records):06d}"
                records.append(Record(record_id, taxon.id, attributes, "col"))
                placed = _accepted_in(index, row, own)
                if placed is None:
                    unresolved += 1
                elif placed != taxon.id:
                    same_species = _species(by_id[placed].name) == _species(taxon.name)
                    dissent[record_id] = "rank" if same_species else "placement"
    authorities = (("wfo",) if reference else ()) + (("col",) if col else ())
    return Benchmark(seed, rows, records, tuple(families or sorted({r.family for r in rows})),
                     dissent=dissent, unresolved=unresolved, authorities=authorities,
                     col_release=col_release)  # fmt: skip


def standard(n_taxa: int = 800, seed: int = 20261008, *, reference: bool = False,
             col: bool = False) -> Benchmark:  # fmt: skip
    """The default benchmark over BENCHMARK_FAMILIES (fetched once, then cached). With
    ``col``, the Catalogue of Life's rows too (fetched with attribution, never shipped)."""
    col_rows = col_families(BENCHMARK_FAMILIES) if col else None
    return generate(families(BENCHMARK_FAMILIES), n_taxa, seed, BENCHMARK_FAMILIES,
                    reference=reference, col=col_rows,
                    col_release=col_release() if col else None)  # fmt: skip


def from_fixture(n_taxa: int = 300, seed: int = 20261008, *, reference: bool = False) -> Benchmark:
    return generate(fixture(), n_taxa, seed, FIXTURE_FAMILIES, reference=reference)
