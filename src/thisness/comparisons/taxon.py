"""Taxon comparisons and constraints (design 06 §4.3, §4.4)."""

from __future__ import annotations

from collections.abc import Mapping

from rapidfuzz.distance import JaroWinkler

from thisness.comparisons.base import FeatureView, Polarity, single, values
from thisness.comparisons.organization import _SetComparison
from thisness.core.components import ComponentRef


def _ref(name: str, version: str, config: dict) -> ComponentRef:
    return ComponentRef.of("comparison", name, version, config)


class TaxonNameComparison:
    """Most specific first. A recombination (the same epithet moved to another genus) is
    the domain's characteristic positive; the same genus with another epithet is a
    sister species, a relationship and never identity."""

    name = "name"
    levels = ("exact", "gender_variant", "abbreviated_genus", "fuzzy", "recombination",
              "same_genus", "different")  # fmt: skip
    features = ("taxon.canonical", "taxon.genus", "taxon.genus_initial", "taxon.stem")
    attributes = frozenset({"name"})
    polarity: Mapping[str, Polarity] = {
        "exact": "supports", "gender_variant": "supports", "abbreviated_genus": "supports",
        "fuzzy": "supports", "recombination": "supports", "same_genus": "neutral",
        "different": "contradicts",
    }  # fmt: skip
    jw = 0.92
    ref = _ref("taxon_name", "1", {"levels": levels, "jw": jw})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        sa, sb = single(a, "taxon.stem"), single(b, "taxon.stem")
        if not sa or not sb:
            return None  # a genus alone ("Quercus sp.") says nothing about the species
        ca, cb = single(a, "taxon.canonical"), single(b, "taxon.canonical")
        ga, gb = single(a, "taxon.genus"), single(b, "taxon.genus")
        if ca and ca == cb:
            return 0
        if ga and ga == gb and sa == sb:
            return 1
        if (not ga or not gb) and single(a, "taxon.genus_initial") == single(b, "taxon.genus_initial") \
                and sa == sb:  # fmt: skip
            return 2
        if ga and gb and JaroWinkler.similarity(ga, gb) >= self.jw \
                and JaroWinkler.similarity(sa, sb) >= self.jw:  # fmt: skip
            return 3
        if sa == sb:
            return 4
        if ga and ga == gb:
            return 5
        return 6


class AuthorshipComparison:
    """'Basionym match' is the recombination's signature: one name's author appears in
    the other's parentheses, as L. in *Caesalpinia pulcherrima* (L.) Sw. Two different
    authors on one canonical name usually mean two names: a later homonym. Across
    genera, authors differ by nature and are not compared, except as basionym evidence.

    'Homonym' is its own level because it means something else than 'different': two
    synonyms in one genus differ in author as a matter of course, while one name with two
    authors is two names (*Quercus ilex* L. and *Quercus ilex* Lour.)."""

    name = "authorship"
    levels = ("same", "basionym_match", "compatible", "homonym", "different")
    features = ("taxon.authors", "taxon.basionym_authors", "taxon.genus", "taxon.canonical")
    # The author part of a name, not the name: blocking on a species name says nothing
    # about who authored it, so EM can learn authorship within name-blocked sessions.
    attributes = frozenset({"authorship"})
    polarity: Mapping[str, Polarity] = {"same": "supports", "basionym_match": "supports",
                                        "compatible": "supports", "homonym": "contradicts",
                                        "different": "contradicts"}  # fmt: skip
    ref = _ref("taxon_authorship", "2", {"levels": levels})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        aa, ab = single(a, "taxon.authors"), single(b, "taxon.authors")
        pa, pb = single(a, "taxon.basionym_authors"), single(b, "taxon.basionym_authors")
        if not (aa or pa) or not (ab or pb):
            return None
        if aa and aa == ab and (pa == pb or not pa or not pb):
            return 0  # the basionym author is often left off: "Sw." for "(L.) Sw."
        if (pa and pa in (ab, pb)) or (pb and pb in (aa, pa)):
            return 1
        ga, gb = single(a, "taxon.genus"), single(b, "taxon.genus")
        if ga and gb and ga != gb:
            # In another genus a name has another combining author by nature: authors
            # are compared only within one genus, or as the basionym evidence above.
            return None
        if (
            aa
            and ab
            and min(len(aa), len(ab)) >= 3
            and (aa.startswith(ab) or ab.startswith(aa) or aa.endswith(ab) or ab.endswith(aa))
        ):
            return 2  # 'Karst.' and 'H.Karst.', 'Benth.' and 'Bentham'
        ca = single(a, "taxon.canonical")
        return 3 if ca and ca == single(b, "taxon.canonical") else 4


class RankComparison:
    name = "rank"
    levels = ("same", "different")
    features = ("taxon.rank",)
    attributes = frozenset({"rank"})
    polarity: Mapping[str, Polarity] = {"same": "supports", "different": "contradicts"}
    ref = _ref("taxon_rank", "1", {})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        ra, rb = single(a, "taxon.rank"), single(b, "taxon.rank")
        if not ra or not rb:
            return None
        return 0 if ra == rb else 1


class AssertionComparison:
    """What sources assert (design 06 §3). The reference authority's assertion that two
    records name one accepted taxon is its own level, since the reference defines the
    entity; another source's agreement is weighted evidence; two different asserted
    accepted names are one source disagreeing with another, or two taxa."""

    name = "assertion"
    levels = ("reference_same", "asserted_same", "asserted_different")
    features = ("taxon.accepted", "taxon.reference_accepted", "taxon.canonical")
    attributes = frozenset({"accepted_name", "taxonomic_status"})
    polarity: Mapping[str, Polarity] = {"reference_same": "supports",
                                        "asserted_same": "supports",
                                        "asserted_different": "contradicts"}  # fmt: skip
    ref = _ref("taxon_assertion", "3", {"levels": levels})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        xa, xb = single(a, "taxon.accepted"), single(b, "taxon.accepted")
        ra, rb = single(a, "taxon.reference_accepted"), single(b, "taxon.reference_accepted")
        ca, cb = single(a, "taxon.canonical"), single(b, "taxon.canonical")
        if xa and xb:
            # Both sources say where their record belongs: compare the two assertions.
            # Two sources listing one name under different accepted taxa disagree, even
            # though the names are identical.
            if xa != xb:
                return 2
            # The reference vouches for a record only through a name it lists: both
            # records its own, or its assertion about one record naming the other. WFO
            # accepting Y says nothing about a name X that another checklist sinks in Y.
            return 0 if (ra and rb) or (ra and ra == cb) or (rb and rb == ca) else 1
        if xa and xa == cb:
            return 0 if ra else 1
        if xb and xb == ca:
            return 0 if rb else 1
        return None


class VernacularComparison(_SetComparison):
    """A shared vernacular name in one language: weak, and capped ('bluebell' names
    different plants in Scotland and England)."""

    name = "vernacular"
    feature = "taxon.vernacular"
    features = ("taxon.vernacular",)
    attributes = frozenset({"vernacular_names"})
    polarity: Mapping[str, Polarity] = {"exact": "supports", "else": "neutral"}
    ref = _ref("taxon_vernacular", "1", {})


class FamilyComparison:
    name = "family"
    levels = ("same", "different")
    features = ("taxon.family",)
    attributes = frozenset({"family"})
    polarity: Mapping[str, Polarity] = {"same": "supports", "different": "contradicts"}
    ref = _ref("taxon_family", "1", {})

    def compare(self, a: FeatureView, b: FeatureView) -> int | None:
        fa, fb = single(a, "taxon.family"), single(b, "taxon.family")
        if not fa or not fb:
            return None
        return 0 if fa == fb else 1


class NameIdentifierComparison(_SetComparison):
    """WFO and IPNI identifiers name names, not taxa: two synonyms have different ids.
    A shared id is strong; different ids say nothing."""

    name = "ident"
    feature = "ident"
    features = ("ident",)
    attributes = frozenset({"identifiers"})
    polarity: Mapping[str, Polarity] = {"exact": "supports", "else": "neutral"}
    ref = _ref("taxon_name_ident", "1", {"names_not_taxa": True})


class KingdomConflict:
    """One genus name can be a plant and an animal (*Oenanthe* is both): different
    kingdoms are different taxa, whatever the names say."""

    name = "kingdom_conflict"
    ref = ComponentRef.of("constraint", "kingdom_conflict", "1", {})

    def cannot_link(self, a: FeatureView, b: FeatureView) -> str | None:
        ka, kb = single(a, "taxon.kingdom"), single(b, "taxon.kingdom")
        return f"kingdoms {ka} and {kb}" if ka and kb and ka != kb else None


class AcceptedByReference:
    """The reference authority asserts that the two records name one accepted taxon:
    MATCH. Another source asserting otherwise is a contradicting edge, so a disagreement
    between authorities goes to review rather than being settled silently."""

    name = "accepted_by_reference"
    ref = ComponentRef.of("resolver", "accepted_by_reference", "1", {})

    def apply(self, levels: Mapping[str, str | None]) -> str | None:
        return "MATCH" if levels.get("assertion") == "reference_same" else None


class SameNameIdentifier:
    """The same name identifier in one scheme: the same name, so the same taxon unless a
    kingdom conflict says otherwise."""

    name = "same_name_identifier"
    ref = ComponentRef.of("resolver", "same_name_identifier", "1", {})

    def apply(self, levels: Mapping[str, str | None]) -> str | None:
        return "MATCH" if levels.get("ident") == "exact" else None


__all__ = ["AcceptedByReference", "AssertionComparison", "AuthorshipComparison", "FamilyComparison", "KingdomConflict",
           "NameIdentifierComparison", "RankComparison", "SameNameIdentifier",
           "TaxonNameComparison", "VernacularComparison", "values"]  # fmt: skip
