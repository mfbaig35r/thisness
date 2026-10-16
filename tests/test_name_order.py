"""Name order (design 09 §3): family-first sources, and compound family names."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from thisness.comparisons.person import PersonNameComparison
from thisness.normalize.person import parse_name
from thisness.profiles.person import person

NAME = PersonNameComparison()


def features(**attributes) -> dict[str, list[str]]:
    obs = SimpleNamespace(attributes=attributes)
    out: dict[str, list[str]] = {}
    for normalizer in person().normalizers:
        for f in normalizer.normalize(obs):
            out.setdefault(f.feature, []).append(f.value)
    return out


def edgar(name: str) -> dict[str, list[str]]:
    return features(name=name, name_order="family_first")


@pytest.mark.parametrize(
    ("raw", "given", "middle", "family", "suffix"),
    [
        ("SMITH JOHN A JR", "john", "a", "smith", "jr"),
        ("Smith John A.", "john", "a", "smith", ""),
        ("DAY II ROWLAND W", "rowland", "w", "day", "ii"),  # suffix after the family name
        ("Hilario Emanuel P.N.", "emanuel", "pn", "hilario", ""),
        ("von Emster Kurt", "kurt", "", "vonemster", ""),
        ("EMSTER KURT VON", "kurt", "", "vonemster", ""),  # particle written last
        ("Liu Hui", "hui", "", "liu", ""),
        ("Smith, John A", "john", "a", "smith", ""),  # a comma decides, whatever the order
    ],
)
def test_family_first_names_parse(raw, given, middle, family, suffix):
    parsed = parse_name(raw, "family_first")
    assert {k: parsed[k] for k in ("given", "middle", "family", "suffix")} == {
        "given": given, "middle": middle, "family": family, "suffix": suffix}  # fmt: skip


def test_three_words_have_a_compound_reading():
    assert parse_name("Pineyro Sublett Carla", "family_first")["alt"] == ("carla", "sublett")
    # A middle initial is not a given name.
    assert "alt" not in parse_name("Smith John A", "family_first")
    assert "alt" not in parse_name("Smith John", "family_first")
    # Given first, nothing changes.
    assert "alt" not in parse_name("Carla Pineyro Sublett")
    assert parse_name("Cher", "family_first") is None


def test_name_order_is_read_from_the_record():
    assert edgar("SMITH JOHN A")["pname.key"] == ["john smith"]
    assert features(name="SMITH JOHN A")["pname.key"] == ["smith a"]  # as before
    assert features(name="SMITH JOHN A", name_order="sideways")["pname.key"] == ["smith a"]
    f = edgar("Pineyro Sublett Carla")
    assert f["pname.alt"] == ["carla sublett"]
    assert f["pname.reading"] == ["carla sublett", "pineyro sublett", "sublett pineyro"]
    assert features(name="Carla Pineyro Sublett")["pname.reading"] == ["carla sublett"]


@pytest.mark.parametrize(
    ("a", "b", "level"),
    [
        ("SMITH JOHN A", "John A. Smith", "exact"),
        ("SMITH JOHN A", "John B. Smith", "middle_conflict"),
        ("SMITH JOHN A JR", "John A. Smith Jr.", "exact"),
        ("MURPHY ROBERT J", "Bob Murphy", "alias"),
        ("Pineyro Sublett Carla", "Carla Pineyro Sublett", "fuzzy"),  # the compound reading
        ("Vojvodich Radakovich Lynn M", "Lynn Radakovich", "fuzzy"),
        ("Smith John Albert", "Mary Smith", "family_only"),  # no reading makes it agree
    ],
)
def test_family_first_against_given_first(a, b, level):
    assert NAME.levels[NAME.compare(edgar(a), features(name=b))] == level
    assert NAME.levels[NAME.compare(features(name=b), edgar(a))] == level


def test_compound_readings_find_each_other():
    """EDGAR's own two orders for one person: 'Losada Moreno Angel', 'Moreno Angel Losada'."""
    assert NAME.levels[
        NAME.compare(edgar("LOSADA MORENO ANGEL"), edgar("Moreno Angel Losada"))
    ] == ("fuzzy")


def test_family_first_names_also_block_swapped():
    """EDGAR is not always consistent: 'Hui Liu' and 'Liu Hui' under one CIK."""
    assert edgar("Hui Liu")["pname.reading"] == ["hui liu", "liu hui"]
    assert features(name="Hui Liu")["pname.reading"] == ["hui liu"]


def test_given_name_and_employer_block_without_a_birth_date():
    f = features(name="Julie Huber", employer="Stifel Financial Corp")
    g = edgar("SMITH JULIE A")
    assert "pname.given_employer" not in g
    assert (
        f["pname.given_employer"]
        == features(name="Julie Smith", employer="STIFEL FINANCIAL CORP")["pname.given_employer"]
    )


@pytest.mark.parametrize(
    ("a", "b", "level"),
    [
        # A suffix on one side only: a dropped "Jr", or a father and son.
        ("Robert F Shuford Jr", "Robert F Shuford", "suffix_one_side"),
        ("Bob Shuford Jr", "Robert Shuford", "suffix_one_side"),
        ("John Smith Jr.", "John Smith Sr.", "middle_conflict"),  # both: a conflict
        # A near match with a different middle initial is a conflict, as an exact one is.
        ("Erik J Anderson", "Eric R. Anderson", "middle_conflict"),
        ("William H Waldrop", "William T Walters", "middle_conflict"),
        ("Erik Anderson", "Eric R. Anderson", "fuzzy"),
        # Soundex alone is not a near match.
        ("John Buchanan", "Jami Beckmann", "else"),
        ("Sean Murphy", "Shawn Murphy", "fuzzy"),
    ],
)
def test_suffixes_middles_and_soundex(a, b, level):
    assert NAME.levels[NAME.compare(features(name=a), features(name=b))] == level


def test_an_initial_or_a_one_sided_suffix_is_never_a_match_alone():
    caps = person().weight_caps["name"]
    assert caps["initial"] <= 8.0 and caps["suffix_one_side"] <= 4.0
