"""Person comparisons, constraints and the identifier rule (design 04 §6.3, §6.4)."""

from __future__ import annotations

from collections.abc import Mapping

from rapidfuzz.distance import JaroWinkler

from thisness.comparisons.base import FeatureView, Polarity, single, values
from thisness.comparisons.organization import _SetComparison
from thisness.core.components import ComponentRef
from thisness.normalize.person import name_groups, soundex


def _ref(name: str, version: str, config: dict) -> ComponentRef:
    return ComponentRef.of("comparison", name, version, config)


def _similar(x: str, y: str, threshold: float) -> bool:
    return x == y or soundex(x) == soundex(y) or JaroWinkler.similarity(x, y) >= threshold


class PersonNameComparison:
    """Most specific level first. The middle initial and generational suffix separate
    'John A. Smith' from 'John B. Smith' and a father from a son; a family name that
    differs with the given name agreeing is its own level, since people change family
    names on marriage. A family-first name's other reading (``pname.alt``, a compound
    family name) that agrees with the other side counts as ``fuzzy``, like a given and
    family name swapped (design 09 §3)."""

    name = "name"
    levels = ("exact", "given_family", "alias", "initial", "fuzzy", "middle_conflict",
              "given_only", "family_only", "else")  # fmt: skip
    features = ("pname.given", "pname.family", "pname.groups", "pname.middle", "pname.suffix",
                "pname.alt")  # fmt: skip
    attributes = frozenset({"name", "given_name", "family_name", "name_order"})
    polarity: Mapping[str, Polarity] = {
        "exact": "supports", "given_family": "supports", "alias": "supports",
        "initial": "supports", "fuzzy": "supports", "middle_conflict": "contradicts",
        "given_only": "neutral", "family_only": "contradicts", "else": "contradicts",
    }  # fmt: skip
    jw = 0.88
    ref = _ref("person_name", "2", {"levels": levels, "jw": jw, "alt_level": "fuzzy"})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        level = self._primary(a, b)
        if level is not None and level >= 6 and (_alt_agrees(a, b) or _alt_agrees(b, a)):
            return 4
        return level

    def _primary(self, a: FeatureView, b: FeatureView) -> int | None:
        ga, gb = single(a, "pname.given"), single(b, "pname.given")
        fa, fb = single(a, "pname.family"), single(b, "pname.family")
        if not (ga and gb and fa and fb):
            return None
        ma, mb = single(a, "pname.middle") or "", single(b, "pname.middle") or ""
        sa, sb = single(a, "pname.suffix") or "", single(b, "pname.suffix") or ""
        middle_ok = not ma or not mb or ma[0] == mb[0]
        suffix_ok = not sa or not sb or sa == sb
        alias = bool(values(a, "pname.groups") & values(b, "pname.groups"))
        initial = (len(ga) == 1 or len(gb) == 1) and ga[0] == gb[0]
        if fa == fb:
            if ga == gb:
                if not (middle_ok and suffix_ok):
                    return 5
                return 0 if ma == mb and sa == sb else 1
            if (alias or initial) and not (middle_ok and suffix_ok):
                return 5
            if alias:
                return 2
            if initial:
                return 3
            if _similar(ga, gb, self.jw):
                return 4
            return 7
        if ga == fb and fa == gb:
            return 4  # given and family swapped
        given_match = ga == gb or alias
        if _similar(fa, fb, self.jw) and (given_match or _similar(ga, gb, self.jw)):
            return 4
        if given_match:
            return 6
        return 8


def _alt_agrees(a: FeatureView, b: FeatureView) -> bool:
    """``a``'s other reading has ``b``'s family name and the same given name, an alias of
    it, or its initial."""
    alt = single(a, "pname.alt")
    gb, fb = single(b, "pname.given"), single(b, "pname.family")
    if not (alt and gb and fb):
        return False
    given, family = alt.split(" ", 1)
    if family != fb:
        return False
    if given == gb or (len(gb) == 1 and given[0] == gb):
        return True
    return bool(set(name_groups(given)) & values(b, "pname.groups"))


class EmailComparison(_SetComparison):
    """People keep several addresses, so a different email is weak evidence."""

    name = "email"
    feature = "email"
    features = ("email",)
    attributes = frozenset({"email"})
    polarity: Mapping[str, Polarity] = {"exact": "supports", "else": "neutral"}
    ref = _ref("person_email", "1", {})


class PhoneComparison(_SetComparison):
    """Households share landlines and carriers recycle numbers: capped."""

    name = "phone"
    feature = "phone"
    features = ("phone",)
    attributes = frozenset({"phone"})
    polarity: Mapping[str, Polarity] = {"exact": "supports", "else": "neutral"}
    ref = _ref("person_phone", "1", {})


class DateOfBirthComparison:
    """Full dates first; 'transposed' is a day and month swapped, the most common
    error in dates entered by hand across countries."""

    name = "date_of_birth"
    levels = ("exact", "transposed", "year_month", "year", "different")
    features = ("dob", "dob.ym", "dob.y")
    attributes = frozenset({"date_of_birth"})
    polarity: Mapping[str, Polarity] = {
        "exact": "supports", "transposed": "neutral", "year_month": "supports",
        "year": "neutral", "different": "contradicts",
    }  # fmt: skip
    ref = _ref("date_of_birth", "1", {"levels": levels})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        da, db = single(a, "dob"), single(b, "dob")
        if da and db:
            if da == db:
                return 0
            return 1 if _transposed(da, db) else 4
        ya, yb = single(a, "dob.y"), single(b, "dob.y")
        if not ya or not yb:
            return None
        if ya != yb:
            return 4
        ma, mb = single(a, "dob.ym"), single(b, "dob.ym")
        if ma and mb:
            return 2 if ma == mb else 4
        return 3


def _transposed(x: str, y: str) -> bool:
    return x[:4] == y[:4] and x[5:7] == y[8:10] and x[8:10] == y[5:7]


class EmployerComparison(_SetComparison):
    """Colleagues share an employer: capped."""

    name = "employer"
    feature = "employer"
    features = ("employer",)
    attributes = frozenset({"employer"})
    polarity: Mapping[str, Polarity] = {"exact": "supports", "else": "neutral"}
    ref = _ref("employer", "1", {})


class AddressComparison(_SetComparison):
    """Households share an address: capped."""

    name = "address"
    feature = "address"
    features = ("address",)
    attributes = frozenset({"address_line", "postal_code"})
    polarity: Mapping[str, Polarity] = {"exact": "supports", "else": "neutral"}
    ref = _ref("address", "1", {})


class IdentifierComparison:
    """Compared within a scheme only, like registration identifiers."""

    name = "ident"
    levels = ("exact", "else")
    features = ("ident",)
    attributes = frozenset({"identifiers"})
    polarity: Mapping[str, Polarity] = {"exact": "supports", "else": "contradicts"}
    ref = _ref("person_ident", "1", {"same_scheme_only": True})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        va, vb = values(a, "ident"), values(b, "ident")
        schemes = {v.split(":", 1)[0] for v in va} & {v.split(":", 1)[0] for v in vb}
        if not schemes:
            return None
        return 0 if va & vb else 1


class DateOfBirthConflict:
    """Two full dates of birth that differ (and are not a day-month swap): different
    people. A hard cannot-link (design 04 §6.4)."""

    name = "date_of_birth_conflict"
    ref = ComponentRef.of("constraint", "date_of_birth_conflict", "1", {"transposed_ok": True})

    def cannot_link(self, a: FeatureView, b: FeatureView) -> str | None:
        da, db = single(a, "dob"), single(b, "dob")
        if da and db and da != db and not _transposed(da, db):
            return "different dates of birth"
        return None


class IdentifierConflict:
    name = "identifier_conflict"
    ref = ComponentRef.of("constraint", "identifier_conflict", "1", {})

    def cannot_link(self, a: FeatureView, b: FeatureView) -> str | None:
        va, vb = values(a, "ident"), values(b, "ident")
        if va & vb:
            return None
        clash = sorted({v.split(":", 1)[0] for v in va} & {v.split(":", 1)[0] for v in vb})
        return f"different {clash[0]} identifiers" if clash else None


class SameIdentifier:
    """Same identifier in the same scheme, and no date-of-birth conflict: MATCH."""

    name = "same_identifier"
    ref = ComponentRef.of("resolver", "same_identifier", "1", {})

    def apply(self, levels: Mapping[str, str | None]) -> str | None:
        if levels.get("ident") == "exact" and levels.get("date_of_birth") != "different":
            return "MATCH"
        return None


class SameBirthDateDifferentGiven:
    """Same family name and full date of birth, given names that cannot be the same
    person's: twins (design 04 §6.4). A given name that is the other side's middle
    initial is compatible ('James R. Smith' and 'Robert Smith')."""

    name = "same_birth_date_different_given"
    ref = ComponentRef.of("constraint", "same_birth_date_different_given", "1", {"jw": 0.88})

    def cannot_link(self, a: FeatureView, b: FeatureView) -> str | None:
        da, db = single(a, "dob"), single(b, "dob")
        fa, fb = single(a, "pname.family"), single(b, "pname.family")
        ga, gb = single(a, "pname.given"), single(b, "pname.given")
        if not (da and da == db and fa and fb and ga and gb and _similar(fa, fb, 0.88)):
            return None
        if values(a, "pname.groups") & values(b, "pname.groups"):
            return None
        if (len(ga) == 1 or len(gb) == 1) and ga[0] == gb[0]:
            return None
        if _similar(ga, gb, 0.88):
            return None
        ma, mb = single(a, "pname.middle") or "", single(b, "pname.middle") or ""
        if (ma and ma[0] == gb[0]) or (mb and mb[0] == ga[0]):
            return None
        return "same birth date, different given names"


class GenerationalSuffixConflict:
    """'Jr.' and 'Sr.' (or 'II' and 'III') on the same name are two people by
    definition. A record without a suffix can be either."""

    name = "generational_suffix_conflict"
    ref = ComponentRef.of("constraint", "generational_suffix_conflict", "1", {})

    def cannot_link(self, a: FeatureView, b: FeatureView) -> str | None:
        sa, sb = single(a, "pname.suffix"), single(b, "pname.suffix")
        if sa and sb and sa != sb:
            return f"generational suffixes {sa} and {sb}"
        return None
