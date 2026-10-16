"""The Person profile (design 04 §6)."""

from __future__ import annotations

from thisness.candidates import ExactValue
from thisness.comparisons.organization import LocationComparison
from thisness.comparisons.person import (
    AddressComparison,
    DateOfBirthComparison,
    DateOfBirthConflict,
    EmailComparison,
    EmployerComparison,
    GenerationalSuffixConflict,
    IdentifierComparison,
    IdentifierConflict,
    PersonNameComparison,
    PhoneComparison,
    SameBirthDateDifferentGiven,
    SameIdentifier,
)
from thisness.fellegi_sunter import TermFrequency
from thisness.normalize.organization import Location
from thisness.normalize.person import (
    Address,
    DateOfBirth,
    Emails,
    Employer,
    Identifiers,
    PersonName,
    Phones,
)
from thisness.profiles.base import (
    AttributeDefinition,
    FeatureConflict,
    RelationshipPattern,
    ResolutionProfile,
)

# Personal data defaults to the cautious side: identifiers never leave the machine, a
# date of birth is withheld from external models, contact details go out hashed.
ATTRIBUTES = {
    "name": AttributeDefinition(name="name", data_type="string", sensitivity="internal"),
    # How the source writes a name without a comma: given_first or family_first (design 09).
    "name_order": AttributeDefinition(name="name_order", data_type="string"),
    "given_name": AttributeDefinition(name="given_name", data_type="string"),
    "family_name": AttributeDefinition(name="family_name", data_type="string"),
    "email": AttributeDefinition(name="email", data_type="string", cardinality="multi",
                                 log_policy="hash", external_model_policy="hash"),
    "phone": AttributeDefinition(name="phone", data_type="string", cardinality="multi",
                                 log_policy="hash", external_model_policy="hash"),
    "date_of_birth": AttributeDefinition(name="date_of_birth", data_type="date",
                                         sensitivity="restricted", log_policy="hash",
                                         external_model_policy="redact"),
    "employer": AttributeDefinition(name="employer", data_type="string"),
    "address_line": AttributeDefinition(name="address_line", data_type="string",
                                        sensitivity="restricted", log_policy="hash",
                                        external_model_policy="local_only", display="address"),
    "city": AttributeDefinition(name="city", data_type="string"),
    "country": AttributeDefinition(name="country", data_type="string"),
    "postal_code": AttributeDefinition(name="postal_code", data_type="string",
                                       sensitivity="restricted"),
    "identifiers": AttributeDefinition(name="identifiers", data_type="string",
                                       cardinality="multi", sensitivity="restricted",
                                       identity_strength="strong", log_policy="redact",
                                       external_model_policy="forbidden"),
}  # fmt: skip

NAME = frozenset({"name", "given_name", "family_name", "name_order"})

# Guidance added to every model prompt for people (design 06 §6).
HINTS = """\
- Nicknames, initials and transliterations of one name are the same person when nothing contradicts it.
- People in one household share a family name, an address and often a phone number; that alone does not make them the same person. A father and son can share a full name.
- Different dates of birth, or different identifiers in the same scheme, mean different people."""


def person(version: str = "1") -> ResolutionProfile:
    return ResolutionProfile(
        entity_type="person",
        id_prefix="per",
        version=version,
        attributes=ATTRIBUTES,
        normalizers=(PersonName(), Emails(), Phones(), DateOfBirth(), Employer(), Address(),
                     Location(), Identifiers()),
        candidate_generators=(
            ExactValue("identifier", "ident", frozenset({"identifiers"}), max_block=20),
            ExactValue("email", "email", frozenset({"email"}), max_block=20),
            ExactValue("phone", "phone", frozenset({"phone"}), max_block=20),
            ExactValue("name_key", "pname.key", NAME, max_block=100),
            # Every reading of a name: a compound family name's other reading (design 09).
            ExactValue("name_reading", "pname.reading", NAME, max_block=100),
            ExactValue("family_initial", "pname.family_initial", NAME, max_block=100),
            ExactValue("phonetic", "pname.phonetic", NAME, max_block=100),
            # Same given name and birth date: finds married-name changes.
            ExactValue("given_dob", "pname.given_dob", NAME | {"date_of_birth"}, max_block=30),
            # Same given name and employer: the same, where there is no date of birth
            # (design 09). Colleagues share employers, so EM does not learn from it.
            ExactValue("given_employer", "pname.given_employer", NAME | {"employer"},
                       max_block=30),
            ExactValue("address", "address", frozenset({"address_line"}), max_block=30),
        ),
        comparisons=(PersonNameComparison(), EmailComparison(), PhoneComparison(),
                     DateOfBirthComparison(), EmployerComparison(), AddressComparison(),
                     LocationComparison(), IdentifierComparison()),
        constraints=(DateOfBirthConflict(), IdentifierConflict(), GenerationalSuffixConflict(),
                     SameBirthDateDifferentGiven()),
        rules=(SameIdentifier(),),
        # Shared by people who are not the same person (design 04 §6.4): households share
        # phones and addresses, carriers recycle numbers, colleagues share employers.
        # Contradicting name levels are bounded below zero too: random pairs almost never
        # land on them, so the m floor alone would make them look like support.
        weight_caps={"phone": {"exact": 4.0}, "address": {"exact": 3.0},
                     "employer": {"exact": 2.0},
                     "name": {"middle_conflict": -4.0, "family_only": -6.0, "initial": 8.0,
                              # Neither alone is a match (design 09 §11.5): an initial
                              # and a family name, or a suffix on one side only, which
                              # a father and son at one company share with employer
                              # and city; with those it reaches review, not a merge.
                              "suffix_one_side": 4.0}},
        relationship_patterns=(
            RelationshipPattern("household", (("name", "family_only"), ("address", "exact"))),
            RelationshipPattern("same_birth_date", (("name", "family_only"),
                                                    ("date_of_birth", "exact"))),
        ),
        term_frequency=TermFrequency("name", "pname.tf_key", exact_level=0,
                                     fuzzy_levels=(1, 2, 3, 4), count="observation"),
        contradictions=(FeatureConflict("dob", "date_of_birth"),
                        FeatureConflict("ident", "identifiers", scheme_separator=":")),
        value_keys={"name": "pname.key", "email": "email", "phone": "phone",
                    "date_of_birth": "dob", "employer": "employer",
                    "address_line": "address", "country": "country", "city": "city",
                    "postal_code": "postal"},
        relations=("spouse_of", "parent_of", "sibling_of", "household_member_of",
                   "colleague_of", "related_to"),
        min_strong_weight=12.0,
        em_excluded_generators=frozenset({"phone", "address", "given_employer"}),
        starter_model="person_starter.json",
        prompt_hints=HINTS,
        # Its "same" on hard pairs of people is right three times in four: it suggests,
        # a person decides (design 06 §10.9, §10.10).
        model_role="suggest",
    )  # fmt: skip
