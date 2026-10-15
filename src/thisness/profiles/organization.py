"""The Organization profile (design 03 §13)."""

from __future__ import annotations

from thisness.candidates import CrossFeature, ExactValue, Overlap, RareValue
from thisness.canonicalize import ReadableSpelling
from thisness.comparisons.organization import (
    DomainComparison,
    LegalFormComparison,
    LocationComparison,
    NameComparison,
    RegistrationComparison,
    RegistrationConflict,
    SameRegistration,
)
from thisness.fellegi_sunter import TermFrequency
from thisness.normalize.organization import (
    Domains,
    Location,
    OrganizationName,
    RegistrationIds,
    clean_name,
    name_parts,
)
from thisness.profiles.base import AttributeDefinition, FeatureConflict, ResolutionProfile

ATTRIBUTES = {
    "name": AttributeDefinition(name="name", data_type="string", nullable=False,
                                sensitivity="public"),
    "registration_ids": AttributeDefinition(name="registration_ids", data_type="string",
                                            cardinality="multi", sensitivity="internal",
                                            identity_strength="strong"),
    "domain": AttributeDefinition(name="domain", data_type="string", cardinality="multi",
                                  sensitivity="public"),
    "website": AttributeDefinition(name="website", data_type="string", sensitivity="public"),
    "email": AttributeDefinition(name="email", data_type="string", cardinality="multi",
                                 sensitivity="internal", log_policy="hash"),
    "country": AttributeDefinition(name="country", data_type="string", sensitivity="public"),
    "city": AttributeDefinition(name="city", data_type="string", sensitivity="public"),
    "postal_code": AttributeDefinition(name="postal_code", data_type="string",
                                       sensitivity="public"),
    "address_line": AttributeDefinition(name="address_line", data_type="string",
                                        sensitivity="internal", display="address"),
}  # fmt: skip

NAME = frozenset({"name"})

# Guidance added to every model prompt for organizations (design 06 §6).
HINTS = """\
- Legal-form differences ("Inc", "LLC", "Ltd", "GmbH") and account or reference numbers appended to a name ("Acme - 812773") usually name the same organization.
- A parent and its subsidiary, two companies in one group, and a brand and a different legal entity are different organizations, often related.
- Companies that share a word but are otherwise distinct are different: a shared brand with a different line of business ("Harjas Foods" and "Harjas Freight") is usually two related organizations, not one.
- A common name ("Premier Services") may belong to many unrelated organizations.
- Different registration identifiers in the same scheme mean different organizations."""


def _name_key(raw: str) -> tuple[str, str] | None:
    """A name's key and its cleaned text, for choosing the entity's label."""
    cleaned = clean_name(raw).text
    tokens, _ = name_parts(cleaned)
    return ("".join(tokens), cleaned) if tokens else None


def organization(version: str = "1") -> ResolutionProfile:
    return ResolutionProfile(
        entity_type="organization",
        id_prefix="org",
        version=version,
        attributes=ATTRIBUTES,
        normalizers=(OrganizationName(), RegistrationIds(), Domains(), Location()),
        candidate_generators=(
            ExactValue("identifier", "reg_id", frozenset({"registration_ids"}), max_block=50),
            ExactValue("domain", "domain", frozenset({"domain", "website", "email"}),
                       max_block=50),
            ExactValue("name_key", "name.key", NAME, max_block=100),
            RareValue("rare_token", "name.tokens", NAME, per_observation=2, max_df=60),
            CrossFeature("acronym", "name.acronym", "name.key", NAME, max_block=50),
            # A trade name proposes the company it names, and its sisters under one brand.
            CrossFeature("trade_name", "name.alias_key", "name.key", NAME, max_block=50),
            ExactValue("shared_trade_name", "name.alias_key", NAME, max_block=50),
            Overlap("trigram", "name.trigram", NAME, min_share=0.6, max_df=150),
        ),
        comparisons=(NameComparison(), RegistrationComparison(), DomainComparison(),
                     LocationComparison(), LegalFormComparison()),
        constraints=(RegistrationConflict(),),
        rules=(SameRegistration(),),
        # Random-pair u never sees related companies, so these levels would otherwise
        # count as strong identity evidence (design 03 §8.6). Re-set on the benchmark.
        # A name with words added ("Zodiac Maritime" and "Zodiac Maritime Chartering") or
        # another qualifier ("... UK", "... Europe") is mostly a group's other company on
        # real UK data: no evidence of identity, a sign of a relationship (design 07
        # §13.2). Capped at 2 bits, it still tipped such pairs over T_high at 5,000
        # companies, where a shared postcode weighs more.
        weight_caps={"name": {"brand_only": 0.0, "token_subset": 0.0, "qualifier_differs": 0.0,
                              "trade_name": 0.0, "truncated_prefix": 0.0},
                     "domain": {"exact": 4.0}},
        relationship_levels=frozenset({("name", "brand_only"), ("name", "token_subset"),
                                       ("name", "qualifier_differs"), ("name", "trade_name")}),
        # Frequency is looked up on the confusable-folded key (design 03 §21).
        # A name shared by many companies is weak evidence, and so is a postcode: 62% of
        # live UK companies share their registered address, a fifth of them with a
        # hundred others at a formation agent's or an accountant's (design 07 §13.2).
        term_frequency=(TermFrequency("name", "name.tf_key", exact_level=0, fuzzy_levels=(2, 5)),
                        # Counted over records: a first resolution pass would already
                        # have merged a group's companies, hiding how shared it is.
                        TermFrequency("location", "postal", exact_level=0,
                                      count="observation", boost=False)),
        contradictions=(FeatureConflict("reg_id", "registration_ids", scheme_separator=":"),
                        FeatureConflict("country", "country")),
        relations=("subsidiary_of", "parent_of", "sibling_of", "brand_of", "successor_of",
                   "related_to"),
        value_keys={"name": "name.key", "website": "domain", "country": "country", "city": "city",
                    "postal_code": "postal"},
        # Tuned on the benchmark for cluster-level false merges (design 03 §21).
        min_strong_weight=12.0,
        starter_model="organization_starter.json",
        # The label: the readable spelling of the most supported name (design 08 §5.1).
        canonicalizer=ReadableSpelling("name", _name_key, "organization_name"),
        prompt_hints=HINTS,
        # gpt-6-luna on 805 organizations: R .888 -> .981 for 3 more false-merge clusters.
        model_role="decide",
    )  # fmt: skip
