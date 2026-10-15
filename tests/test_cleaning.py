"""Supplier-name cleaning (design 08 §3), trade names (decision 3), record status and the
readable label (§5.1)."""

from types import SimpleNamespace

import pytest

from thisness import Thisness
from thisness.comparisons.organization import NameComparison
from thisness.name_only import collapse_key
from thisness.normalize.organization import clean_name
from thisness.profiles.organization import organization

PROFILE = organization()

# What an extract adds, removed.
CHANGES = {
    "Acme - 812773": "Acme",
    "ACME CORP #4411": "ACME CORP",
    "Acme Corp 812773": "Acme Corp",
    "Acme (Vendor 10233)": "Acme",
    "Acme REF 4411": "Acme",
    "NATAL Ltd. A/C 916655": "NATAL Ltd.",
    "Amazon, Inc. 4411": "Amazon, Inc.",
    "Harjas Foods PLC - 812773": "Harjas Foods PLC",
    "PAYPAL *ACME": "ACME",
    "SQ *JOES CAFE": "JOES CAFE",
    "TST* BRIDGEHAVEN EUROP": "BRIDGEHAVEN EUROP",  # cut at 22 characters: marked, kept
    "WALGREENS STORE 1022": "WALGREENS",
    "SHELL OIL 57442 BLDG 3": "SHELL OIL",
    "ACME CORP - DO NOT USE": "ACME CORP",
    "ZZ Acme Corp": "Acme Corp",
    "XX-Woodhol Svcs - 221803": "Woodhol Svcs",
    "*INACTIVE* Acme": "Acme",
    "Acme Inc (OLD)": "Acme Inc",
    "Acme Inc INACTIVE": "Acme Inc",
    "Woodhol Svcs - DUPLICATE OF 94858": "Woodhol Svcs",
    "Acme - USE VENDOR 4411": "Acme",
    "Woodhol, Services Ltd 334750 (OLD)": "Woodhol, Services Ltd",
    "John Swire & Sons (57Bg) Ltd - 905952 STORE 4990": "John Swire & Sons (57Bg) Ltd",
    "Intercede 2374 - 747109": "Intercede 2374",
    "Acme Holdings LLC dba Acme Tools": "Acme Holdings LLC",
}

# Names that are names, untouched.
KEEPS = ["3M", "7-Eleven", "Studio 54", "Omega Holdings II", "Landmark Mortgage Securities No. 3",
         "Mayfield 1966 Investments", "Intercede 2296 Ltd", "Old Mutual", "Store 54 Records",
         "The Inactive Club", "Unit 4 Ltd", "Route 66 Diner", "Area 51", "Building 3", "Smith Dup",
         "Fund 2 LP", "Zodiac Maritime"]  # fmt: skip


@pytest.mark.parametrize(("raw", "cleaned"), sorted(CHANGES.items()))
def test_what_extracts_add_is_removed(raw, cleaned):
    assert clean_name(raw).text == cleaned


@pytest.mark.parametrize("raw", KEEPS)
def test_names_are_never_cut(raw):
    assert clean_name(raw) == (raw, None, None, False, False)


def test_notes_become_the_record_status():
    statuses = {raw: clean_name(raw).status for raw in CHANGES}
    assert statuses["ACME CORP - DO NOT USE"] == "do_not_use"
    assert statuses["ZZ Acme Corp"] == statuses["*INACTIVE* Acme"] == "inactive"
    assert statuses["Acme Inc (OLD)"] == "old"
    assert statuses["Woodhol Svcs - DUPLICATE OF 94858"] == "duplicate"
    assert statuses["Acme - USE VENDOR 4411"] == "use_other"
    assert statuses["Acme - 812773"] is None
    # A name that is only a note keeps itself as its name.
    assert clean_name("DO NOT USE") == ("DO NOT USE", None, "do_not_use", False, False)


def test_trade_names_and_descriptors():
    assert clean_name("BRIP 6 (POOLE) LLP - 374822 DBA BRIP 6").trade_name == "BRIP 6"
    assert clean_name("Argyll Energy Ltd T/A ARGYLL ENERGY").trade_name == "ARGYLL ENERGY"
    short = clean_name("SQ *JOES CAFE")
    assert short.descriptor and not short.truncated


def _view(name: str) -> dict[str, list[str]]:
    view: dict[str, list[str]] = {}
    for normalizer in PROFILE.normalizers:
        for f in normalizer.normalize(SimpleNamespace(attributes={"name": name})):
            view.setdefault(f.feature, []).append(f.value)
    return view


def test_features_come_from_the_cleaned_name():
    assert _view("SQ *ACME CORP - 812773")["name.key"] == _view("Acme Corp")["name.key"]
    assert _view("Acme Corp - DO NOT USE")["record_status"] == ["do_not_use"]
    assert "record_status" not in _view("Acme Corp")
    assert _view("Acme Holdings LLC dba Acme Tools")["name.alias_key"] == ["acmetools"]


def test_a_trade_name_is_a_capped_relationship_level():
    compare = NameComparison()
    level = compare.levels.index("trade_name")
    assert (
        compare.compare(_view("Johnson Holdings LLC dba Sunrise Bakery"), _view("Sunrise Bakery"))
        == level
    )
    assert compare.compare(_view("BRIP 6 (Poole) LLP dba Brip Six"), _view("BRIP 6 (Nailsea) LLP dba Brip Six")) in (
        level, compare.levels.index("number_differs"), compare.levels.index("qualifier_differs"))  # fmt: skip
    assert PROFILE.weight_caps["name"]["trade_name"] == 0.0
    assert ("name", "trade_name") in PROFILE.relationship_levels
    assert compare.polarity["trade_name"] == "neutral"
    # The term-frequency levels keep their places (design 07): typo and token set.
    assert compare.levels[2] == "token_typo" and compare.levels[5] == "token_set_0.92"


def test_numbered_companies_stay_apart_before_a_model_sees_them():
    assert collapse_key(_view("Intercede 2296 Ltd")) != collapse_key(_view("Intercede 2302 Ltd"))
    assert collapse_key(_view("SQ *AMAZON INC")) == collapse_key(_view("Amazon Inc - 4411"))


def test_the_label_is_the_readable_spelling():
    engine = Thisness(":memory:", profiles=[PROFILE])
    names = ["HALEON UK FINANCE LIMITED", "HALEON UK FINANCE LIMITED", "HALEON UK FINANCE LIMITED",
             "Haleon UK Finance Limited", "PAYPAL *HALEON UK FINANCE LIMITED"]  # fmt: skip
    obs = [engine.observe("organization", {"name": n}, {"type": "t", "id": str(i)})
           for i, n in enumerate(names)]  # fmt: skip
    snapshot = PROFILE.canonicalizer.build(obs)
    assert snapshot["name"] == "Haleon UK Finance Limited"
    only_descriptors = [
        engine.observe("organization", {"name": "SQ *JOES CAFE"}, {"type": "t", "id": "x"})
    ]
    assert PROFILE.canonicalizer.build(only_descriptors)["name"] == "JOES CAFE"


def test_record_status_in_the_mapping(tmp_path):
    engine = Thisness(":memory:", profiles=[PROFILE])
    engine.ingest([
        engine.observe("organization", {"name": "Acme Corp - DO NOT USE"}, {"type": "t", "id": "1"}),
        engine.observe("organization", {"name": "Acme Corp"}, {"type": "t", "id": "2"}),
    ])  # fmt: skip
    rows = {r["source_id"]: r for r in engine.mapping("organization").to_pylist()}
    assert rows["1"]["record_status"] == "do_not_use"
    assert rows["2"]["record_status"] is None


def test_a_cut_descriptor_is_never_taken_for_a_whole_name():
    """Two companies can share a descriptor's 22 characters ("SP * ASSOCIATED BRITIS",
    "TST* ASSOCIATED BRITIS"): a cut name is never an exact or typo match, and never
    collapsed before a model sees it (design 08 §13.3)."""
    cut = clean_name("TST* BRIDGEHAVEN EUROP")
    assert cut.descriptor and cut.truncated
    compare = NameComparison()
    level = compare.levels.index("truncated_prefix")
    assert (
        compare.compare(_view("SP * ASSOCIATED BRITIS"), _view("TST* ASSOCIATED BRITIS")) == level
    )
    assert (
        compare.compare(_view("SQ *LOW PROFILE FASHIO"), _view("Low Profile Fashion Ltd")) == level
    )
    assert compare.compare(_view("SQ *LOW PROFILE FASHIO"), _view("LOW PROFILE LTD")) != 0
    assert compare.compare(_view("SQ *JOES CAFE"), _view("Joes Cafe")) == 0  # short: whole
    assert PROFILE.weight_caps["name"]["truncated_prefix"] == 0.0
    assert collapse_key(_view("SP * ASSOCIATED BRITIS")) is None
