"""A synthetic organization benchmark with ground truth (PRD §47, design 03 §14).

Deterministic for a seed. Promoted from the design 03 sketch, with the traps the
pipeline must handle:

- common names: many different companies named "Premier Services";
- heavy suppliers: a few companies with dozens of records;
- group companies: a holding company and subsidiaries sharing a brand and often a
  domain, which are related but never the same entity;
- acronyms: three-word names that also appear as their initials;
- typos, OCR substitutions, abbreviations, legal-form variants, casing, country
  spellings, and missing fields; 1% identifier typos.

Proportions follow the design 03 dataset; ``n_orgs`` scales the whole set.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field, replace
from typing import ClassVar

SYLLABLES = [
    "ac",
    "me",
    "ap",
    "ex",
    "sum",
    "mit",
    "pi",
    "on",
    "at",
    "las",
    "bea",
    "con",
    "co",
    "balt",
    "del",
    "ta",
    "ev",
    "er",
    "gran",
    "ite",
    "har",
    "bor",
    "iron",
    "wood",
    "ju",
    "ni",
    "per",
    "key",
    "stone",
    "lib",
    "mer",
    "id",
    "nor",
    "or",
    "red",
    "ster",
    "ling",
    "ti",
    "tan",
    "vert",
    "wil",
    "low",
    "zen",
    "ith",
    "kra",
    "vo",
    "lum",
    "sen",
    "dar",
    "quin",
    "bel",
    "mar",
    "tor",
    "vex",
    "cal",
    "dor",
    "fen",
    "gal",
    "hol",
    "jas",
    "kel",
    "lan",
    "mon",
    "nev",
    "pol",
    "ros",
    "sal",
    "tev",
    "ul",
    "var",
    "wen",
    "yor",
    "zal",
    "bri",
    "cor",
    "dun",
    "eld",
    "fair",
    "glen",
]
INDUSTRIES = [
    "Logistics", "Supply", "Industries", "Manufacturing", "Technologies", "Foods",
    "Packaging", "Plastics", "Chemicals", "Consulting", "Electric", "Systems",
    "Solutions", "Materials", "Freight", "Services",
]  # fmt: skip
GEO = ["Northern", "Southern", "Atlantic", "Pacific", "Global", "United", "American",
       "National", "Western", "Eastern", "Central", "Continental"]  # fmt: skip
ABBREVIATIONS = {"Technologies": "Tech", "Manufacturing": "Mfg", "International": "Intl",
                 "Services": "Svcs", "Industries": "Inds", "Solutions": "Soln"}  # fmt: skip
LEGAL = {"US": ["Inc", "LLC", "Corp"], "GB": ["Ltd", "PLC", "LLP"], "DE": ["GmbH", "AG"],
         "CA": ["Inc", "Ltd"]}  # fmt: skip
LEGAL_SPELLINGS = {"Inc": ["Inc.", "Incorporated"], "Corp": ["Corporation", "Corp."],
                   "Ltd": ["Limited", "Ltd."], "LLC": ["L.L.C."], "PLC": ["plc"],
                   "LLP": ["L.L.P."], "GmbH": ["GMBH"], "AG": ["A.G."]}  # fmt: skip
CITIES = {"US": ["Chicago", "Houston", "Phoenix", "Columbus", "Denver", "Atlanta", "Dallas"],
          "GB": ["London", "Leeds", "Bristol", "Glasgow", "Manchester"],
          "DE": ["Berlin", "Hamburg", "Munich", "Cologne", "Frankfurt"],
          "CA": ["Toronto", "Calgary", "Ottawa", "Winnipeg"]}  # fmt: skip
COUNTRY_SPELLINGS = {"US": ["US", "USA", "United States"], "GB": ["GB", "UK", "United Kingdom"],
                     "DE": ["DE", "Germany", "Deutschland"], "CA": ["CA", "Canada"]}  # fmt: skip
REG_SCHEME = {"US": "EIN", "GB": "CRN", "DE": "HRB", "CA": "BN"}
OCR = [("o", "0"), ("l", "1"), ("m", "rn"), ("e", "c"), ("i", "l")]

KINDS = ("normal", "common", "heavy", "family", "acronym")


@dataclass(frozen=True)
class Organization:
    org_id: str
    kind: str
    tokens: tuple[str, ...]
    legal: str
    country: str
    city: str
    postal: str
    domain: str | None
    reg_id: str | None
    group: str | None = None  # group companies share a group id


@dataclass(frozen=True)
class Record:
    record_id: str
    org_id: str
    attributes: dict


@dataclass
class Benchmark:
    entity_type: ClassVar[str] = "organization"
    seed: int
    organizations: list[Organization]
    records: list[Record]
    variant: str = ""  # "names_only" for the supplier-names benchmark
    kind: dict[str, str] = field(init=False)
    truth: dict[str, str] = field(init=False)  # record id -> org id
    group: dict[str, str | None] = field(init=False)  # org id -> group, for related pairs

    def __post_init__(self) -> None:
        self.kind = {o.org_id: o.kind for o in self.organizations}
        self.group = {o.org_id: o.group for o in self.organizations}
        self.truth = {r.record_id: r.org_id for r in self.records}

    @property
    def config(self) -> dict:
        config = {"generator": "organizations", "version": GENERATOR_VERSION, "seed": self.seed,
                  "orgs": len(self.organizations), "records": len(self.records)}  # fmt: skip
        return config | ({"variant": self.variant} if self.variant else {})


GENERATOR_VERSION = "1"


def _organizations(rng: random.Random, n_orgs: int) -> list[Organization]:
    scale = n_orgs / 805  # the design 03 dataset
    counts = {
        "common": max(2, round(25 * scale)),
        "heavy": max(1, round(5 * scale)),
        "family_groups": max(1, round(20 * scale)),
        "acronym": max(1, round(15 * scale)),
    }
    counts["normal"] = max(
        1, n_orgs - counts["common"] - counts["heavy"] - 3 * counts["family_groups"]
        - counts["acronym"]
    )  # fmt: skip
    orgs: list[Organization] = []
    used: set[str] = set()

    def brand() -> str:
        for _ in range(10_000):
            b = (rng.choice(SYLLABLES) + rng.choice(SYLLABLES)).capitalize()
            if b not in used and len(b) > 3:
                used.add(b)
                return b
        # Larger sets exhaust two-syllable brands; add a third syllable.
        while True:
            b = "".join(rng.choice(SYLLABLES) for _ in range(3)).capitalize()
            if b not in used:
                used.add(b)
                return b

    def add(tokens, kind, *, country=None, domain="auto", group=None):
        country = country or rng.choice(sorted(LEGAL))
        if domain == "auto":
            domain = f"{tokens[0].lower()}-{tokens[-1].lower()}.com" if rng.random() < 0.8 else None
        reg_id = (
            f"{REG_SCHEME[country]}:{rng.randrange(10**7, 10**8)}" if rng.random() < 0.7 else None
        )
        orgs.append(
            Organization(
                org_id=f"org{len(orgs):05d}",
                kind=kind,
                tokens=tuple(tokens),
                legal=rng.choice(LEGAL[country]),
                country=country,
                city=rng.choice(CITIES[country]),
                postal=str(rng.randrange(10000, 99999)),
                domain=domain,
                reg_id=reg_id,
                group=group,
            )
        )

    for _ in range(counts["normal"]):
        add([brand(), rng.choice(INDUSTRIES)], "normal")
    for _ in range(counts["common"]):
        domain = f"premier{rng.randrange(100, 999)}.com" if rng.random() < 0.5 else None
        add(["Premier", "Services"], "common", domain=domain)
    for _ in range(counts["heavy"]):
        add([brand(), rng.choice(INDUSTRIES)], "heavy")
    for g in range(counts["family_groups"]):
        b = brand()
        country = rng.choice(sorted(LEGAL))
        shared = f"{b.lower()}.com"
        for industry in ["Holdings", *rng.sample(INDUSTRIES, 2)]:
            domain = shared if rng.random() < 0.6 else f"{b.lower()}-{industry.lower()}.com"
            add([b, industry], "family", country=country, domain=domain, group=f"group{g:04d}")
    for _ in range(counts["acronym"]):
        g1, g2 = rng.sample(GEO, 2)
        add([g1, g2, rng.choice(INDUSTRIES)], "acronym")
    return orgs


def _typo(word: str, rng: random.Random) -> str:
    if len(word) < 4:
        return word
    i = rng.randrange(1, len(word) - 1)
    op = rng.choice("sdi")
    c = rng.choice("abcdefghijklmnopqrstuvwxyz")
    if op == "s":
        return word[:i] + c + word[i + 1 :]
    if op == "d":
        return word[:i] + word[i + 1 :]
    return word[:i] + c + word[i:]


def _ocr(word: str, rng: random.Random) -> str:
    options = [(a, b) for a, b in OCR if a in word]
    if not options:
        return word
    a, b = rng.choice(options)
    return word.replace(a, b, 1)


def _record(org: Organization, rng: random.Random, record_id: str) -> Record:
    tokens = list(org.tokens)
    r = rng.random()
    if org.kind == "acronym" and r < 0.3:
        tokens = ["".join(t[0] for t in tokens).upper()]
    elif r < 0.45:
        tokens = [ABBREVIATIONS.get(t, t) for t in tokens]
    if rng.random() < 0.15:
        j = rng.randrange(len(tokens))
        tokens[j] = _typo(tokens[j], rng)
    if rng.random() < 0.03:
        j = rng.randrange(len(tokens))
        tokens[j] = _ocr(tokens[j], rng)
    legal_r = rng.random()
    legal = (
        org.legal
        if legal_r < 0.55
        else rng.choice(LEGAL_SPELLINGS.get(org.legal, [org.legal]))
        if legal_r < 0.75
        else None
    )
    name = " ".join(tokens + ([legal] if legal else []))
    if rng.random() < 0.2:
        name = name.upper()
    if rng.random() < 0.1 and legal:
        name = name.replace(" ", ", ", 1)
    attributes: dict = {"name": name}
    if org.reg_id and rng.random() < 0.4:
        reg = org.reg_id
        if rng.random() < 0.01:
            reg = reg[:-1] + str((int(reg[-1]) + 1) % 10)
        attributes["registration_ids"] = [reg]
    if org.domain and rng.random() < 0.5:
        if rng.random() < 0.6:
            attributes["website"] = f"https://www.{org.domain}/"
        else:
            attributes["email"] = f"ap@{org.domain}"
    if rng.random() < 0.9:
        attributes["country"] = rng.choice(COUNTRY_SPELLINGS[org.country])
    if rng.random() < 0.7:
        city = org.city
        attributes["city"] = _typo(city, rng) if rng.random() < 0.05 else city
    if rng.random() < 0.5:
        attributes["postal_code"] = org.postal
    return Record(record_id, org.org_id, attributes)


def generate(n_orgs: int = 805, seed: int = 20261007) -> Benchmark:
    rng = random.Random(seed)
    orgs = _organizations(rng, n_orgs)
    records: list[Record] = []
    for org in orgs:
        n = rng.randint(25, 40) if org.kind == "heavy" else min(1 + int(rng.expovariate(0.7)), 6)
        for _ in range(n):
            records.append(_record(org, rng, f"rec{len(records):06d}"))
    return Benchmark(seed, orgs, records)


def names_only(benchmark, *, account_numbers: float = 0.25, seed: int = 0):
    """The supplier-names benchmark (design 05 §7): the same organizations and records with
    every attribute but the name removed, and a vendor or account number appended to a
    quarter of the names ("Harjas Foods PLC - 812773"), as in AP and ERP extracts. Works
    on the synthetic benchmark and on the UK one (design 07)."""
    rng = random.Random(f"{seed}|names_only")
    records = []
    for r in benchmark.records:
        name = r.attributes["name"]
        if rng.random() < account_numbers:
            number = rng.randrange(100_000, 999_999)
            name = rng.choice([f"{name} - {number}", f"{name} {number}", f"{name} #{number}"])
        records.append(replace(r, attributes={"name": name}))
    if not isinstance(benchmark, Benchmark):
        return replace(benchmark, records=records, variant="names_only")
    return Benchmark(benchmark.seed, benchmark.organizations, records, variant="names_only")
