"""The Taxon profile (design 06): plant names under the botanical Code.

An entity is a taxon as the reference authority (WFO by default) circumscribes it;
other authorities' views enter as observations from their own sources, so
disagreement is evidence, not a silent choice (design 06 §3).
"""

from __future__ import annotations

from thisness.candidates import CrossFeature, ExactValue, Overlap
from thisness.canonicalize import AcceptedByAuthority
from thisness.comparisons.taxon import (
    AcceptedByReference,
    AssertionComparison,
    AuthorshipComparison,
    FamilyComparison,
    KingdomConflict,
    NameIdentifierComparison,
    RankComparison,
    SameNameIdentifier,
    TaxonNameComparison,
    VernacularComparison,
)
from thisness.normalize.person import Identifiers
from thisness.normalize.taxon import Classification, TaxonName, VernacularNames, collapse_key
from thisness.profiles.base import (
    AssertionFields,
    AttributeDefinition,
    FeatureConflict,
    ResolutionProfile,
)

ATTRIBUTES = {
    "name": AttributeDefinition(name="name", data_type="string", nullable=False,
                                sensitivity="public", display="scientific name"),
    "authorship": AttributeDefinition(name="authorship", data_type="string",
                                      sensitivity="public"),
    "rank": AttributeDefinition(name="rank", data_type="string", sensitivity="public"),
    "family": AttributeDefinition(name="family", data_type="string", sensitivity="public"),
    "kingdom": AttributeDefinition(name="kingdom", data_type="string", sensitivity="public"),
    "accepted_name": AttributeDefinition(name="accepted_name", data_type="string",
                                         sensitivity="public"),
    "taxonomic_status": AttributeDefinition(name="taxonomic_status", data_type="string",
                                            sensitivity="public"),
    "vernacular_names": AttributeDefinition(name="vernacular_names", data_type="string",
                                            cardinality="multi", sensitivity="public"),
    # Names held by Indigenous and local communities (design 06 §7): matched locally,
    # never sent to a hosted model, and only from sources whose terms allow their use.
    "community_names": AttributeDefinition(name="community_names", data_type="string",
                                           cardinality="multi", sensitivity="restricted",
                                           external_model_policy="local_only"),
    "identifiers": AttributeDefinition(name="identifiers", data_type="string",
                                       cardinality="multi", sensitivity="public"),
}  # fmt: skip

NAME = frozenset({"name"})

# Guidance added to every model prompt for taxa (design 06 §6). The rules of botanical
# nomenclature are the domain's knowledge and may be used; which names are synonyms is
# an authority's opinion, so it comes from the records, never from the model's memory.
HINTS = """\
- Records name plants under the botanical Code: genus, epithet, an optional rank marker and infraspecific epithet ("subsp.", "var.", "f."), then authorship.
- The same name written differently is the same taxon: typos, case, an abbreviated genus ("C. pulcherrima"), rank markers written differently ("var." or "v."), author abbreviations ("L." or "Linn."), and the basionym author in parentheses present or missing ("(L.) Sw." or "Sw.").
- An epithet agrees in gender with its genus: "pulcherrimus", "pulcherrima" and "pulcherrimum" are one epithet.
- A recombination is the same taxon: the same epithet moved to another genus with the original author in parentheses ("Poinciana pulcherrima L." and "Caesalpinia pulcherrima (L.) Sw."). The same epithet in another genus without that evidence may be a recombination or a different plant (epithets such as "japonica", "alba" or "dentata" occur in many genera): answer "unsure" unless the records settle it.
- A species and a subspecies, variety or form under it, or one epithet at two infraspecific ranks ("var. depressa" and "f. depressa"), may be one taxon or several: many such names are synonyms in one authority and distinct taxa in another. From the names alone answer "related" or "unsure"; answer "same" when an authority record in the input places them in one taxon.
- Two different species epithets in one genus are usually different taxa and sometimes synonyms: answer "related" ("congeneric_with") unless the records show more.
- Answer "different" only when something in the input contradicts identity: different families or kingdoms; one name with two different authors ("Quercus ilex L." and "Quercus ilex Lour.", a later homonym); or an authority record placing the two names in different accepted taxa. A wrong "different" is costly; "related" and "unsure" leave the pair to a reviewer.
- A record from an authority may give its status and accepted name. That is the authority's opinion: cite it. Do not decide that two different names are synonyms from your own knowledge of the literature; when only knowledge outside the input would join them, answer "unsure"."""


def taxon(version: str = "1", *, authority: str = "wfo") -> ResolutionProfile:
    return ResolutionProfile(
        entity_type="taxon",
        id_prefix="tax",
        version=version,
        attributes=ATTRIBUTES,
        normalizers=(TaxonName(authority), Classification(),
                     VernacularNames(("vernacular_names", "community_names")), Identifiers()),
        candidate_generators=(
            ExactValue("canonical", "taxon.canonical", NAME, max_block=200),
            ExactValue("stem_key", "taxon.stem_key", NAME, max_block=100),
            # An abbreviated genus ("C. pulcherrima") meets its full form here.
            ExactValue("initial_key", "taxon.initial_key", NAME, max_block=100),
            # A recombination keeps its epithet in a new genus.
            ExactValue("epithet_key", "taxon.epithet_key", NAME, max_block=40),
            # A source's asserted accepted name meets that name, and two records asserting
            # the same accepted name meet each other (design 06 §3).
            # Blocked on the assertion, not on either record's own name, so the name
            # comparison can be learned here (a synonym's name differs from its taxon's).
            CrossFeature("asserted", "taxon.accepted", "taxon.canonical",
                         frozenset({"accepted_name"}), max_block=100),
            ExactValue("accepted", "taxon.accepted", frozenset({"accepted_name"}), max_block=100),
            Overlap("trigram", "taxon.trigram", NAME, min_share=0.7, max_df=200),
            ExactValue("vernacular", "taxon.vernacular",
                       frozenset({"vernacular_names", "community_names"}), max_block=30),
            ExactValue("identifier", "ident", frozenset({"identifiers"}), max_block=20),
        ),
        comparisons=(TaxonNameComparison(), AuthorshipComparison(), RankComparison(),
                     AssertionComparison(), VernacularComparison(), FamilyComparison(),
                     NameIdentifierComparison()),
        constraints=(KingdomConflict(),),
        rules=(AcceptedByReference(), SameNameIdentifier()),
        # A shared common name is weak ('bluebell'); a shared genus is a sister species.
        # One name with two authors is two names under the Code (a later homonym). EM
        # cannot learn that: the level exists only on an exact name, which random pairs
        # almost never share, so its u from random pairs is near zero and the weight
        # comes out positive. The cap is the Code's rule, stated (design 06 §10.2).
        weight_caps={"vernacular": {"exact": 2.0}, "name": {"same_genus": 0.0},
                     "authorship": {"homonym": -8.0}},
        relationship_levels=frozenset({("name", "same_genus")}),
        relations=("congeneric_with", "misapplied_for", "hybrid_parent_of", "related_to"),
        contradictions=(FeatureConflict("taxon.kingdom", "kingdom"),
                        FeatureConflict("taxon.family", "family")),
        value_keys={"name": "taxon.canonical", "family": "taxon.family"},
        canonicalizer=AcceptedByAuthority(authority),
        assertions=AssertionFields("taxonomic_status", "accepted_name", authorship="authorship",
                                   reference=authority),
        min_strong_weight=12.0,
        # Synonyms in one genus differ in name by nature and weigh about zero; at the
        # default T_low of 0 they were negative edges inside their own taxon, which then
        # failed validation whole (design 06 §10.5).
        max_negative_weight=-4.0,
        starter_model="taxon_starter.json",
        prompt_hints=HINTS,
        # With WFO rows, decide mode with yielding reaches the oracle's recall (§10.7).
        model_role="decide",
        collapse_key=collapse_key,
    )  # fmt: skip
