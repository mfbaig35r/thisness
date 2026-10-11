from __future__ import annotations

import pytest

from thisness.comparisons.organization import (
    DomainComparison,
    LegalFormComparison,
    LocationComparison,
    NameComparison,
    RegistrationComparison,
    RegistrationConflict,
    SameRegistration,
)
from thisness.core import Observation
from thisness.profiles.organization import organization

PROFILE = organization()


def view(ids, now, **attributes):
    """Features exactly as the profile's normalizers produce them."""
    obs = Observation.new(ids=ids, entity_type="organization", attributes=attributes,
                          source={"type": "t", "id": "t"}, ingested_at=now)  # fmt: skip
    table = PROFILE.features([obs])
    out: dict[str, list[str]] = {}
    for feature, value in zip(table.column("feature").to_pylist(),
                              table.column("value").to_pylist(), strict=True):  # fmt: skip
        out.setdefault(feature, []).append(value)
    return out


@pytest.mark.parametrize(
    ("left", "right", "level"),
    [
        ("IBM Corp.", "I.B.M. Corporation", "exact_key"),
        ("Granite Logistics", "Granitte Logistics", "token_typo"),
        ("Logistics Granite", "Granite Logistics", "token_set_0.92"),
        ("Logistics Granite", "Granite Logistics Group", "token_subset"),
        ("Bridgepoint Advisers UK", "Bridgepoint Advisers Europe", "qualifier_differs"),
        ("Acme International Services", "Acme Intl Services Group", "token_subset"),
        ("Fund II", "Fund III", "number_differs"),
        ("Omega Holdings 2", "Omega Holdings 3", "number_differs"),
        ("International Business Machines", "IBM", "acronym"),
        # One brand, another business: a relationship signal, never a typo (design 07 §13.2).
        ("Libid Holdings", "Libid Solutions", "qualifier_differs"),
        ("Acme Freight", "Zenith Foods", "else"),
    ],
)
def test_name_levels(ids, now, left, right, level):
    name = NameComparison()
    got = name.compare(view(ids, now, name=left), view(ids, now, name=right))
    assert name.levels[got] == level


def test_brand_only_is_not_mistaken_for_a_typo(ids, now):
    # Whole-string Jaro-Winkler rewards the shared prefix; token comparison does not.
    name = NameComparison()
    level = name.compare(view(ids, now, name="Libid Holdings"), view(ids, now, name="Libid Solns"))
    assert name.levels[level] not in ("exact_key", "token_typo", "token_set_0.92")
    assert ("name", name.levels[level]) in organization().relationship_level_pairs


def test_registration_compares_within_a_scheme_only(ids, now):
    reg = RegistrationComparison()
    ein = view(ids, now, name="A", registration_ids=["EIN:123456789"])
    other_ein = view(ids, now, name="A", registration_ids=["EIN:987654321"])
    crn = view(ids, now, name="A", registration_ids=["CRN:01234567"])
    both = view(ids, now, name="A", registration_ids=["CRN:01234567", "EIN:123456789"])
    assert reg.compare(ein, ein) == 0
    assert reg.compare(ein, other_ein) == 1
    assert reg.compare(ein, crn) is None  # different schemes say nothing
    assert reg.compare(both, crn) == 0
    assert reg.compare(view(ids, now, name="A"), ein) is None


def test_domain_and_legal_form(ids, now):
    a = view(ids, now, name="Acme Inc", website="https://acme.com")
    b = view(ids, now, name="Acme LLC", email="ops@acme.com")
    c = view(ids, now, name="Acme Inc", domain="acme.co.uk")
    assert DomainComparison().compare(a, b) == 0
    assert DomainComparison().compare(a, c) == 1
    assert DomainComparison().compare(a, view(ids, now, name="Acme")) is None
    assert LegalFormComparison().compare(a, c) == 0
    assert LegalFormComparison().compare(a, b) == 1


@pytest.mark.parametrize(
    ("left", "right", "level"),
    [
        ({"country": "US", "postal_code": "60601", "city": "Chicago"},
         {"country": "USA", "postal_code": "60601"}, "postal"),
        ({"city": "Chicago"}, {"city": "chicago"}, "city"),
        ({"city": "Chicago", "postal_code": "60601"}, {"city": "Chicago", "postal_code": "60602"},
         "city_other_postal"),
        ({"city": "Chicago"}, {"city": "Chicgo"}, "city_fuzzy"),
        ({"country": "US"}, {"country": "United States", "city": "Denver"}, "country_only"),
        ({"country": "US", "postal_code": "60601"}, {"country": "GB", "postal_code": "60601"},
         "different_country"),
        ({"city": "Chicago"}, {}, None),
    ],
)  # fmt: skip
def test_location_levels(ids, now, left, right, level):
    loc = LocationComparison()
    got = loc.compare(view(ids, now, name="A", **left), view(ids, now, name="A", **right))
    assert (None if got is None else loc.levels[got]) == level


def test_registration_conflict_and_rule(ids, now):
    conflict = RegistrationConflict()
    ein = view(ids, now, name="A", registration_ids=["EIN:111111111"])
    other = view(ids, now, name="A", registration_ids=["EIN:222222222"])
    shared = view(ids, now, name="A", registration_ids=["EIN:222222222", "EIN:111111111"])
    assert conflict.cannot_link(ein, other) == "different EIN identifiers"
    assert conflict.cannot_link(ein, shared) is None
    assert conflict.cannot_link(ein, view(ids, now, name="A")) is None
    rule = SameRegistration()
    assert rule.apply({"reg_id": "exact", "location": "city"}) == "MATCH"
    assert rule.apply({"reg_id": "exact", "location": "different_country"}) is None
    assert rule.apply({"reg_id": "else"}) is None
