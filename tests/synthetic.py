"""A small, deterministic organization dataset with ground truth, for tests.

The benchmark generator (M1 step 6) supersedes this; it stays small so training
in tests takes well under a second.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# Distinctive brands, as most real company names are: a narrow vocabulary floods the
# candidates with different companies sharing a word, which is a different (and
# harsher) test than this small set is for.
SYLLABLES = ["ac", "me", "ap", "ex", "sum", "mit", "pi", "on", "at", "las", "bea", "con", "co", "balt", "del", "ta", "gran", "ite", "har", "bor", "iron", "wood", "ju", "ni", "per", "key", "stone", "lib", "mer", "id", "nor", "red", "ster", "ling", "ti", "tan", "vert", "wil", "low", "zen", "ith", "kra", "vo", "lum", "sen", "dar", "quin", "bel", "mar", "tor", "vex", "cal", "dor", "fen"]  # fmt: skip
INDUSTRIES = ["Logistics", "Supply", "Manufacturing", "Technologies", "Foods", "Packaging",
              "Plastics", "Chemicals", "Services", "Freight"]  # fmt: skip
ABBREV = {"Manufacturing": "Mfg", "Technologies": "Tech", "Services": "Svcs"}
CITIES = {"US": ["Chicago", "Houston", "Phoenix", "Denver"], "GB": ["London", "Leeds", "Bristol"],
          "DE": ["Berlin", "Hamburg", "Munich"]}  # fmt: skip
LEGAL = {"US": ["Inc", "LLC", "Corp"], "GB": ["Ltd", "PLC"], "DE": ["GmbH", "AG"]}


@dataclass
class Org:
    org_id: str
    name: str
    legal: str
    country: str
    city: str
    postal: str
    domain: str | None
    reg_id: str | None
    kind: str = "normal"


def organizations(seed: int = 0, n: int = 120) -> list[Org]:
    rng = random.Random(seed)
    brands: list[str] = []
    while len(brands) < n:
        brand = (rng.choice(SYLLABLES) + rng.choice(SYLLABLES)).capitalize()
        if len(brand) > 3 and brand not in brands:
            brands.append(brand)
    names = [f"{b} {rng.choice(INDUSTRIES)}" for b in brands]
    orgs = []

    def add(name, kind="normal", domain="auto"):
        country = rng.choice(list(LEGAL))
        brand, industry = name.split(" ", 1)
        orgs.append(
            Org(
                f"org{len(orgs):03d}", name, rng.choice(LEGAL[country]), country,
                rng.choice(CITIES[country]), str(rng.randrange(10000, 99999)),
                f"{brand.lower()}-{industry.lower()}.com" if domain == "auto" and rng.random() < 0.7
                else domain if domain != "auto" else None,
                f"EIN:{rng.randrange(10**8, 10**9)}" if rng.random() < 0.6 else None, kind,
            )
        )  # fmt: skip

    for name in names[:n]:
        add(name)
    for _ in range(8):  # one common name, eight different companies
        add("Premier Services", kind="common", domain=None)
    return orgs


def _typo(word: str, rng: random.Random) -> str:
    i = rng.randrange(1, len(word) - 1)
    return word[:i] + word[i + 1 :]


def observations(orgs: list[Org], seed: int = 0) -> list[tuple[str, dict]]:
    """(org_id, attributes) for 1-4 noisy observations of each organization."""
    rng = random.Random(seed + 1)
    out = []
    for org in orgs:
        for _ in range(rng.randint(1, 4)):
            tokens = org.name.split()
            if rng.random() < 0.3:
                tokens = [ABBREV.get(t, t) for t in tokens]
            if rng.random() < 0.15:
                j = rng.randrange(len(tokens))
                tokens[j] = _typo(tokens[j], rng) if len(tokens[j]) > 3 else tokens[j]
            name = " ".join(tokens + ([org.legal] if rng.random() < 0.6 else []))
            if rng.random() < 0.2:
                name = name.upper()
            attrs: dict = {"name": name}
            if org.reg_id and rng.random() < 0.4:
                attrs["registration_ids"] = [org.reg_id]
            if org.domain and rng.random() < 0.5:
                attrs["website"] = f"https://www.{org.domain}/"
            if rng.random() < 0.9:
                attrs["country"] = org.country
            if rng.random() < 0.7:
                attrs["city"] = org.city
            if rng.random() < 0.5:
                attrs["postal_code"] = org.postal
            out.append((org.org_id, attrs))
    return out
