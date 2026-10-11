"""Botanical names (design 06 §4.2).

A parser for scientific names of plants under the International Code of
Nomenclature for algae, fungi, and plants, and the Taxon profile's normalizers. It
handles what records actually contain: authorship with or without a basionym author
in parentheses, "ex" and "in", rank markers written several ways, hybrids, an
abbreviated genus ("C. pulcherrima"), annotations such as "auct. non L." or "s.l.",
indeterminate names ("Quercus sp."), and names in capitals. It is deliberately small;
gnparser (Global Names Architecture, MIT) is the reference it is measured against.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

from thisness.core.components import ComponentRef
from thisness.core.observation import Observation
from thisness.normalize.organization import _features, _fold, _values
from thisness.profiles.base import Feature

HYBRID_SIGN = "\u00d7"
QUOTES = "'\"\u2018\u2019\u201c\u201d"
CULTIVAR = re.compile(f"[{QUOTES}][^{QUOTES}]*[{QUOTES}]")  # 'Crimson King': not a taxon
RANKS = {"subsp": "subsp.", "ssp": "subsp.", "subspecies": "subsp.", "var": "var.",
         "variety": "var.", "v": "var.", "subvar": "subvar.", "f": "f.", "fo": "f.",
         "forma": "f.", "form": "f.", "subf": "subf."}  # fmt: skip
INDETERMINATE = {"sp", "spp", "sp.", "spp.", "species", "indet", "cf", "aff"}
# Annotations that qualify how a name is used rather than which name it is.
ANNOTATIONS = (
    (re.compile(r"\bauct\.?(\s+non\s+[^,;]*)?", re.I), "misapplied"),
    (re.compile(r"\bsensu\s+(lato|stricto|[A-Z][^,;]*)", re.I), "sensu"),
    (re.compile(r"\bs\.\s?(l|str|lat)\.", re.I), "sensu"),
    (re.compile(r"\b(p\.\s?p\.|pro\s+parte)", re.I), "pro_parte"),
    (re.compile(r"\bnom\.\s*(illeg|inval|nud|cons|rej|superfl)\.?", re.I), "nom_status"),
    (re.compile(r"\b(cf|aff)\.", re.I), "uncertain"),
)
# Common spellings of a few prolific authors, mapped to the standard abbreviation.
AUTHOR_ALIASES = {"linn": "l", "linnaeus": "l", "linne": "l", "lin": "l", "dc": "dc",
                  "decandolle": "dc", "swartz": "sw", "miller": "mill", "willdenow": "willd",
                  "lamarck": "lam", "bentham": "benth", "hooker": "hook"}  # fmt: skip
# Latin adjective endings, longest first: an epithet agrees in gender with its genus, so
# "pulcherrima", "pulcherrimus" and "pulcherrimum" are one epithet.
ENDINGS = ("rum", "ra", "er", "um", "us", "is", "a", "e")


@dataclass(frozen=True)
class ParsedName:
    genus: str
    epithet: str | None = None
    rank: str | None = None
    infra: str | None = None
    hybrid: bool = False
    abbreviated_genus: bool = False
    authors: str | None = None  # the combining (or only) author, normalized
    basionym_authors: str | None = None  # the parenthetical author, normalized
    flags: frozenset[str] = field(default_factory=frozenset)

    @property
    def canonical(self) -> str:
        parts = [self.genus, self.epithet, self.infra]
        return " ".join(p for p in parts if p)

    @property
    def stem(self) -> str:
        return " ".join(epithet_stem(p) for p in (self.epithet, self.infra) if p)


def epithet_stem(epithet: str) -> str:
    for ending in ENDINGS:
        if epithet.endswith(ending) and len(epithet) - len(ending) >= 3:
            return epithet[: -len(ending)]
    return epithet


def author_key(text: str) -> str | None:
    """'H. Karst.' -> 'hkarst'; 'Linn.' -> 'l'; '&'/'et' joins kept as '&'. 'fil.' and
    'filius' (the son) are written 'f.': 'Hook.fil.' is 'Hook.f.'."""
    text = _fold(text).replace(" et ", " & ")
    text = re.sub(r"\bfil(?:ius)?\b\.?", "f.", text)
    parts = []
    for part in re.split(r"\s*&\s*|\s*,\s*", text):
        key = re.sub(r"[^a-z]", "", part)
        if key:
            parts.append(AUTHOR_ALIASES.get(key, key))
    return "&".join(parts) or None


def collapse_key(view: Mapping[str, list[str]]) -> str | None:
    """The name-only plan's collapse key for taxa (design 06 §10.5): the canonical name
    and the combining author. "Quercus ilex L." and "QUERCUS ILEX L." are one node;
    "Quercus ilex", "Quercus ilex L." and "Quercus ilex Lour." stay three, since
    collapsing cannot be undone within a run and an authorless name may be either."""
    canonical = view.get("taxon.canonical")
    if not canonical:
        return None
    authors = view.get("taxon.authors")
    return f"{canonical[0]}|{authors[0]}" if authors else canonical[0]


def _authorship(text: str) -> tuple[str | None, str | None]:
    """'(L.) Sw.' -> ('sw', 'l'). With 'ex', the author after it is the one the Code
    counts; with 'in', the one before it."""
    text = re.sub(r"\b1[5-9]\d\d\b|\b20\d\d\b", "", text)  # years, as zoologists write them
    basionym = None
    if m := re.match(r"\s*\(([^)]*)\)\s*(.*)$", text):
        basionym, text = m.group(1), m.group(2)

    def counted(part: str | None) -> str | None:
        if not part:
            return None
        part = re.split(r"\s+in\s+", part, maxsplit=1)[0]
        part = re.split(r"\s+ex\.?\s+", part)[-1]
        return author_key(part)

    return counted(text.strip()), counted(basionym)


def parse(raw: str) -> ParsedName | None:
    """A name string as a ParsedName, or None when it is not a scientific name."""
    text = unicodedata.normalize("NFKC", raw).replace(HYBRID_SIGN, " x ").strip()
    text = CULTIVAR.sub("", text)
    flags = set()
    for pattern, flag in ANNOTATIONS:
        if pattern.search(text):
            flags.add(flag)
            text = pattern.sub(" ", text)
    tokens = text.replace(",", " ").split()
    hybrid = False
    if tokens and tokens[0].lower() == "x":
        hybrid, tokens = True, tokens[1:]
    if not tokens:
        return None
    genus_token = tokens[0]
    if not re.fullmatch(r"[A-Za-z][a-zA-Z-]*\.?", genus_token):
        return None
    abbreviated = bool(re.fullmatch(r"[A-Za-z]\.", genus_token))
    genus = _fold(genus_token.rstrip("."))
    # Case tells an epithet from an author only when the genus is written normally
    # ("Caesalpinia"); in capitals or in lowercase, position decides.
    case_reliable = genus_token[0].isupper() and not genus_token.rstrip(".").isupper()
    rest = tokens[1:]

    def is_epithet(token: str) -> bool:
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z-]*", token) or len(token) < 2:
            return False
        if token.lower().rstrip(".") in RANKS or token.lower() in INDETERMINATE:
            return False
        return token[0].islower() if case_reliable else len(token) > 2

    epithet = rank = infra = None
    if rest and rest[0].lower() == "x":
        hybrid, rest = True, rest[1:]
    if rest and rest[0].lower().rstrip(".") in INDETERMINATE:
        rest = rest[1:]
    elif rest and is_epithet(rest[0]):
        epithet, rest = _fold(rest[0]), rest[1:]
    # Skip the species' own authorship before an infraspecific rank marker.
    for i, token in enumerate(rest):
        marker = token.lower().rstrip(".")
        if marker in RANKS and i + 1 < len(rest) and is_epithet(rest[i + 1]) and epithet:
            rank, infra = RANKS[marker], _fold(rest[i + 1])
            rest = rest[i + 2 :]
            break
    if epithet is None and not abbreviated and len(genus) < 2:
        return None
    authors, basionym = _authorship(" ".join(rest)) if rest else (None, None)
    return ParsedName(genus=genus, epithet=epithet, rank=rank, infra=infra, hybrid=hybrid,
                      abbreviated_genus=abbreviated, authors=authors,
                      basionym_authors=basionym, flags=frozenset(flags))  # fmt: skip


def trigrams(text: str) -> list[str]:
    compact = text.replace(" ", "")
    return sorted({compact[i : i + 3] for i in range(len(compact) - 2)}) if len(compact) > 2 \
        else [compact]  # fmt: skip


class TaxonName:
    """Features of a record's scientific name and of the accepted name a source asserts
    for it. ``taxon.initial_key`` (genus initial and epithet stems) blocks an
    abbreviated genus with its full form; ``taxon.epithet_key`` blocks recombinations."""

    features = frozenset({
        "taxon.canonical", "taxon.genus", "taxon.genus_initial", "taxon.epithet",
        "taxon.stem", "taxon.stem_key", "taxon.initial_key", "taxon.epithet_key",
        "taxon.rank", "taxon.hybrid", "taxon.authors", "taxon.basionym_authors",
        "taxon.flags", "taxon.trigram", "taxon.accepted", "taxon.reference_accepted",
        "taxon.status",
    })  # fmt: skip

    def __init__(self, authority: str = "wfo") -> None:
        self.authority = authority
        self.ref = ComponentRef.of(
            "normalizer", "taxon_name", "2",
            {"ranks": RANKS, "endings": list(ENDINGS), "author_aliases": AUTHOR_ALIASES,
             "annotations": [p.pattern for p, _ in ANNOTATIONS], "authority": authority},
        )  # fmt: skip

    def normalize(self, observation: Observation) -> list[Feature]:
        attrs = observation.attributes or {}
        raw = attrs.get("name")
        parsed = parse(raw) if isinstance(raw, str) and raw.strip() else None
        out: list[Feature] = []
        if parsed is not None:
            authors, basionym = parsed.authors, parsed.basionym_authors
            if isinstance(attrs.get("authorship"), str) and authors is None and basionym is None:
                authors, basionym = _authorship(attrs["authorship"])
            out.append(Feature("taxon.genus_initial", 0, parsed.genus[0]))
            if not parsed.abbreviated_genus:
                out.append(Feature("taxon.genus", 0, parsed.genus))
            if parsed.epithet:
                stem = parsed.stem
                out += [Feature("taxon.epithet", 0, parsed.epithet),
                        Feature("taxon.stem", 0, stem),
                        Feature("taxon.initial_key", 0, f"{parsed.genus[0]} {stem}"),
                        Feature("taxon.epithet_key", 0, stem)]  # fmt: skip
                if not parsed.abbreviated_genus:
                    out += [Feature("taxon.canonical", 0, parsed.canonical),
                            Feature("taxon.stem_key", 0, f"{parsed.genus} {stem}")]  # fmt: skip
                    out += _features("taxon.trigram", trigrams(parsed.canonical))
            rank = parsed.rank or _rank(attrs.get("rank"))
            if rank:
                out.append(Feature("taxon.rank", 0, rank))
            if parsed.hybrid:
                out.append(Feature("taxon.hybrid", 0, "x"))
            if authors:
                out.append(Feature("taxon.authors", 0, authors))
            if basionym:
                out.append(Feature("taxon.basionym_authors", 0, basionym))
            out += _features("taxon.flags", sorted(parsed.flags))
        # What the source asserts is accepted: the name it names, or the record's own name
        # when the source lists it as accepted. The reference authority's assertions are
        # kept apart, since they define the entity (design 06, decision 2).
        status = str(attrs.get("taxonomic_status") or "").strip().casefold()
        accepted = attrs.get("accepted_name")
        asserted = None
        if isinstance(accepted, str) and (a := parse(accepted)) is not None and a.epithet:
            asserted = a.canonical
        elif status == "accepted" and parsed is not None and parsed.epithet \
                and not parsed.abbreviated_genus:  # fmt: skip
            asserted = parsed.canonical
        if asserted:
            out.append(Feature("taxon.accepted", 0, asserted))
            source = getattr(getattr(observation, "source", None), "type", None)
            if source == self.authority:
                out.append(Feature("taxon.reference_accepted", 0, asserted))
        if status:
            out.append(Feature("taxon.status", 0, status))
        return out


def _rank(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    key = raw.strip().casefold().rstrip(".")
    return RANKS.get(key) or ("species" if key == "species" else None)


class Classification:
    """Family and kingdom, when a source gives them."""

    features = frozenset({"taxon.family", "taxon.kingdom"})
    ref = ComponentRef.of("normalizer", "taxon_classification", "1", {})

    def normalize(self, observation: Observation) -> list[Feature]:
        attrs = observation.attributes or {}
        out = []
        for attribute, feature in (("family", "taxon.family"), ("kingdom", "taxon.kingdom")):
            value = attrs.get(attribute)
            if isinstance(value, str) and value.strip():
                out.append(Feature(feature, 0, _fold(value).strip()))
        return out


class VernacularNames:
    """Each vernacular or community name as 'language:name', folded, from every
    attribute named. A value may carry its language as 'en:Barbados pride' or as a
    mapping {"lang": "en", "name": ...}. Matching happens locally; what a model may see
    is still each attribute's own policy (design 06 §7)."""

    features = frozenset({"taxon.vernacular"})

    def __init__(self, attributes: tuple[str, ...] = ("vernacular_names",)) -> None:
        self.attributes = attributes
        self.ref = ComponentRef.of("normalizer", "vernacular_names", "1",
                                   {"attributes": list(attributes)})  # fmt: skip

    def normalize(self, observation: Observation) -> list[Feature]:
        out = []
        for attribute in self.attributes:
            raw = (observation.attributes or {}).get(attribute)
            values = raw if isinstance(raw, list) else [raw] if raw else []
            for value in values:
                if isinstance(value, dict):
                    lang, name = value.get("lang") or "und", value.get("name")
                elif isinstance(value, str) and re.match(r"^[a-z]{2,3}:", value):
                    lang, name = value.split(":", 1)
                else:
                    lang, name = "und", value
                if isinstance(name, str) and (folded := " ".join(_fold(name).split())):
                    out.append(f"{lang.casefold()}:{folded}")
        return _features("taxon.vernacular", out)


__all__ = [
    "Classification",
    "ParsedName",
    "TaxonName",
    "VernacularNames",
    "_values",
    "author_key",
    "epithet_stem",
    "parse",
]  # fmt: skip
