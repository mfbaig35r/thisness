"""The messy names-only benchmark (design 08 §8)."""

import re

from thisness.benchmark import uk
from thisness.benchmark.organizations import PROCESSORS, generate, messy, names_only
from thisness.cli import main


def test_messy_is_deterministic_and_keeps_truth():
    clean = names_only(generate(80, seed=3))
    a, b = messy(clean), messy(clean)
    assert [r.attributes for r in a.records] == [r.attributes for r in b.records]
    assert [r.record_id for r in a.records] == [r.record_id for r in clean.records]
    assert a.truth == clean.truth
    assert a.config["variant"] == "names_only+messy"


def test_a_share_of_names_is_messed_in_every_way():
    clean = names_only(generate(300, seed=3))
    dirty = messy(clean)
    changed = [(c.attributes["name"], d.attributes["name"])
               for c, d in zip(clean.records, dirty.records, strict=True)
               if c.attributes["name"] != d.attributes["name"]]  # fmt: skip
    assert 0.28 < len(changed) / len(clean.records) < 0.42
    names = [d for _, d in changed]
    assert any(n.startswith(PROCESSORS) for n in names)
    assert any(len(n) == 22 and n.startswith(PROCESSORS) for n in names)  # truncated
    assert any(re.search(r"DO NOT USE|\(OLD\)|INACTIVE|DUPLICATE OF|^ZZ |^XX-", n) for n in names)
    assert any(re.search(r" (STORE|LOC|UNIT) \d+$| - BLDG \d+$", n) for n in names)
    assert any(re.search(r"\(VENDOR \d+\)$| REF \d+$| A/C \d+$", n) for n in names)
    assert any(re.search(r" (DBA|dba|T/A) ", n) for n in names)


def test_messy_uk_names():
    dirty = messy(names_only(uk.from_fixture(30)))
    assert dirty.config["variant"] == "names_only+messy"
    assert all(set(r.attributes) == {"name"} for r in dirty.records)


def test_eval_messy_needs_names_only(capsys):
    assert main(["eval", "--messy", "--size", "20"]) == 1
    assert main(["eval", "--names-only", "--messy", "--size", "20", "--seeds", "3"]) == 0
