from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from thisness.comparisons.person import (
    DateOfBirthComparison,
    DateOfBirthConflict,
    GenerationalSuffixConflict,
    PersonNameComparison,
    SameBirthDateDifferentGiven,
    SameIdentifier,
)
from thisness.normalize.person import (
    Address,
    Employer,
    canonical_given,
    date_of_birth,
    email_address,
    name_groups,
    parse_name,
    phone_number,
    soundex,
)
from thisness.profiles.person import person


def features(**attributes) -> dict[str, list[str]]:
    """Every person normalizer over one record, as a feature view."""
    obs = SimpleNamespace(attributes=attributes)
    out: dict[str, list[str]] = {}
    for normalizer in person().normalizers:
        for f in normalizer.normalize(obs):
            out.setdefault(f.feature, []).append(f.value)
    return out


# -- normalizers -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "given", "middle", "family", "suffix"),
    [
        ("Mohammad A. Khan", "mohammad", "a", "khan", ""),
        ("Khan, Mohammad A.", "mohammad", "a", "khan", ""),
        ("Dr. Jane Q. Public PhD", "jane", "q", "public", ""),
        ("John Smith, Jr.", "john", "", "smith", "jr"),
        ("Michael Smith Sr.", "michael", "", "smith", "sr"),
        ("Ludwig van Beethoven", "ludwig", "", "vanbeethoven", ""),
        ("Oscar de la Hoya", "oscar", "", "delahoya", ""),
        ("Conor O'Brien", "conor", "", "obrien", ""),
        ("José Müller", "jose", "", "muller", ""),
        ("J. Smith", "j", "", "smith", ""),
        ("Mr. Rahman", "mr", "", "rahman", ""),  # two tokens: kept, not stripped to one
    ],
)
def test_names_parse(raw, given, middle, family, suffix):
    assert parse_name(raw) == {"given": given, "middle": middle, "family": family,
                               "suffix": suffix}  # fmt: skip


def test_single_tokens_are_not_names():
    assert parse_name("Cher") is None


def test_nicknames_and_transliterations_share_groups():
    assert canonical_given("bob") == canonical_given("robert") == "robert"
    assert canonical_given("mohammed") == canonical_given("muhammad")
    assert "samantha" in name_groups("sam") and "samuel" in name_groups("sam")
    assert canonical_given("zebedee") == "zebedee"
    assert soundex("smith") == soundex("smyth") == "s530"
    assert soundex("robert") == "r163"


def test_name_features_block_and_compare():
    f = features(name="Bob Smith", date_of_birth="1980-02-03")
    assert f["pname.key"] == ["robert smith"] and f["pname.given"] == ["bob"]
    assert f["pname.family_initial"] == ["smith b"]
    assert f["pname.given_dob"] == ["robert 1980-02-03"]
    assert features(given_name="Ann", family_name="Lee")["pname.key"] == ["ann lee"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("John.Smith+news@Gmail.com", "johnsmith@gmail.com"),
        ("john.smith@googlemail.com", "johnsmith@gmail.com"),
        ("j.smith+x@outlook.com", "j.smith@outlook.com"),  # dots matter outside Gmail
        ("j.smith+x@acme.com", "j.smith+x@acme.com"),  # tags only for known providers
        ("not-an-email", None),
        ("a@b@c.com", None),
    ],
)
def test_emails_normalize(raw, expected):
    assert email_address(raw) == expected


@pytest.mark.parametrize(
    ("raw", "country", "expected"),
    [
        ("(312) 555-0142", "US", "13125550142"),
        ("+1 312.555.0142", None, "13125550142"),
        ("1-312-555-0142", "US", "13125550142"),
        ("020 7946 0018", "GB", "442079460018"),
        ("+44 20 7946 0018", "DE", "442079460018"),  # an explicit code wins
        ("0049 30 1234567", None, "49301234567"),
        ("312-555-0142 ext. 12", "US", "13125550142"),
        ("12345", "US", None),
        ("0301234567", None, "?301234567"),  # no country: kept, marked national
    ],
)
def test_phones_normalize(raw, country, expected):
    assert phone_number(raw, country) == expected


@pytest.mark.parametrize(
    ("raw", "country", "expected"),
    [
        ("1985-03-07", None, ("1985-03-07", "1985-03", "1985")),
        ("03/07/1985", "US", ("1985-03-07", "1985-03", "1985")),
        ("03/07/1985", "United Kingdom", ("1985-07-03", "1985-07", "1985")),
        ("25/12/1990", "US", ("1990-12-25", "1990-12", "1990")),  # day over 12: unambiguous
        ("1985-03", None, ("", "1985-03", "1985")),
        ("1985", None, ("", "", "1985")),
        ("1985-02-30", None, None),
        ("1850-01-01", None, None),
        ("soon", None, None),
    ],
)
def test_dates_of_birth_normalize(raw, country, expected):
    assert date_of_birth(raw, country) == expected


def test_employer_and_address_keys():
    norm = Employer().normalize(SimpleNamespace(attributes={"employer": "Acme Logistics, Inc."}))
    assert [f.value for f in norm] == ["acmelogistics"]
    a = Address().normalize(SimpleNamespace(attributes={"address_line": "12 Oak Street #4",
                                                        "postal_code": "60601"}))  # fmt: skip
    b = Address().normalize(SimpleNamespace(attributes={"address_line": "12 oak st apt 4",
                                                        "postal_code": "60601"}))  # fmt: skip
    assert a[0].value == b[0].value == "12 oak st apt 4 60601"


# -- comparisons and constraints ----------------------------------------------------------------

NAME = PersonNameComparison()


@pytest.mark.parametrize(
    ("a", "b", "level"),
    [
        ("John A. Smith", "Smith, John A.", "exact"),
        ("John A. Smith", "John Smith", "given_family"),
        ("Bob Smith", "Robert Smith", "alias"),
        ("Mohammed Khan", "Muhammad Khan", "alias"),
        ("M. Khan", "Mohammad Khan", "initial"),
        ("Jon Smith", "John Smyth", "fuzzy"),
        ("Smith John", "John Smith", "fuzzy"),  # order swapped
        ("John A. Smith", "John B. Smith", "middle_conflict"),
        ("John Smith Jr.", "John Smith Sr.", "middle_conflict"),
        ("Mary Jones", "Mary Clarke", "given_only"),
        ("Mary Jones", "Peter Jones", "family_only"),
        ("Mary Jones", "Peter Clarke", "else"),
    ],
)
def test_name_levels(a, b, level):
    assert NAME.levels[NAME.compare(features(name=a), features(name=b))] == level


def test_date_of_birth_levels_and_conflicts():
    dob = DateOfBirthComparison()

    def level(x, y):
        return dob.levels[dob.compare(features(date_of_birth=x), features(date_of_birth=y))]

    assert level("1985-03-07", "1985-03-07") == "exact"
    assert level("1985-03-07", "1985-07-03") == "transposed"
    assert level("1985-03", "1985-03-07") == "year_month"
    assert level("1985", "1985-03-07") == "year"
    assert level("1985-03-07", "1986-03-07") == "different"
    assert dob.compare(features(), features(date_of_birth="1985")) is None
    conflict = DateOfBirthConflict()
    assert conflict.cannot_link(features(date_of_birth="1985-03-07"),
                                features(date_of_birth="1985-03-08"))  # fmt: skip
    assert not conflict.cannot_link(features(date_of_birth="1985-03-07"),
                                    features(date_of_birth="1985-07-03"))  # fmt: skip


def test_suffixes_and_twins_are_kept_apart():
    suffix = GenerationalSuffixConflict()
    assert suffix.cannot_link(features(name="John Smith Jr."), features(name="John Smith Sr."))
    assert not suffix.cannot_link(features(name="John Smith Jr."), features(name="John Smith"))
    twins = SameBirthDateDifferentGiven()
    born = {"date_of_birth": "2001-05-06"}
    assert twins.cannot_link(features(name="Anna Berg", **born), features(name="Lena Berg", **born))
    # Same person, by nickname, by initial, by a typo, or known by a middle name: no.
    for other in ("Annie Berg", "A. Berg", "Ana Berg"):
        assert not twins.cannot_link(features(name="Anna Berg", **born),
                                     features(name=other, **born))  # fmt: skip
    assert not twins.cannot_link(features(name="James R. Berg", **born),
                                 features(name="Robert Berg", **born))  # fmt: skip


def test_a_shared_identifier_matches_unless_birth_dates_disagree():
    rule = SameIdentifier()
    assert rule.apply({"ident": "exact", "date_of_birth": None}) == "MATCH"
    assert rule.apply({"ident": "exact", "date_of_birth": "different"}) is None


def test_relationship_patterns_need_every_level():
    profile = person()
    assert profile.relationship_signals({"name": "family_only", "address": "exact"}) == [
        "household"
    ]
    assert profile.relationship_signals({"name": "family_only", "address": "else"}) == []
    assert ("address", "exact") in profile.relationship_level_pairs


# -- engine ----------------------------------------------------------------------------------


def test_a_small_store_resolves_people_with_the_starter(store, ids):
    now = datetime(2026, 10, 8, tzinfo=UTC)
    engine = __import__("thisness").Thisness(store, profiles=[person()], ids=ids,
                                              clock=lambda: now)  # fmt: skip
    rows = [
        {"name": "Robert Smith", "email": "bob.smith@gmail.com", "date_of_birth": "1980-02-03"},
        {"name": "Smith, Bob", "email": "bobsmith@gmail.com", "phone": "(312) 555-0142",
         "country": "US"},
        {"name": "Bob Smith", "phone": "+1 312 555 0142", "date_of_birth": "02/03/1980",
         "country": "US"},
        {"name": "Robert Smith", "date_of_birth": "1975-11-30"},
    ]  # fmt: skip
    obs = [engine.observe("person", r, {"type": "crm", "id": str(i)}) for i, r in enumerate(rows)]
    engine.resolve_many("person", obs)
    assert engine.model_source("person") == "starter"
    entity = {o.observation_id: engine.entity_of(o.observation_id) for o in obs}
    assert entity[obs[0].observation_id] == entity[obs[1].observation_id] \
        == entity[obs[2].observation_id]  # fmt: skip
    assert entity[obs[3].observation_id] != entity[obs[0].observation_id]  # another birth date
    dossier = engine.dossier(entity[obs[0].observation_id])
    phone = next(a for a in dossier.attributes if a.attribute == "phone")
    assert len(phone.values) == 1 and phone.observed == 2  # two spellings, one number
    text = dossier.to_context()
    assert "bob.smith@gmail.com" not in text and "1980" not in text  # hashed, redacted
