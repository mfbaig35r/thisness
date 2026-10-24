"""Accepting suggestions in bulk (design 10 §4.2)."""

from __future__ import annotations

import json

import pytest

from tests.test_assistant import setup
from thisness.benchmark import organizations
from thisness.cli import main
from thisness.core.decisions import Actor, ReviewDecision
from thisness.reviews import BULK_REASON
from thisness.storage.duckdb.invariants import check

PERSON = Actor(kind="human", id="dana")


@pytest.fixture
def engine():
    engine = setup(organizations.generate(300, seed=11))
    engine.recommend_open(limit=1000)
    yield engine
    assert check(engine.store) == []


def kinds(engine):
    """The latest suggestion's kind for every open review."""
    out = {}
    for s in engine.reviews():
        recs = engine.recommendations(s.review.review_id)
        out[s.review.review_id] = recs[-1].resolution if recs else None
    return out


def test_a_dry_run_counts_and_changes_nothing(engine):
    before = kinds(engine)
    result = engine.accept_recommendations(actor=PERSON, dry_run=True)
    assert result.dry_run and sum(result.accepted.values()) > 0
    assert set(result.accepted) <= {"related", "different"}  # the profile's kinds
    assert kinds(engine) == before


def test_accepted_suggestions_are_a_persons_answers(engine):
    before = kinds(engine)
    allowed = {"related", "different"}
    result = engine.accept_recommendations(actor=PERSON)
    accepted = sum(result.accepted.values())
    assert accepted > 0
    left = kinds(engine)
    assert len(left) == len(before) - accepted
    # What the profile does not accept in bulk is left for a person.
    assert all(k not in allowed for k in left.values()) or result.skipped
    answers = [d for d in engine.store.decisions_of_type("review") if isinstance(d, ReviewDecision)]
    assert len(answers) == accepted
    assert all(d.actor == PERSON and d.reason == BULK_REASON and d.recommendation_id
               for d in answers)  # fmt: skip
    # Agreement statistics keep them apart: they agree by construction.
    measured = engine.assistant_agreement()
    assert measured["accepted_in_bulk"] == accepted and measured["reviews"] == 0


def test_named_kinds_override_the_profile(engine):
    result = engine.accept_recommendations(actor=PERSON, resolutions=["same"], dry_run=True)
    assert set(result.accepted) <= {"same"}


def test_a_review_changed_since_its_suggestion_is_skipped(engine):
    """A merge touching a review's entity after its suggestion makes the suggestion stale."""
    from datetime import timedelta

    target = next(s.review for s in engine.reviews()
                  if engine.recommendations(s.review.review_id)[-1].resolution != "unsure")  # fmt: skip
    member = target.members[0]
    entity = engine.store.entities_of([member])[member]
    other = next(e.entity_id for e in engine.entities("organization", limit=500).rows
                 if e.entity_id != entity)  # fmt: skip
    later = engine.store._clock() + timedelta(minutes=1)
    engine.store._clock = lambda: later  # the merge happens after the suggestion
    engine.merge_entities(entity, [other], reason="test")
    result = engine.accept_recommendations(
        actor=PERSON, resolutions=["same", "different", "related", "groups"])  # fmt: skip
    assert result.skipped.get("changed since the suggestion", 0) >= 1
    assert target.review_id in kinds(engine)


def test_the_command_line_accepts_and_reports(tmp_path, capsys):
    store = str(tmp_path / "id.duckdb")
    assert main(["--store", store, "demo", "--orgs", "60"]) == 0
    capsys.readouterr()
    assert main(["--store", store, "reviews", "accept", "--dry-run", "--actor", "dana"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("would accept 0 suggestions") and "no suggestion" in out


def test_eval_reports_what_accepting_leaves(capsys):
    assert main(["eval", "--size", "120", "--seeds", "3", "--assistant", "oracle", "--accept",
                 "--json"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{") : out.index("\n}") + 2])
    assert report["reviews_per_1000"] > 0
    assisted = json.loads(out[out.index("  assistant: ") + len("  assistant: ") :])
    accepted = assisted["accepted"]
    assert accepted["reviews_left"] == report["reviews_opened"] - sum(accepted["accepted"].values())
    assert accepted["false_merge_clusters"] == 0  # the oracle is never wrong


def test_accept_needs_an_assistant(capsys):
    assert main(["eval", "--size", "120", "--accept"]) == 1
    assert "--assistant" in capsys.readouterr().err
