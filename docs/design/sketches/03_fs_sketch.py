"""Sketch for docs/design/03-resolution-pipeline.md.

Not Thisness code. An end-to-end check, on synthetic organizations with known
ground truth, of the design 03 pipeline:

    normalize -> block (5 generators) -> compare (7 comparisons, levels)
    -> u from random pairs -> m and lambda by EM in blocking sessions
    -> Fellegi-Sunter weights (+ term-frequency adjustment, 4 variants)
    -> edge classes -> validated components (design 01) -> metrics

The dataset deliberately contains the traps the design must handle:
25 different companies named "Premier Services" (common name), 5 heavily
repeated suppliers (25-40 observations each), group companies sharing a
domain, acronyms, typos, abbreviations and legal-form variants.

    uv run --with rapidfuzz python docs/design/sketches/03_fs_sketch.py
"""

from __future__ import annotations

import math
import random
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

sys.path.insert(0, str(Path(__file__).parent))
from consensus_sketch import (
    components,
    load_bearing_articulations,
    load_bearing_bridges,
)

SEED = 20261007

# ---------------------------------------------------------------------------
# Synthetic organizations
# ---------------------------------------------------------------------------

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
    "Logistics",
    "Supply",
    "Industries",
    "Manufacturing",
    "Technologies",
    "Foods",
    "Packaging",
    "Plastics",
    "Chemicals",
    "Consulting",
    "Electric",
    "Systems",
    "Solutions",
    "Materials",
    "Freight",
    "Services",
]
GEO = [
    "Northern",
    "Southern",
    "Atlantic",
    "Pacific",
    "Global",
    "United",
    "American",
    "National",
    "Western",
    "Eastern",
    "Central",
    "Continental",
]
ABBREV = {
    "Technologies": "Tech",
    "Manufacturing": "Mfg",
    "International": "Intl",
    "Services": "Svcs",
    "Industries": "Inds",
    "Solutions": "Soln",
}
LEGAL = {
    "US": ["Inc", "LLC", "Corp"],
    "GB": ["Ltd", "PLC", "LLP"],
    "DE": ["GmbH", "AG"],
    "CA": ["Inc", "Ltd"],
}
LEGAL_SPELLINGS = {
    "Inc": ["Inc.", "Incorporated"],
    "Corp": ["Corporation", "Corp."],
    "Ltd": ["Limited", "Ltd."],
    "LLC": ["L.L.C."],
    "PLC": ["plc"],
    "LLP": ["L.L.P."],
    "GmbH": ["GMBH"],
    "AG": ["A.G."],
}
CITIES = {
    "US": ["Chicago", "Houston", "Phoenix", "Columbus", "Denver", "Atlanta", "Dallas"],
    "GB": ["London", "Leeds", "Bristol", "Glasgow", "Manchester"],
    "DE": ["Berlin", "Hamburg", "Munich", "Cologne", "Frankfurt"],
    "CA": ["Toronto", "Calgary", "Ottawa", "Winnipeg"],
}
REG_SCHEME = {"US": "EIN", "GB": "CRN", "DE": "HRB", "CA": "BN"}


@dataclass
class Org:
    org_id: str
    tokens: list[str]
    legal: str
    country: str
    city: str
    postal: str
    domain: str | None
    reg_id: str | None
    kind: str = "normal"  # normal | common | heavy | family | acronym


@dataclass
class Obs:
    obs_id: str
    org_id: str
    raw: dict[str, str | None]
    feat: dict[str, object] = field(default_factory=dict)


def make_orgs(rng: random.Random) -> list[Org]:
    orgs: list[Org] = []
    used: set[str] = set()

    def brand() -> str:
        while True:
            b = (rng.choice(SYLLABLES) + rng.choice(SYLLABLES)).capitalize()
            if b not in used and len(b) > 3:
                used.add(b)
                return b

    def org(tokens, kind, country=None, domain="auto", reg=True):
        country = country or rng.choice(list(LEGAL))
        oid = f"org{len(orgs):04d}"
        if domain == "auto":
            domain = (
                tokens[0].lower() + "-" + tokens[-1].lower() + ".com"
                if rng.random() < 0.8
                else None
            )
        reg_id = (
            f"{REG_SCHEME[country]}:{rng.randrange(10**7, 10**8)}"
            if reg and rng.random() < 0.7
            else None
        )
        o = Org(
            oid,
            tokens,
            rng.choice(LEGAL[country]),
            country,
            rng.choice(CITIES[country]),
            f"{rng.randrange(10000, 99999)}",
            domain,
            reg_id,
            kind,
        )
        orgs.append(o)
        return o

    for _ in range(700):
        org([brand(), rng.choice(INDUSTRIES)], "normal")
    for _ in range(25):  # one common name, 25 different companies
        o = org(["Premier", "Services"], "common", domain=None)
        if rng.random() < 0.5:
            o.domain = f"premier{rng.randrange(100, 999)}.com"
    for _ in range(5):
        org([brand(), rng.choice(INDUSTRIES)], "heavy")
    for _ in range(20):  # group companies: Holdings + two operating subsidiaries
        b = brand()
        country = rng.choice(list(LEGAL))
        shared = b.lower() + ".com"
        for ind in ["Holdings", *rng.sample(INDUSTRIES, 2)]:
            org(
                [b, ind],
                "family",
                country=country,
                domain=shared if rng.random() < 0.6 else f"{b.lower()}-{ind.lower()}.com",
            )
    for _ in range(15):
        g1, g2 = rng.sample(GEO, 2)
        org([g1, g2, rng.choice(INDUSTRIES)], "acronym")
    return orgs


def typo(s: str, rng: random.Random) -> str:
    if len(s) < 4:
        return s
    i = rng.randrange(1, len(s) - 1)
    op = rng.choice("sdi")
    c = rng.choice("abcdefghijklmnopqrstuvwxyz")
    return (
        s[:i] + c + s[i + 1 :]
        if op == "s"
        else s[:i] + s[i + 1 :]
        if op == "d"
        else s[:i] + c + s[i:]
    )


def make_obs(org: Org, n: int, rng: random.Random, start: int) -> list[Obs]:
    out = []
    for k in range(n):
        toks = list(org.tokens)
        r = rng.random()
        if org.kind == "acronym" and r < 0.3:
            toks = ["".join(t[0] for t in toks).upper()]
        elif r < 0.45:
            toks = [ABBREV.get(t, t) for t in toks]
        if rng.random() < 0.15:
            j = rng.randrange(len(toks))
            toks[j] = typo(toks[j], rng)
        legal_r = rng.random()
        legal = (
            org.legal
            if legal_r < 0.55
            else rng.choice(LEGAL_SPELLINGS.get(org.legal, [org.legal]))
            if legal_r < 0.75
            else None
        )
        name = " ".join(toks + ([legal] if legal else []))
        if rng.random() < 0.2:
            name = name.upper()
        if rng.random() < 0.1:
            name = name.replace(" ", ", ", 1) if legal else name
        reg = org.reg_id if org.reg_id and rng.random() < 0.4 else None
        if reg and rng.random() < 0.01:
            reg = reg[:-1] + str((int(reg[-1]) + 1) % 10)
        dom = org.domain if org.domain and rng.random() < 0.5 else None
        city = org.city if rng.random() < 0.7 else None
        if city and rng.random() < 0.05:
            city = typo(city, rng)
        out.append(
            Obs(
                f"obs{start + k:05d}",
                org.org_id,
                {
                    "name": name,
                    "reg_id": reg,
                    "domain": dom,
                    "country": org.country if rng.random() < 0.9 else None,
                    "city": city,
                    "postal": org.postal if rng.random() < 0.5 else None,
                },
            )
        )
    return out


def make_dataset(seed: int = SEED):
    rng = random.Random(seed)
    orgs = make_orgs(rng)
    obs: list[Obs] = []
    for o in orgs:
        n = rng.randint(25, 40) if o.kind == "heavy" else min(1 + int(rng.expovariate(0.7)), 6)
        obs += make_obs(o, n, rng, len(obs))
    return orgs, obs


# ---------------------------------------------------------------------------
# Normalizers (organization profile)
# ---------------------------------------------------------------------------

LEGAL_CANON = {
    "inc": "inc",
    "incorporated": "inc",
    "llc": "llc",
    "corp": "corp",
    "corporation": "corp",
    "ltd": "ltd",
    "limited": "ltd",
    "plc": "plc",
    "llp": "llp",
    "gmbh": "gmbh",
    "ag": "ag",
    "co": "co",
    "company": "co",
}
EXPAND = {
    "tech": "technologies",
    "mfg": "manufacturing",
    "intl": "international",
    "svcs": "services",
    "inds": "industries",
    "soln": "solutions",
}


def normalize(o: Obs) -> None:
    name = unicodedata.normalize("NFKC", o.raw["name"]).casefold().replace("&", " and ")
    name = re.sub(r"\b([a-z])\.(?=[a-z]\.)", r"\1", name)  # l.l.c. -> llc
    raw_tokens = re.findall(r"[a-z0-9]+", name)
    legal = [LEGAL_CANON[t] for t in raw_tokens if t in LEGAL_CANON]
    tokens = [EXPAND.get(t, t) for t in raw_tokens if t not in LEGAL_CANON]
    f = o.feat
    f["name.key"] = "".join(tokens)
    f["name.tokens"] = tokens
    f["name.acronym"] = "".join(t[0] for t in tokens) if len(tokens) >= 3 else None
    f["legal_form"] = legal[-1] if legal else None
    for a in ("reg_id", "domain", "country", "postal"):
        v = o.raw[a]
        f[a] = re.sub(r"\s+", "", v).lower() if v else None
    f["city"] = o.raw["city"].casefold() if o.raw["city"] else None


# ---------------------------------------------------------------------------
# Candidate generators
# ---------------------------------------------------------------------------


def block_on(obs: list[Obs], key, max_block: int) -> set[tuple[str, str]]:
    index = defaultdict(list)
    for o in obs:
        for v in key(o):
            index[v].append(o.obs_id)
    pairs = set()
    for ids in index.values():
        if 2 <= len(ids) <= max_block:
            pairs.update(combinations(sorted(ids), 2))
    return pairs


def generate(obs: list[Obs]) -> dict[str, set[tuple[str, str]]]:
    df = Counter(t for o in obs for t in set(o.feat["name.tokens"]))

    def rare_tokens(o):
        toks = sorted(set(o.feat["name.tokens"]), key=lambda t: (df[t], t))
        return [t for t in toks if df[t] <= 60][:2]

    def trigrams(o):
        k = o.feat["name.key"]
        return {k[i : i + 3] for i in range(len(k) - 2)}

    tri_df = Counter(g for o in obs for g in trigrams(o))
    by_key = {o.obs_id: o.feat["name.key"] for o in obs}
    acro_index = defaultdict(list)
    for o in obs:
        if o.feat["name.acronym"]:
            acro_index[o.feat["name.acronym"]].append(o.obs_id)
    acronym_pairs = set()
    for oid, key in by_key.items():
        for other in acro_index.get(key, []):
            acronym_pairs.add(tuple(sorted((oid, other))))

    # Trigram blocking: pairs sharing >= 60% of the shorter key's rarer trigrams.
    postings = defaultdict(list)
    grams = {}
    for o in obs:
        g = {x for x in trigrams(o) if tri_df[x] <= 150}
        grams[o.obs_id] = g
        for x in g:
            postings[x].append(o.obs_id)
    shared = Counter()
    for ids in postings.values():
        for a, b in combinations(sorted(ids), 2):
            shared[(a, b)] += 1
    trigram_pairs = {
        p for p, c in shared.items() if c >= 0.6 * max(1, min(len(grams[p[0]]), len(grams[p[1]])))
    }

    return {
        "identifier": block_on(obs, lambda o: [o.feat["reg_id"]] if o.feat["reg_id"] else [], 50),
        "domain": block_on(obs, lambda o: [o.feat["domain"]] if o.feat["domain"] else [], 50),
        "name_key": block_on(obs, lambda o: [o.feat["name.key"]], 100),
        "rare_token": block_on(obs, rare_tokens, 60),
        "acronym": acronym_pairs,
        "trigram": trigram_pairs,
    }


# ---------------------------------------------------------------------------
# Comparisons and levels
# ---------------------------------------------------------------------------

COMPARISONS = {
    # Token-aware: a shared brand prefix alone is its own (weak) level, never "similar".
    "name": ["exact_key", "token_typo", "token_set_0.92", "acronym", "brand_only", "else"],
    "reg_id": ["exact", "else"],
    "domain": ["exact", "else"],
    # Country, city and postal code are correlated, so they form ONE comparison.
    "location": ["postal", "city", "city_fuzzy", "country_only", "different_country"],
    "legal_form": ["same", "different"],
}
NAMES = list(COMPARISONS)
FUZZY_NAME_LEVELS = (1, 2)  # receive the common-name penalty (never a boost)

# Profile-declared bounds on learned weights (design 03 §6.5). Random-pair u never sees
# related-but-distinct entities, so levels that mostly signal relatedness look like strong
# identity evidence to EM. A shared brand is a relationship signal, not identity; a shared
# domain is supportive but must not carry a merge on its own.
WEIGHT_CAPS = {("name", "brand_only"): 0.0, ("domain", "exact"): 4.0}
RELATIONSHIP_LEVELS = {("name", "brand_only")}


def _token_typo(ta: list[str], tb: list[str]) -> bool:
    """Same token count, and every aligned token is equal or a near-typo."""
    if len(ta) != len(tb) or not ta:
        return False
    return all(x == y or JaroWinkler.similarity(x, y) >= 0.90 for x, y in zip(ta, tb))


def _brand_only(ta: list[str], tb: list[str]) -> bool:
    """Share the first (brand) token exactly but differ in at least one other token."""
    return bool(ta and tb and ta[0] == tb[0] and len(ta) > 1 and len(tb) > 1 and ta != tb)


def compare(a: Obs, b: Obs) -> tuple[int | None, ...]:
    fa, fb = a.feat, b.feat
    ka, kb = fa["name.key"], fb["name.key"]
    ta, tb = fa["name.tokens"], fb["name.tokens"]
    if ka == kb:
        name = 0
    elif _token_typo(ta, tb):
        name = 1
    elif fuzz.token_set_ratio(" ".join(ta), " ".join(tb)) >= 92:
        name = 2
    elif (fa["name.acronym"] and fa["name.acronym"] == kb) or (
        fb["name.acronym"] and fb["name.acronym"] == ka
    ):
        name = 3
    elif _brand_only(ta, tb):
        name = 4
    else:
        name = 5

    def eq(attr):
        va, vb = fa[attr], fb[attr]
        return None if va is None or vb is None else (0 if va == vb else 1)

    ca, cb = fa["country"], fb["country"]
    if ca and cb and ca != cb:
        location = 4
    elif fa["postal"] and fa["postal"] == fb["postal"]:
        location = 0
    elif fa["city"] and fb["city"] and fa["city"] == fb["city"]:
        location = 1
    elif fa["city"] and fb["city"] and JaroWinkler.similarity(fa["city"], fb["city"]) >= 0.90:
        location = 2
    elif ca and cb:
        location = 3
    else:
        location = None

    return (name, eq("reg_id"), eq("domain"), location, eq("legal_form"))


def cannot_link(a: Obs, b: Obs) -> bool:
    """Hard constraint: two registration ids in the same scheme that differ."""
    ra, rb = a.feat["reg_id"], b.feat["reg_id"]
    return bool(ra and rb and ra.split(":")[0] == rb.split(":")[0] and ra != rb)


# ---------------------------------------------------------------------------
# Fellegi-Sunter: u from random pairs, m and lambda by EM over level patterns
# ---------------------------------------------------------------------------


def level_dist(patterns: Counter, comp: int, smoothing=1.0) -> list[float]:
    L = len(COMPARISONS[NAMES[comp]])
    counts = [smoothing] * L
    for pat, n in patterns.items():
        if pat[comp] is not None:
            counts[pat[comp]] += n
    total = sum(counts)
    return [c / total for c in counts]


def estimate_u(obs: list[Obs], rng: random.Random, n_pairs=300_000):
    pats = Counter()
    for _ in range(n_pairs):
        a, b = rng.sample(obs, 2)
        pats[compare(a, b)] += 1
    return [level_dist(pats, c) for c in range(len(NAMES))]


def em(patterns: Counter, u, active: set[int], m0=None, lam=0.2, iters=500, tol=1e-9):
    m = m0 or [
        [0.9 if i == 0 else 0.1 / (len(COMPARISONS[n]) - 1) for i in range(len(COMPARISONS[n]))]
        for n in NAMES
    ]
    m = [list(row) for row in m]
    total = sum(patterns.values())
    for _ in range(iters):
        num = [[1e-6] * len(COMPARISONS[n]) for n in NAMES]
        den = [1e-6 * len(COMPARISONS[n]) for n in NAMES]
        p_sum = 0.0
        for pat, n in patterns.items():
            lm, lu = math.log(lam), math.log(1 - lam)
            for c in active:
                if pat[c] is not None:
                    lm += math.log(m[c][pat[c]])
                    lu += math.log(u[c][pat[c]])
            p = 1 / (1 + math.exp(lu - lm))
            p_sum += p * n
            for c in active:
                if pat[c] is not None:
                    num[c][pat[c]] += p * n
                    den[c] += p * n
        new_m = [
            [num[c][i] / den[c] for i in range(len(num[c]))] if c in active else m[c]
            for c in range(len(NAMES))
        ]
        new_lam = p_sum / total
        delta = abs(new_lam - lam) + max(abs(x - y) for c in active for x, y in zip(new_m[c], m[c]))
        m, lam = new_m, new_lam
        if delta < tol:
            break
    return m, lam


def train(obs_by_id, cand_by_gen, u):
    """Blocking sessions (design 03 §6.3): a comparison's m is learned only in sessions
    whose candidates were not blocked on that comparison's attribute."""

    def pats(gens):
        pairs = set().union(*(cand_by_gen[g] for g in gens))
        return Counter(compare(obs_by_id[a], obs_by_id[b]) for a, b in pairs)

    idx = {n: i for i, n in enumerate(NAMES)}
    all_c = set(range(len(NAMES)))
    m_ids, lam_ids = em(pats(["identifier", "domain"]), u, all_c - {idx["reg_id"], idx["domain"]})
    m_name, lam_name = em(
        pats(["name_key", "rare_token", "acronym", "trigram"]), u, all_c - {idx["name"]}
    )
    m = [m_ids[c] if c == idx["name"] else m_name[c] for c in range(len(NAMES))]
    return m, {"identifier/domain session": lam_ids, "name session": lam_name}


def train_naive(obs_by_id, candidates, u):
    patterns = Counter(compare(obs_by_id[a], obs_by_id[b]) for a, b in candidates)
    return em(patterns, u, set(range(len(NAMES))))


def true_m(orgs, obs):
    by_org = defaultdict(list)
    for o in obs:
        by_org[o.org_id].append(o)
    pats = Counter()
    for members in by_org.values():
        for a, b in combinations(members, 2):
            pats[compare(a, b)] += 1
    return [level_dist(pats, c, smoothing=0) for c in range(len(NAMES))]


# ---------------------------------------------------------------------------
# Scoring, term frequency, consensus
# ---------------------------------------------------------------------------


def weight(pat, m, u, caps=True) -> float:
    total = 0.0
    for c, name in enumerate(NAMES):
        if pat[c] is None:
            continue
        w = math.log2(m[c][pat[c]] / u[c][pat[c]])
        cap = WEIGHT_CAPS.get((name, COMPARISONS[name][pat[c]])) if caps else None
        total += w if cap is None else min(w, cap)
    return total


def tf_table(obs, variant, clusters=None) -> dict[str, float]:
    """Frequency of each name.key under one of four definitions."""
    if variant == "none":
        return {}
    if variant == "naive":  # per observation
        c = Counter(o.feat["name.key"] for o in obs)
    elif variant == "dedup":  # per distinct record signature
        sig = {tuple(sorted((k, str(v)) for k, v in o.feat.items())) for o in obs}
        c = Counter(dict(s)["name.key"] for s in sig)
    elif variant == "entity":  # per distinct entity (from a first pass)
        c = Counter()
        for members in clusters:
            for key in {m.feat["name.key"] for m in members}:
                c[key] += 1
    total = sum(c.values())
    return {k: v / total for k, v in c.items()}


def score_pairs(pairs, obs_by_id, m, u, tf, caps=True):
    u_exact = u[0][0]
    floor = 1 / len(obs_by_id)
    out = {}
    for a, b in pairs:
        oa, ob = obs_by_id[a], obs_by_id[b]
        pat = compare(oa, ob)
        w = weight(pat, m, u, caps)
        if tf and pat[0] == 0:
            w += math.log2(u_exact / tf.get(oa.feat["name.key"], floor))
        elif tf and pat[0] in FUZZY_NAME_LEVELS:
            # Penalize a near-match on a common name; never boost a rare one.
            f = max(tf.get(oa.feat["name.key"], floor), tf.get(ob.feat["name.key"], floor))
            w += min(0.0, math.log2(u_exact / f))
        out[(a, b)] = (w, cannot_link(oa, ob))
    return out


def holdout(comp, negative, max_share=0.10):
    """Observations to hold so the rest of a component has no internal conflict, or None.

    Greedy vertex cover of the conflict graph (highest conflict count first, ties by id).
    Held observations are merged nowhere; they go to review. The cover may be at most
    `max_share` of the component, so components under 10 observations never qualify and
    a small contradicted cluster (the Apple case) still goes wholly to review.
    """
    members = set(comp)
    conflicts = {p for p in negative if p[0] in members and p[1] in members}
    cover = []
    while conflicts:
        deg = Counter(x for p in conflicts for x in p)
        v = min(deg, key=lambda x: (-deg[x], x))
        cover.append(v)
        conflicts = {p for p in conflicts if v not in p}
    return cover if len(cover) <= max_share * len(comp) else None


def consensus(obs_ids, scored, t_high, t_low, peel=True):
    strong = {p for p, (w, cl) in scored.items() if w >= t_high and not cl}
    negative = {p for p, (w, cl) in scored.items() if w <= t_low or cl}
    accepted, review = [], []

    def validate(comp):
        members = set(comp)
        has_conflict = any(p[0] in members and p[1] in members for p in negative)
        structural = load_bearing_bridges(comp, strong) or load_bearing_articulations(comp, strong)
        return has_conflict, bool(structural)

    for comp in components(sorted(obs_ids), strong):
        if len(comp) == 1:
            accepted.append(comp)
            continue
        has_conflict, structural = validate(comp)
        if not has_conflict and not structural:
            accepted.append(comp)
            continue
        cover = holdout(comp, negative) if (peel and has_conflict and not structural) else None
        if not cover:
            review.append(comp)
            continue
        review.extend([[v] for v in cover])  # held, not merged anywhere
        core = [x for x in comp if x not in cover]
        for piece in components(core, strong):
            if len(piece) == 1 or validate(piece) == (False, False):
                accepted.append(piece)
            else:
                review.append(piece)
    return accepted, review


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def metrics(orgs, obs, accepted, review):
    truth = {o.obs_id: o.org_id for o in obs}
    kind = {o.org_id: o.kind for o in orgs}
    # Review clusters are held: each member counts as its own cluster.
    clusters = accepted + [[x] for comp in review for x in comp]
    cluster_of = {x: i for i, c in enumerate(clusters) for x in c}
    true_members = defaultdict(set)
    for x, org in truth.items():
        true_members[org].add(x)
    p_sum = r_sum = 0.0
    heavy_r, heavy_n = 0.0, 0
    for x in truth:
        pred = set(clusters[cluster_of[x]])
        true = true_members[truth[x]]
        inter = len(pred & true)
        p_sum += inter / len(pred)
        r_sum += inter / len(true)
        if kind[truth[x]] == "heavy":
            heavy_r += inter / len(true)
            heavy_n += 1
    n = len(truth)
    bad = [c for c in accepted if len({truth[x] for x in c}) > 1]
    contaminated = sum(len(c) - Counter(truth[x] for x in c).most_common(1)[0][1] for c in bad)
    premier_mixed = sum(
        1 for c in accepted if len({truth[x] for x in c if kind[truth[x]] == "common"}) > 1
    )
    return {
        "B3 P": p_sum / n,
        "B3 R": r_sum / n,
        "false-merge clusters": len(bad),
        "contaminated obs": contaminated,
        "largest bad cluster": max((len(c) for c in bad), default=0),
        "obs in review": sum(len(c) for c in review),
        "heavy B3 R": heavy_r / max(heavy_n, 1),
        "Premier clusters mixed": premier_mixed,
    }


def pair_pr(scored, truth, threshold, true_pairs_total):
    tp = fp = 0
    for (a, b), (w, cl) in scored.items():
        if w >= threshold and not cl:
            if truth[a] == truth[b]:
                tp += 1
            else:
                fp += 1
    return tp / max(tp + fp, 1), tp / true_pairs_total


# ---------------------------------------------------------------------------


def main():
    orgs, obs = make_dataset()
    for o in obs:
        normalize(o)
    obs_by_id = {o.obs_id: o for o in obs}
    truth = {o.obs_id: o.org_id for o in obs}
    kinds = Counter(o.kind for o in orgs)
    print("=== Dataset")
    print(f"  {len(orgs)} organizations ({dict(kinds)}), {len(obs)} observations")

    # Blocking
    cand_by_gen = generate(obs)
    candidates = set().union(*cand_by_gen.values())
    by_org = defaultdict(list)
    for o in obs:
        by_org[o.org_id].append(o.obs_id)
    true_pairs = {tuple(sorted(p)) for ids in by_org.values() for p in combinations(ids, 2)}
    found = true_pairs & candidates
    connected = sum(
        1
        for ids in by_org.values()
        if len(components(sorted(ids), {p for p in candidates if p[0] in ids and p[1] in ids})) == 1
    )
    print("\n=== Blocking")
    print(f"  candidate pairs: {len(candidates):,} (all pairs: {len(obs) * (len(obs) - 1) // 2:,})")
    print(
        f"  true pairs covered: {len(found)}/{len(true_pairs)} ({len(found) / len(true_pairs):.1%})"
    )
    print(
        f"  organizations whose observations are connected by candidates: "
        f"{connected}/{len(by_org)} ({connected / len(by_org):.1%})"
    )
    for g, pairs in cand_by_gen.items():
        others = set().union(*(p for h, p in cand_by_gen.items() if h != g))
        only = len((pairs - others) & true_pairs)
        print(
            f"    {g:<11} {len(pairs):>7,} pairs, {len(pairs & true_pairs):>6,} true, "
            f"{only:>4} true pairs found by no other generator"
        )

    # Training
    rng = random.Random(SEED + 1)
    u = estimate_u(obs, rng)
    m, lams = train(obs_by_id, cand_by_gen, u)
    m_naive, _ = train_naive(obs_by_id, candidates, u)
    m_true = true_m(orgs, obs)
    print("\n=== m estimates vs truth (P(level | match))")
    for c, name in enumerate(NAMES):
        for lvl, label in enumerate(COMPARISONS[name]):
            if label in ("else", "different") and name != "reg_id":
                continue
            print(
                f"  {name:<10} {label:<15} true {m_true[c][lvl]:.3f}   "
                f"sessions {m[c][lvl]:.3f}   single EM {m_naive[c][lvl]:.3f}   "
                f"u {u[c][lvl]:.5f}"
            )
    print("  lambda: " + ", ".join(f"{k} {v:.3f}" for k, v in lams.items()))

    # Thresholds: label-tuned (needs ground truth, as a benchmark would provide)
    # versus label-free (posterior >= 0.999 under the name-session lambda).
    lam = lams["name session"]
    t_free = math.log2(0.999 / 0.001) - math.log2(lam / (1 - lam))
    t_low = 0.0

    results = {}
    print("\n=== Variants (accepted clusters; review clusters held as singletons)")
    header = (
        "variant",
        "threshold",
        "T_high",
        "pair P",
        "pair R",
        "B3 P",
        "B3 R",
        "false-merge clusters",
        "contaminated obs",
        "largest bad cluster",
        "obs in review",
        "heavy B3 R",
        "Premier clusters mixed",
    )
    rows = []
    first_pass = None
    plan = (
        [("none", False, True)]
        + [(v, True, True) for v in ("none", "naive", "dedup", "entity")]
        + [("entity", True, False)]
    )
    for variant, peel, caps in plan:
        if variant == "entity":
            tf = tf_table(obs, "entity", [[obs_by_id[x] for x in c] for c in first_pass])
        else:
            tf = tf_table(obs, variant)
        scored = score_pairs(candidates, obs_by_id, m, u, tf, caps=caps)
        ws = sorted({w for w, _ in scored.values()})
        t_tuned = next(t for t in ws if pair_pr(scored, truth, t, len(true_pairs))[0] >= 0.995)
        for label, t_high in (("tuned", t_tuned), ("label-free", t_free)):
            accepted, review = consensus(list(obs_by_id), scored, t_high, t_low, peel=peel)
            if variant == "none" and peel and caps and label == "tuned":
                first_pass = accepted + [[x] for c in review for x in c]
            p, r = pair_pr(scored, truth, t_high, len(true_pairs))
            mt = metrics(orgs, obs, accepted, review)
            results[(variant, label, peel)] = mt
            tag = "" if peel and caps else " (no peel)" if not peel else " (no caps)"
            rows.append(
                (
                    variant + tag,
                    label,
                    f"{t_high:.1f}",
                    f"{p:.3f}",
                    f"{r:.3f}",
                    f"{mt['B3 P']:.4f}",
                    f"{mt['B3 R']:.4f}",
                    str(mt["false-merge clusters"]),
                    str(mt["contaminated obs"]),
                    str(mt["largest bad cluster"]),
                    str(mt["obs in review"]),
                    f"{mt['heavy B3 R']:.3f}",
                    str(mt["Premier clusters mixed"]),
                )
            )
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(header)]
    short = [
        "variant",
        "thresh",
        "T_high",
        "pairP",
        "pairR",
        "B3 P",
        "B3 R",
        "FMclus",
        "contam",
        "maxbad",
        "review",
        "heavyR",
        "Premier",
    ]
    widths = [max(len(s), *(len(r[i]) for r in rows)) for i, s in enumerate(short)]
    print("  " + "  ".join(s.rjust(w) for s, w in zip(short, widths)))
    for r in rows:
        print("  " + "  ".join(v.rjust(w) for v, w in zip(r, widths)))
    print("  FMclus = accepted clusters mixing >1 true organization; contam = observations in")
    print("  them not of the majority organization; heavyR = B-cubed recall on the 5 heavy")
    print("  suppliers; Premier = accepted clusters mixing >1 of the 25 'Premier Services'.")

    # Group companies: merged across the family, and caught as relationship candidates?
    kind = {o.org_id: o.kind for o in orgs}
    tf = tf_table(obs, "entity", [[obs_by_id[x] for x in c] for c in first_pass])
    print("\n=== Group companies (entity TF, label-free threshold)")
    for caps in (False, True):
        scored = score_pairs(candidates, obs_by_id, m, u, tf, caps=caps)
        accepted, _ = consensus(list(obs_by_id), scored, t_free, t_low)
        fam_mixed = sum(
            1 for c in accepted if len({truth[x] for x in c if kind[truth[x]] == "family"}) > 1
        )
        print(
            f"  caps {'on ' if caps else 'off'}: accepted clusters mixing two group companies: "
            f"{fam_mixed}"
        )
    org_of = {o.org_id: o for o in orgs}
    rel = [
        (a, b)
        for a, b in candidates
        if compare(obs_by_id[a], obs_by_id[b])[0] == COMPARISONS["name"].index("brand_only")
    ]
    sib = [
        (a, b)
        for a, b in rel
        if truth[a] != truth[b]
        and org_of[truth[a]].tokens[0] == org_of[truth[b]].tokens[0]
        and kind[truth[a]] == kind[truth[b]] == "family"
    ]
    same = [(a, b) for a, b in rel if truth[a] == truth[b]]
    print(
        f"  brand_only candidate pairs: {len(rel)}; {len(sib)} are sibling companies "
        f"(relationship candidates), {len(same)} are the same company, "
        f"{len(rel) - len(sib) - len(same)} other"
    )


if __name__ == "__main__":
    main()
