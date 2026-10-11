from __future__ import annotations

import pytest

from thisness.core import Observation
from thisness.normalize.organization import (
    Domains,
    Location,
    OrganizationName,
    RegistrationIds,
    country_code,
    lei_is_valid,
    name_parts,
    registrable_domain,
    registration_id,
    trigrams,
)
from thisness.profiles.organization import organization

VALID_LEI = "HWUPKR0MPOU8FGXBT394"


@pytest.mark.parametrize(
    ("raw", "tokens", "legal"),
    [
        ("IBM Corp.", ["ibm"], "corp"),
        ("I.B.M. Corporation", ["ibm"], "corp"),
        ("International Business Machines Corporation",
         ["international", "business", "machines"], "corp"),
        ("Nestlé S.A.", ["nestle"], "sa"),
        ("The Home Depot, Inc.", ["home", "depot"], "inc"),
        ("AT&T Inc.", ["at", "and", "t"], "inc"),
        ("AT and T", ["at", "and", "t"], None),
        ("Acme Intl Mfg LLC", ["acme", "international", "manufacturing"], "llc"),
        ("Barr GmbH & Co. KG", ["barr"], "gmbh co kg"),
        ("J.P. Morgan Chase & Co.", ["jp", "morgan", "chase"], "co"),
        ("Acme Holdings Pty Ltd", ["acme", "holdings"], "pty ltd"),
        # Legal forms are stripped only from the end, and never the whole name.
        ("A.G. Barr plc", ["ag", "barr"], "plc"),
        ("Co-operative Bank", ["co", "operative", "bank"], None),
        ("Company", ["company"], None),
        ("Acme Tech", ["acme", "technology"], None),
        ("ACME TECHNOLOGIES", ["acme", "technology"], None),
    ],
)  # fmt: skip
def test_name_parts(raw, tokens, legal):
    assert name_parts(raw) == (tokens, legal)


def test_trigrams_are_sorted_and_unique():
    assert trigrams("aaaa") == ["aaa"]
    assert trigrams("ibm") == ["ibm"]
    assert trigrams("ab") == ["ab"]
    assert trigrams("acme") == ["acm", "cme"]


@pytest.mark.parametrize(
    ("raw", "domain"),
    [
        ("https://www.ibm.com/us-en?x=1", "ibm.com"),
        ("IBM.COM", "ibm.com"),
        ("jane.doe@research.ibm.com", "ibm.com"),
        ("jane@ibm.co.uk", "ibm.co.uk"),
        ("http://shop.example.com.au:8080/", "example.com.au"),
        ("someone@gmail.com", None),  # free mail says nothing about the employer
        ("not a domain", None),
        ("localhost", None),
    ],
)
def test_registrable_domain(raw, domain):
    assert registrable_domain(raw) == domain


def test_lei_check_digits():
    assert lei_is_valid(VALID_LEI)
    assert not lei_is_valid(VALID_LEI[:-1] + "5")
    assert not lei_is_valid("TOO-SHORT")


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("EIN: 12-3456789", "EIN:123456789"),
        (f"lei:{VALID_LEI.lower()}", f"LEI:{VALID_LEI}"),
        (f"LEI:{VALID_LEI[:-1]}5", None),  # fails its check digits: dropped, not a conflict
        ("no scheme here", None),
        ("EIN:", None),
    ],
)
def test_registration_id(raw, normalized):
    assert registration_id(raw) == normalized


@pytest.mark.parametrize(
    ("raw", "code"),
    [("United States", "US"), ("U.S.A.", "US"), ("gb", "GB"), ("Deutschland", "DE"),
     ("Narnia", None)],
)  # fmt: skip
def test_country_code(raw, code):
    assert country_code(raw) == code


def _obs(ids, now, **attributes):
    return Observation.new(
        ids=ids,
        entity_type="organization",
        attributes=attributes,
        source={"type": "test", "id": "t"},
        ingested_at=now,
    )


def test_name_features(ids, now):
    feats = OrganizationName().normalize(
        _obs(ids, now, name="International Business Machines Corp")
    )
    by_name: dict[str, list[str]] = {}
    for f in feats:
        by_name.setdefault(f.feature, []).append(f.value)
    assert by_name["name.key"] == ["internationalbusinessmachines"]
    assert by_name["name.tokens"] == ["international", "business", "machines"]
    assert by_name["name.acronym"] == ["ibm"]
    assert by_name["legal_form"] == ["corp"]
    assert by_name["name.trigram"] == sorted(set(by_name["name.trigram"]))


def test_multi_valued_features_are_deduplicated(ids, now):
    obs = _obs(
        ids,
        now,
        name="Acme",
        domain=["acme.com", "ACME.COM"],
        website="https://www.acme.com",
        email=["a@acme.com", "b@gmail.com"],
        registration_ids=["EIN:12-3456789", "EIN 123456789", "EIN:123456789"],
    )
    assert [f.value for f in Domains().normalize(obs)] == ["acme.com"]
    assert [f.value for f in RegistrationIds().normalize(obs)] == ["EIN:123456789"]


def test_location_features(ids, now):
    obs = _obs(ids, now, name="Acme", country="U.K.", city="  São   Paulo ", postal_code="sw1a 1aa")
    assert [(f.feature, f.value) for f in Location().normalize(obs)] == [
        ("country", "GB"),
        ("city", "sao paulo"),
        ("postal", "SW1A1AA"),
    ]


def test_profile_features_skip_other_types_and_redacted(ids, now):
    profile = organization()
    org = _obs(ids, now, name="IBM")
    person = Observation.new(ids=ids, entity_type="person", attributes={"name": "M. Khan"},
                             source={"type": "t", "id": "t"}, ingested_at=now)  # fmt: skip
    redacted = org.model_copy(update={"attributes": None, "redacted_at": now})
    table = profile.features([org, person, redacted])
    assert set(table.column("observation_id").to_pylist()) == {org.observation_id}
    assert set(table.column("normalizer_ref").to_pylist()) == {OrganizationName.ref.ref}


def test_profile_ref_tracks_its_components():
    assert organization().ref == organization().ref
    assert organization().ref != organization(version="2").ref
    assert organization().ref.ref.startswith("profile:organization@1#")
