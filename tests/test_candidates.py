from __future__ import annotations

import pytest

from thisness.candidates import (
    CrossFeature,
    ExactValue,
    Overlap,
    RareValue,
    distinct_pairs,
    generate_all,
)
from thisness.core import Observation, ResolutionRun, RunStatus
from thisness.profiles.organization import organization

NAME = frozenset({"name"})


class Harness:
    def __init__(self, store, ids, now):
        self.store, self.ids, self.now = store, ids, now
        self.profile = organization()
        self.store.register_component(self.profile.ref, {})
        self.names: dict[str, str] = {}

    def ingest(self, *names: str, entity_type: str = "organization", **attrs) -> list[str]:
        observations = [
            Observation.new(
                ids=self.ids,
                entity_type=entity_type,
                attributes={"name": n, **attrs},
                source={"type": "test", "id": "t", "locator": {"i": len(self.names) + i}},
                ingested_at=self.now,
            )
            for i, n in enumerate(names)
        ]
        self.store.ingest(observations)
        self.store.write_features(self.profile.features(observations))
        for o in observations:
            self.names[o.observation_id] = o.attributes["name"]
        return [o.observation_id for o in observations]

    def run(self, scope: list[str], entity_type: str = "organization") -> str:
        run = ResolutionRun(
            run_id=self.ids.run(),
            entity_type=entity_type,
            profile_ref=self.profile.ref.ref,
            base_seq=self.store.head_seq(),
            status=RunStatus.EXECUTING,
            plan={},
            created_at=self.now,
        )
        self.store.record_run(run, scope)
        return run.run_id

    def named_pairs(self, table) -> set[frozenset[str]]:
        return {
            frozenset((self.names[a], self.names[b]))
            for a, b in zip(table.column("left_id").to_pylist(),
                            table.column("right_id").to_pylist(), strict=True)
        }  # fmt: skip


@pytest.fixture
def h(store, ids, now) -> Harness:
    return Harness(store, ids, now)


def test_exact_value_pairs_within_cap(h):
    ids = h.ingest("IBM", "IBM Corp.", "I.B.M.", "Acme")
    run = h.run(ids)
    gen = ExactValue("name_key", "name.key", NAME, max_block=3)
    assert h.named_pairs(gen.generate(h.store, run)) == {
        frozenset(p) for p in [("IBM", "IBM Corp."), ("IBM", "I.B.M."), ("IBM Corp.", "I.B.M.")]
    }
    small = ExactValue("name_key", "name.key", NAME, max_block=2)
    assert small.generate(h.store, run).num_rows == 0
    assert h.store.oversize_blocks(run, "name.key", 2) == [("ibm", 3)]


def test_incremental_scope_pairs_new_observations_with_stored_ones(h):
    old = h.ingest("IBM", "IBM Corp.", "Acme Corp")
    new = h.ingest("I.B.M.")
    run = h.run(new)  # only the new observation is in scope
    pairs = h.named_pairs(ExactValue("k", "name.key", NAME).generate(h.store, run))
    assert pairs == {frozenset(("I.B.M.", "IBM")), frozenset(("I.B.M.", "IBM Corp."))}
    assert old  # stored observations are candidates, but not paired with each other


def test_other_entity_types_are_never_candidates(h):
    org = h.ingest("Jordan Smith")
    person = h.ingest("Jordan Smith", entity_type="person")
    run = h.run(org + person)
    assert ExactValue("k", "name.key", NAME).generate(h.store, run).num_rows == 0


def test_rare_value_ignores_common_tokens(h):
    ids = h.ingest(
        "Zorbex Logistics", "Zorbex Freight", "Atlas Logistics", "Pioneer Logistics",
        "Summit Logistics",
    )  # fmt: skip
    run = h.run(ids)
    # "logistics" (on 4 names) is above max_df, so it never blocks. "freight", "atlas",
    # "pioneer" and "summit" are on one name each, so they are not ranked: they can pair
    # with nothing. That leaves "zorbex" as the one shared rare token.
    gen = RareValue("rare_token", "name.tokens", NAME, per_observation=1, max_df=3)
    assert h.named_pairs(gen.generate(h.store, run)) == {
        frozenset(("Zorbex Logistics", "Zorbex Freight"))
    }
    # With a looser cap, "logistics" becomes the rarest shared token of the other three.
    loose = RareValue("rare_token", "name.tokens", NAME, per_observation=1, max_df=60)
    assert frozenset(("Atlas Logistics", "Summit Logistics")) in h.named_pairs(
        loose.generate(h.store, run)
    )


def test_cross_feature_finds_acronyms(h):
    ids = h.ingest("International Business Machines", "IBM", "Acme")
    run = h.run(ids)
    gen = CrossFeature("acronym", "name.acronym", "name.key", NAME)
    assert h.named_pairs(gen.generate(h.store, run)) == {
        frozenset(("International Business Machines", "IBM"))
    }


def test_overlap_catches_typos_exact_blocking_misses(h):
    ids = h.ingest("Granite Logistics", "Granitte Logistics", "Harbor Supply")
    run = h.run(ids)
    assert ExactValue("k", "name.key", NAME).generate(h.store, run).num_rows == 0
    gen = Overlap("trigram", "name.trigram", NAME, min_share=0.6)
    table = gen.generate(h.store, run)
    assert h.named_pairs(table) == {frozenset(("Granite Logistics", "Granitte Logistics"))}
    assert 0.6 <= table.column("score_value")[0].as_py() <= 1.0


def test_identifier_and_domain_blocking(h):
    a, b = h.ingest("Acme Holdings", "ACME HLDGS", registration_ids=["EIN:12-3456789"])
    c, d = h.ingest("Acme Logistics", "Acme Packaging", website="https://acme.com")
    run = h.run([a, b, c, d])
    by_id = ExactValue("identifier", "reg_id", frozenset({"registration_ids"}))
    by_domain = ExactValue("domain", "domain", frozenset({"domain"}))
    assert distinct_pairs(by_id.generate(h.store, run)) == [tuple(sorted((a, b)))]
    assert distinct_pairs(by_domain.generate(h.store, run)) == [tuple(sorted((c, d)))]


def test_profile_generators_union_and_dedupe(h):
    ids = h.ingest(
        "International Business Machines Corp", "IBM", "IBM Corp.", "Intl Business Machines",
        "Acme Logistics", "Acme Logistcs",
    )  # fmt: skip
    run = h.run(ids)
    candidates = generate_all(h.store, h.profile.candidate_generators, run)
    pairs = distinct_pairs(candidates)
    assert len(pairs) == len(set(pairs))
    named = {frozenset((h.names[a], h.names[b])) for a, b in pairs}
    assert frozenset(("IBM", "IBM Corp.")) in named  # name key
    assert frozenset(("International Business Machines Corp", "IBM")) in named  # acronym
    assert frozenset(("Acme Logistics", "Acme Logistcs")) in named  # trigram
    assert frozenset(("International Business Machines Corp", "Intl Business Machines")) in named
    refs = {r.split("#")[0] for r in candidates.column("generator_ref").to_pylist()}
    assert {"candidate_generator:name_key@1", "candidate_generator:acronym@1",
            "candidate_generator:trigram@1"} <= refs  # fmt: skip


def test_candidates_record_into_the_store(h, ids, now):
    from thisness.core import StageExecution

    obs = h.ingest("IBM", "IBM Corp.")
    run = h.run(obs)
    stage = StageExecution(stage_id=ids.stage(), run_id=run, kind="candidates",
                           producer_ref=h.profile.ref.ref, family="blocking", view="raw",
                           started_at=now)  # fmt: skip
    h.store.record_stage(stage)
    table = generate_all(h.store, h.profile.candidate_generators, run)
    import pyarrow as pa

    n = table.num_rows
    h.store.record_candidates(
        table.append_column("run_id", pa.array([run] * n)).append_column(
            "stage_id", pa.array([stage.stage_id] * n)
        )
    )
    stored = h.store._con.execute("SELECT count(*) FROM candidates").fetchone()[0]
    assert stored == n > 0
