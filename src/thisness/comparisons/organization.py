"""Organization comparisons, the registration constraint and the identifier rule
(design 03 §6.1, §7)."""

from __future__ import annotations

import re
from collections.abc import Mapping

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from thisness.comparisons.base import FeatureView, Polarity, single, values
from thisness.core.components import ComponentRef


def _ref(name: str, version: str, config: dict) -> ComponentRef:
    return ComponentRef.of("comparison", name, version, config)


_ROMAN = re.compile(r"(?=[ivxl]+$)l?x{0,3}(ix|iv|v?i{0,3})")


def _abbreviates(short: str, long: str) -> bool:
    """ "intl" for "international", "hldgs" for "holdings": the first letter shared and
    every letter of the short form in order in the long one."""
    if not short or not long or short[0] != long[0] or len(short) >= len(long):
        return False
    rest = iter(long)
    return all(ch in rest for ch in short)


def _numbers(tokens: list[str]) -> list[str]:
    """The numbering in a name: digits, and Roman numerals up to fifty."""
    return [t for t in tokens if t.isdigit() or (_ROMAN.fullmatch(t) and t)]


class NameComparison:
    """Token-aware: a shared brand with a different descriptor is its own level,
    never mistaken for a typo (design 03 §6.2 rule 3). Two names numbered differently
    are different organizations: a fund's second and third vehicles, "Holdings II" and
    "Holdings III", sit at one address with one name otherwise (design 07 §13.2). One
    name being the other with words added is its own level, capped by the profile: on
    real UK data it is mostly a parent and its subsidiary."""

    name = "name"
    levels = ("exact_key", "number_differs", "token_typo", "token_subset", "qualifier_differs",
              "token_set_0.92", "acronym", "brand_only", "else")  # fmt: skip
    features = ("name.key", "name.tokens", "name.acronym")
    attributes = frozenset({"name"})
    polarity: Mapping[str, Polarity] = {
        "exact_key": "supports", "number_differs": "contradicts", "token_typo": "supports",
        "token_subset": "neutral", "qualifier_differs": "contradicts",
        "token_set_0.92": "supports", "acronym": "supports",
        "brand_only": "neutral", "else": "contradicts",
    }  # fmt: skip
    token_jw = 0.90
    token_set = 92
    ref = _ref("name", "4", {"levels": levels, "token_jw": token_jw, "token_set": token_set})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        ka, kb = single(a, "name.key"), single(b, "name.key")
        if ka is None or kb is None:
            return None
        ta, tb = list(a.get("name.tokens", ())), list(b.get("name.tokens", ()))
        if ka == kb:
            return 0
        na, nb = _numbers(ta), _numbers(tb)
        if na and nb and na != nb:
            return 1
        if (
            len(ta) == len(tb)
            and ta
            and all(
                x == y or JaroWinkler.similarity(x, y) >= self.token_jw
                for x, y in zip(ta, tb, strict=True)
            )
        ):
            return 2
        # One name is the other with words added: how a group's companies are told apart
        # ("Castle Hearing", "Castle Hearing Group", "Castle Hearing (Dumfries)").
        sa, sb = set(ta), set(tb)
        if sa and sb and sa != sb and (sa < sb or sb < sa):
            return 3
        # Each name has a word the other lacks, and neither is a typo or an abbreviation
        # of the other: a group's siblings ("Bridgepoint Advisers UK", "... Europe";
        # "(GBP)", "(EURO)"; "South West", "South East").
        ua, ub = sa - sb, sb - sa
        if (
            sa & sb
            and ua
            and ub
            and not any(
                JaroWinkler.similarity(x, y) >= self.token_jw
                or _abbreviates(x, y)
                or _abbreviates(y, x)
                for x in ua
                for y in ub
            )
        ):
            return 4
        if fuzz.token_set_ratio(" ".join(ta), " ".join(tb)) >= self.token_set:
            return 5
        if single(a, "name.acronym") == kb or single(b, "name.acronym") == ka:
            return 6
        if ta and tb and ta[0] == tb[0] and len(ta) > 1 and len(tb) > 1:
            return 7
        return 8


class _SetComparison:
    """``exact`` when the two sides share any value, ``else`` when both have values
    and share none, ``None`` when either side has none."""

    feature: str
    levels = ("exact", "else")
    polarity: Mapping[str, Polarity] = {"exact": "supports", "else": "contradicts"}

    def _scoped(self, view: FeatureView) -> set[str]:
        return values(view, self.feature)

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        va, vb = self._scoped(a), self._scoped(b)
        if not va or not vb:
            return None
        return 0 if va & vb else 1


class RegistrationComparison(_SetComparison):
    """Compared only within a scheme: an EIN says nothing about a CRN."""

    name = "reg_id"
    feature = "reg_id"
    features = ("reg_id",)
    attributes = frozenset({"registration_ids"})
    ref = _ref("reg_id", "1", {"same_scheme_only": True})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        va, vb = values(a, "reg_id"), values(b, "reg_id")
        schemes = {v.split(":", 1)[0] for v in va} & {v.split(":", 1)[0] for v in vb}
        if not schemes:
            return None
        return 0 if va & vb else 1


class DomainComparison(_SetComparison):
    name = "domain"
    feature = "domain"
    features = ("domain",)
    attributes = frozenset({"domain", "website", "email"})
    ref = _ref("domain", "1", {})


class LocationComparison:
    """Country, city and postal code are correlated, so they are one comparison
    (design 03 §6.2 rule 2). Same city with two different postal codes is its own
    level: one company is usually at one site, so the disagreement is evidence."""

    name = "location"
    levels = ("postal", "city", "city_other_postal", "city_fuzzy", "country_only",
              "different_country")  # fmt: skip
    features = ("country", "city", "postal")
    attributes = frozenset({"country", "city", "postal_code"})
    polarity: Mapping[str, Polarity] = {
        "postal": "supports", "city": "supports", "city_other_postal": "neutral",
        "city_fuzzy": "supports", "country_only": "neutral", "different_country": "contradicts",
    }  # fmt: skip
    city_jw = 0.90
    ref = _ref("location", "2", {"levels": levels, "city_jw": city_jw})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        ca, cb = single(a, "country"), single(b, "country")
        if ca and cb and ca != cb:
            return 5
        pa, pb = single(a, "postal"), single(b, "postal")
        if pa and pa == pb:
            return 0
        xa, xb = single(a, "city"), single(b, "city")
        if xa and xb and xa == xb:
            return 2 if pa and pb else 1
        if xa and xb and JaroWinkler.similarity(xa, xb) >= self.city_jw:
            return 3
        if ca and cb:
            return 4
        return None


class LegalFormComparison:
    name = "legal_form"
    levels = ("same", "different")
    features = ("legal_form",)
    attributes = frozenset({"name"})
    polarity: Mapping[str, Polarity] = {"same": "supports", "different": "contradicts"}
    ref = _ref("legal_form", "1", {})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        la, lb = single(a, "legal_form"), single(b, "legal_form")
        if la is None or lb is None:
            return None
        return 0 if la == lb else 1


class RegistrationConflict:
    """Two identifiers in the same scheme, none shared: these are different legal
    entities. A hard cannot-link (design 03 §7)."""

    name = "registration_conflict"
    ref = ComponentRef.of("constraint", "registration_conflict", "1", {})

    def cannot_link(self, a: FeatureView, b: FeatureView) -> str | None:
        va, vb = values(a, "reg_id"), values(b, "reg_id")
        if va & vb:
            return None
        by_scheme_a = {v.split(":", 1)[0] for v in va}
        clash = sorted(by_scheme_a & {v.split(":", 1)[0] for v in vb})
        return f"different {clash[0]} identifiers" if clash else None


class SameRegistration:
    """Same identifier in the same scheme, and no country conflict: MATCH."""

    name = "same_registration"
    ref = ComponentRef.of("resolver", "same_registration", "1", {})

    def apply(self, levels: Mapping[str, str | None]) -> str | None:
        if levels.get("reg_id") == "exact" and levels.get("location") != "different_country":
            return "MATCH"
        return None
