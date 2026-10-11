"""The engine: one code path from observations to committed identity (design 01 §3).

``resolve`` is ``resolve_many`` with one observation. A run:

1. scopes the unresolved observations, fitting a model first if none exists;
2. generates candidates, pairing the scope with every stored observation of its type;
3. adds comparisons against representative members of each candidate entity, so a
   contradiction anywhere in an entity is seen;
4. scores pairs, classifies edges, folds them onto nodes (observations in scope and
   existing entities), and runs consensus;
5. turns outcomes into a change set under the resolution policy, records the
   evidence and decisions behind it, and commits.

On a bulk load the common-name adjustment is two-pass: a first consensus without it
supplies entity-level frequencies (design 03 §8.5).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any, Literal

import pyarrow as pa

from thisness.adjudicate import Adjudication, Adjudicator, PairCase
from thisness.candidates import ExactValue, distinct_pairs, generate_all
from thisness.canonicalize import ByProfile, Canonicalizer
from thisness.consensus import ConsensusResult, EdgeClass, ValidatedComponents
from thisness.core.decisions import (
    Actor,
    ConsensusDecision,
    ConsensusOutcome,
    OperatorAction,
    OperatorDecision,
    PairJudgment,
    SuggestedCut,
    Verdict,
)
from thisness.core.evidence import EvidenceItem, Score
from thisness.core.identity import Entity, EntitySnapshot
from thisness.core.ledger import LedgerEvent
from thisness.core.observation import Observation, SourceRef
from thisness.core.run import (
    Attach,
    ChangeSet,
    CreateEntity,
    Merge,
    OpenReview,
    Operation,
    Promote,
    ReadSet,
    Relate,
    ResolutionRun,
    RunStatus,
    Split,
    StageExecution,
)
from thisness.edges import VERDICT, FellegiSunterEdgePolicy, aggregate
from thisness.fellegi_sunter import FSModel
from thisness.ids import IdFactory
from thisness.policy import ResolutionPolicy
from thisness.profiles.base import AttributeDefinition, ResolutionProfile
from thisness.scoring import evidence_items, score_pairs
from thisness.starter import load_starter
from thisness.storage.duckdb import DuckDBStore
from thisness.storage.errors import CommitConflict, NotFound, ThisnessError
from thisness.storage.protocol import CommitResult
from thisness.training import (
    frequencies_from_clusters,
    load_model,
    register_model,
    term_frequencies,
    train,
)

Outcome = Literal["ATTACH_TO_EXISTING", "CREATE_ENTITY", "REQUIRES_REVIEW"]
_REVIEW = {ConsensusOutcome.REVIEW_CLUSTER, ConsensusOutcome.PROPOSE_MERGE}


@dataclass(frozen=True)
class ObservationOutcome:
    observation_id: str
    outcome: Outcome
    entity_id: str
    entity_state: str
    decision_id: str


@dataclass(frozen=True)
class RunResult:
    run_id: str | None
    committed: bool
    outcomes: dict[str, ObservationOutcome] = field(default_factory=dict)
    commit: CommitResult | None = None
    changes: ChangeSet | None = None
    reviews: tuple[str, ...] = ()
    relationship_candidates: tuple[tuple[str, str], ...] = ()
    stats: dict[str, Any] = field(default_factory=dict)


class Thisness:
    def __init__(
        self,
        store: DuckDBStore | str = ":memory:",
        *,
        profiles: Sequence[ResolutionProfile] = (),
        ids: IdFactory | None = None,
        clock: Callable[[], datetime] | None = None,
        edge_policy: FellegiSunterEdgePolicy | None = None,
        consensus: ValidatedComponents | None = None,
        policy: ResolutionPolicy | None = None,
        canonicalizer: Canonicalizer | None = None,
        representatives: int = 20,
        random_pairs: int = 300_000,
        min_observations_to_fit: int = 200,
        tf_min_entities: int = 200,
        adjudicator: Adjudicator | None = None,
        assistant: Any = None,
    ) -> None:
        self.ids = ids or IdFactory()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.store = (
            store
            if isinstance(store, DuckDBStore)
            else DuckDBStore(store, ids=self.ids, clock=self.clock)
        )
        self.edge_policy = edge_policy or FellegiSunterEdgePolicy()
        self.consensus = consensus or ValidatedComponents()
        self.policy = policy or ResolutionPolicy()
        self.profiles: dict[str, ResolutionProfile] = {}
        self.canonicalizer = canonicalizer or ByProfile(self.profiles)
        self.representatives = representatives
        self.random_pairs = random_pairs
        self.min_observations_to_fit = min_observations_to_fit
        self.tf_min_entities = tf_min_entities
        self.adjudicator = adjudicator  # design 05 §4: judges contested pairs
        self.assistant = assistant  # design 05 §5: recommends answers to reviews
        for component in (self.edge_policy, self.consensus, self.policy, self.canonicalizer):
            self.store.register_component(component.ref, {})
        for profile in profiles:
            self.register(profile)

    # -- profiles, ingest, models --------------------------------------------------------------

    def register(self, profile: ResolutionProfile) -> None:
        self.profiles[profile.entity_type] = profile
        self.store.register_component(profile.ref, {})
        for component in (
            *profile.normalizers,
            *profile.candidate_generators,
            *profile.comparisons,
            *profile.constraints,
            *profile.rules,
        ):
            self.store.register_component(component.ref, {})

    def _profile(self, entity_type: str) -> ResolutionProfile:
        try:
            return self.profiles[entity_type]
        except KeyError:
            raise NotFound(f"no profile registered for {entity_type!r}") from None

    def observe(
        self,
        entity_type: str,
        attributes: dict[str, Any],
        source: SourceRef | dict[str, Any],
        **kwargs: Any,
    ) -> Observation:
        return Observation.new(
            ids=self.ids,
            entity_type=entity_type,
            attributes=attributes,
            source=source,
            ingested_at=kwargs.pop("ingested_at", None) or self.clock(),
            **kwargs,
        )

    def ingest(self, observations: Sequence[Observation]) -> list[str]:
        """Store observations and their features. Returns the stored id of each, which is
        the earlier id when an observation was already ingested."""
        result = self.store.ingest(observations)
        inserted = set(result.inserted)
        fresh = [o for o in observations if o.observation_id in inserted]
        for entity_type in sorted({o.entity_type for o in fresh}):
            profile = self.profiles.get(entity_type)
            if profile is not None:
                self.store.write_features(
                    profile.features([o for o in fresh if o.entity_type == entity_type])
                )
        return [result.existing.get(o.observation_id, o.observation_id) for o in observations]

    def model(self, entity_type: str) -> FSModel | None:
        ref = self.store.get_meta(f"model.{entity_type}")
        return None if ref is None else load_model(self.store, ref)

    def model_source(self, entity_type: str) -> str | None:
        """``fitted`` or ``starter``: where the active model came from."""
        return self.store.get_meta(f"model.{entity_type}.source")

    def _active_model(self, entity_type: str) -> FSModel:
        """A fitted model if the type has one. Otherwise fit, once there are enough
        observations; until then, the profile's starter model (design 03 §21)."""
        current = self.model(entity_type)
        if current is not None and self.model_source(entity_type) == "fitted":
            return current
        if len(self.store.observation_ids(entity_type)) >= self.min_observations_to_fit:
            return self.fit(entity_type)
        if current is not None:
            return current
        starter = load_starter(self._profile(entity_type))
        if starter is None:
            return self.fit(entity_type)  # raises: too few observations, and no starter
        register_model(self.store, starter)
        self.store.set_meta(f"model.{entity_type}", starter.ref.ref)
        self.store.set_meta(f"model.{entity_type}.source", "starter")
        return starter

    def fit(self, entity_type: str, *, random_pairs: int | None = None, seed: int = 0) -> FSModel:
        """Train on every observation of the type and make the model active.

        Refuses below ``min_observations_to_fit``: with a handful of records, random
        pairs are mostly matches and the learned weights mean nothing."""
        profile = self._profile(entity_type)
        scope = self.store.observation_ids(entity_type)
        if len(scope) < self.min_observations_to_fit:
            raise ThisnessError(
                f"{len(scope)} {entity_type} observations is too few to fit a model "
                f"(need {self.min_observations_to_fit}); ingest more before resolving"
            )
        run = self._new_run(profile, scope, purpose="fit")
        model = train(self.store, profile, run.run_id, seed=seed,
                      random_pairs=random_pairs or self.random_pairs,
                      prior=load_starter(profile))  # fmt: skip
        register_model(self.store, model)
        self.store.set_meta(f"model.{entity_type}", model.ref.ref)
        self.store.set_meta(f"model.{entity_type}.source", "fitted")
        self.store.close_run(run.run_id, "abandoned")  # training never commits identity
        return model

    def _new_run(self, profile: ResolutionProfile, scope: Sequence[str], **plan: Any):
        run = ResolutionRun(
            run_id=self.ids.run(),
            entity_type=profile.entity_type,
            profile_ref=profile.ref.ref,
            base_seq=self.store.head_seq(),
            status=RunStatus.EXECUTING,
            plan=plan,
            created_at=self.clock(),
        )
        self.store.record_run(run, scope)
        return run

    # -- resolution -----------------------------------------------------------------------------

    def resolve(
        self, entity_type: str, attributes: dict[str, Any], source: SourceRef | dict[str, Any],
        **kwargs: Any,
    ) -> ObservationOutcome:  # fmt: skip
        observation = self.observe(entity_type, attributes, source, **kwargs)
        stored = self.ingest([observation])[0]
        result = self.resolve_many(entity_type, [stored])
        if stored in result.outcomes:
            return result.outcomes[stored]
        entity = self.store.entities_of([stored])[stored]  # already resolved earlier
        return ObservationOutcome(stored, "ATTACH_TO_EXISTING", entity,
                                  self.store.entity(entity).state, "")  # fmt: skip

    def resolve_many(
        self,
        entity_type: str,
        observations: Sequence[Observation | str] | None = None,
        *,
        commit: bool = True,
        actor: Actor | None = None,
        adjudicator: Adjudicator | None = None,
        plan: Any = None,
    ) -> RunResult:
        """Resolve a batch as one run. ``observations`` may be new observations or stored
        ids; by default, every unresolved observation of the type. ``adjudicator``
        overrides the engine's for this run (design 05 §4). ``plan`` runs a different
        plan instead of Fellegi-Sunter: a ``NameOnlyPlan`` groups names with a model
        (design 05 §6)."""
        profile = self._profile(entity_type)
        if observations is None:
            scope = self.store.unresolved(entity_type)
        else:
            new = [o for o in observations if isinstance(o, Observation)]
            stored = dict(zip([o.observation_id for o in new], self.ingest(new), strict=True))
            wanted = [
                stored[o.observation_id] if isinstance(o, Observation) else o for o in observations
            ]
            resolved = self.store.entities_of(wanted)
            scope = sorted({o for o in wanted if o not in resolved})
        if not scope:
            return RunResult(run_id=None, committed=False)
        actor = actor or Actor(kind="resolver", id=f"thisness:{entity_type}")
        if plan is not None:
            from thisness.name_only import resolve as resolve_name_only

            try:
                return resolve_name_only(self, profile, scope, plan, commit=commit, actor=actor)
            except CommitConflict:
                return resolve_name_only(self, profile, scope, plan, commit=commit, actor=actor)
        model = self._active_model(entity_type)
        adjudicator = adjudicator or self.adjudicator
        if adjudicator is not None:
            adjudicator = adjudicator.for_profile(profile)
        try:
            return self._run(profile, model, scope, commit=commit, actor=actor,
                             adjudicator=adjudicator)  # fmt: skip
        except CommitConflict:
            # Something this run read changed after its base: rerun from current state.
            return self._run(profile, model, scope, commit=commit, actor=actor,
                             adjudicator=adjudicator)  # fmt: skip

    def _run(
        self,
        profile: ResolutionProfile,
        model: FSModel,
        scope: list[str],
        *,
        commit: bool,
        actor: Actor,
        adjudicator: Adjudicator | None = None,
    ) -> RunResult:
        store, now = self.store, self.clock()
        run = self._new_run(profile, scope, purpose="resolve", model=model.ref.ref)
        try:
            return self._execute(profile, model, run, scope, commit=commit, actor=actor, now=now,
                                 adjudicator=adjudicator)  # fmt: skip
        except CommitConflict:
            store.close_run(run.run_id, "abandoned")
            raise

    def _execute(self, profile, model, run, scope, *, commit, actor, now,
                 adjudicator=None) -> RunResult:  # fmt: skip
        store = self.store
        scope_set = set(scope)
        candidate_stage = self._stage(run.run_id, "candidates", profile.ref.ref, "blocking", now)
        candidates = generate_all(store, profile.candidate_generators, run.run_id)
        pairs = distinct_pairs(candidates)

        # Partners outside the scope count only if already resolved: an unresolved one
        # will meet this run's entities in its own run.
        partners = sorted({x for p in pairs for x in p} - scope_set)
        entity_of = store.entities_of(partners)
        pairs = [p for p in pairs if all(x in scope_set or x in entity_of for x in p)]
        entity_nodes = sorted(set(entity_of.values()))
        members = store.members_of(entity_nodes)

        # Representative members of each candidate entity, so its contradictions count.
        touching: dict[str, set[str]] = {}
        for a, b in pairs:
            for obs, other in ((a, b), (b, a)):
                if obs in scope_set and other in entity_of:
                    touching.setdefault(entity_of[other], set()).add(obs)
        recent = {e: list(reversed(members[e]))[: 3 * self.representatives] for e in entity_nodes}
        member_features = store.features(sorted({m for ms in recent.values() for m in ms}))
        extra = set()
        for entity, obs_set in touching.items():
            reps = _representatives(recent[entity], member_features, self.representatives)
            for rep in reps:
                entity_of[rep] = entity
                for obs in obs_set:
                    extra.add((obs, rep) if obs < rep else (rep, obs))
        all_pairs = sorted(set(pairs) | extra)
        features = store.features(sorted(scope_set | set(entity_of)))

        states = store.entity_states(entity_nodes)
        held = self._held_nodes()
        # Reviewers' verdicts between candidate entities override scores (design 04 §2.5).
        constrained: dict[tuple[str, str], EdgeClass] = {}
        for constraint, left, right in store.constraints_among(entity_nodes):
            if left == right:
                continue
            pair = (left, right) if left < right else (right, left)
            if constraint.kind == "cannot_link" or constrained.get(pair) != "cannot_link":
                constrained[pair] = "cannot_link" if constraint.kind == "cannot_link" else "strong"
        t_high = self.edge_policy.threshold(model, profile.min_strong_weight)
        # The first pass of a two-pass load only counts how many entities share each
        # name. It uses the automatic threshold without the profile's floor: the floor
        # guards commits, and applied here it fragments heavily repeated suppliers into
        # dozens of clusters, which then makes their names look common (design 03 §21).
        t_count = self.edge_policy.threshold(model)

        def node_of(x: str) -> str | None:
            return x if x in scope_set else entity_of.get(x)

        adjudications: list[Adjudication] = []
        adjudication_stage = None

        def consensus_pass(tables, t_high=t_high, adjudicate=False):
            nonlocal adjudication_stage
            scored = score_pairs(profile, model, features, all_pairs, tables=tables)
            rows = scored.to_pylist()
            classes = [
                (
                    r["left_id"],
                    r["right_id"],
                    self.edge_policy.classify(r, t_high, profile.max_negative_weight),
                )
                for r in rows
            ]
            folded = aggregate(classes, node_of)
            edges: dict[tuple[str, str], EdgeClass] = {}
            for pair, (cls, _) in folded.items():
                # Nodes under an open review are on hold: no automatic merges into them.
                if cls == "strong" and (pair[0] in held or pair[1] in held):
                    cls = "contested"
                edges[pair] = cls
            edges.update(constrained)
            if adjudicate and adjudicator is not None:
                # Contested pairs only, and none a person is already deciding: pairs under
                # review or settled by a reviewer's constraint are left to them.
                contested = [p for p, c in edges.items() if c == "contested"
                             and p not in constrained and p[0] not in held and p[1] not in held]  # fmt: skip
                adjudication_stage = self._stage(run.run_id, "pair_resolver",
                                                 adjudicator.ref.ref, adjudicator.model, now)  # fmt: skip
                self.store.register_component(adjudicator.ref, {"model": adjudicator.model,
                                                                "mode": adjudicator.mode})  # fmt: skip
                cases, definitions = self._pair_cases(profile, model, contested, folded, rows,
                                                      members)  # fmt: skip
                adjudications[:] = adjudicator.adjudicate(
                    cases, entity_type=profile.entity_type, attributes=definitions,
                    relations=profile.relations, run_id=run.run_id,
                    stage_id=adjudication_stage)  # fmt: skip
                model_same, model_apart = set(), set()
                for i, adjudication in enumerate(adjudications):
                    cls = adjudicator.edge(adjudication)
                    if cls is not None:
                        edges[adjudication.case.nodes] = cls
                        adjudications[i] = replace(adjudication, applied=True)
                        (model_same if cls == "strong" else model_apart).add(
                            adjudication.case.nodes
                        )
                if adjudicator.same_yields and model_same:
                    yielded = yield_to_conflicts(edges, model_same, model_apart)
                    for i, adjudication in enumerate(adjudications):
                        if adjudication.case.nodes in yielded:
                            adjudications[i] = replace(adjudication, applied=False)
            # How strong each contested pair is, to order them when a review must be cut.
            strength = {pair: max(rows[i]["weight"] for i in idx)
                        for pair, (_, idx) in folded.items() if idx}  # fmt: skip
            result = self.consensus.run([*scope, *entity_nodes], edges, states,
                                        strength=strength)  # fmt: skip
            return scored, rows, classes, folded, result

        # Common-value adjustments (design 03 §8.5): one per profile entry, each over its
        # own feature (a company's name, and its address: design 07 §13.2).
        modes: list[str] = []
        tables: dict[str, tuple[dict[str, float], float]] = {}
        first_clusters = None
        total = len(store.observation_ids(profile.entity_type))
        for tf in profile.term_frequencies:
            if tf.count == "observation":
                mode = "observations"
                frequencies, floor = term_frequencies(
                    store, tf.feature, resolved_only=False, by="observation"
                )
            elif len(scope) > 0.5 * total:
                mode = "two-pass"
                if first_clusters is None:  # one first pass serves every adjustment
                    _, _, _, _, first = consensus_pass({}, t_count)
                    first_clusters = [[m for n in c.members for m in (members.get(n) or [n])]
                                      for c in first.clusters]  # fmt: skip
                frequencies, floor = frequencies_from_clusters(first_clusters, features, tf.feature)
            else:
                mode = "store"
                frequencies, floor = term_frequencies(store, tf.feature)
            # A frequency estimated over a handful of entities is noise: with four
            # companies, every name looks like a quarter of the market.
            if 1 / floor < self.tf_min_entities:
                mode = f"off (under {self.tf_min_entities} entities)"
            else:
                tables[tf.feature] = (frequencies, floor)
            modes.append(mode)
        # One adjustment reports its mode as before; several report each by comparison.
        tf_mode = ("off" if not modes else modes[0] if len(modes) == 1 else
                   ", ".join(f"{t.comparison}:{t.feature} {m}"
                             for t, m in zip(profile.term_frequencies, modes, strict=True)))  # fmt: skip
        scored, rows, classes, folded, result = consensus_pass(tables, adjudicate=True)

        # Record the evidence and pair judgments behind every edge that matters.
        resolver_stage = self._stage(run.run_id, "pair_resolver", model.ref.ref,
                                     "fellegi_sunter", now)  # fmt: skip
        cluster_of = result.cluster_of()
        keep = []
        for i, (left, right, cls) in enumerate(classes):
            a, b = node_of(left), node_of(right)
            same = a is not None and b is not None and cluster_of.get(a) == cluster_of.get(b)
            # Relationship signals are kept too: candidates are derived from them later.
            if cls != "negative" or same or rows[i]["relationship_signal"]:
                keep.append(i)
        kept = scored.take(pa.array(keep, pa.int64()))
        evidence = evidence_items(profile, model, kept, features, new_id=self.ids.evidence,
                                  created_at=now, run_id=run.run_id,
                                  stage_id=resolver_stage)  # fmt: skip
        by_pair: dict[tuple[str, str], list[EvidenceItem]] = {}
        for item in evidence:
            by_pair.setdefault((item.left_id, item.right_id), []).append(item)
        refs = (model.ref.ref, self.edge_policy.ref.ref, profile.ref.ref)
        judgment_of: dict[int, str] = {}
        judgments = []
        for i in keep:
            r = rows[i]
            scores = [
                Score(kind="fellegi_sunter_weight", value=r["fs_weight"], model_ref=model.ref.ref),
                Score(kind="tf_adjustment", value=r["tf_adjustment"]),
                Score(kind="fs_posterior", value=r["posterior"], model_ref=model.ref.ref),
            ]
            if r["rule"] is not None:
                scores.append(Score(kind="rule", value=r["rule"]))
            judgment = PairJudgment(
                decision_id=self.ids.decision(),
                actor=Actor(kind="resolver", id=model.ref.name),
                run_id=run.run_id,
                stage_id=resolver_stage,
                left_id=r["left_id"],
                right_id=r["right_id"],
                verdict=VERDICT[classes[i][2]],
                scores=tuple(scores),
                evidence_ids=tuple(
                    e.evidence_id for e in by_pair.get((r["left_id"], r["right_id"]), ())
                ),
                component_refs=refs,
                created_at=now,
            )
            judgment_of[i] = judgment.decision_id
            judgments.append(judgment)

        llm_judgments, llm_evidence, llm_strong = self._adjudication_records(
            adjudicator, adjudications, by_pair, run.run_id, adjudication_stage, now)  # fmt: skip
        evidence += llm_evidence
        judgments += llm_judgments
        ops, outcomes, decisions, reviews = self._plan(
            profile,
            model,
            run,
            scope_set,
            result,
            folded,
            judgment_of,
            members,
            states,
            now,
            llm_strong,
        )
        relationship_candidates = tuple(
            (r["left_id"], r["right_id"]) for r in rows if r["relationship_signal"]
        )
        store.record_evidence(evidence)
        store.record_decisions([*judgments, *decisions])
        changes = ChangeSet(
            run_id=run.run_id,
            base_seq=run.base_seq,
            ops=tuple(ops),
            read_set=ReadSet(
                entity_ids=frozenset(entity_nodes),
                blocking_keys=self._blocking_keys(profile, features, scope),
            ),
        )
        stats = {
            "scope": len(scope),
            "candidate_pairs": len(pairs),
            "representative_pairs": len(extra),
            "scored_pairs": len(rows),
            "recorded_judgments": len(judgments),
            "evidence": len(evidence),
            "clusters": len(result.clusters),
            "t_high": t_high,
            "term_frequency": tf_mode,
            "boundary_pairs_unasked": result.unasked_pairs,
            "candidate_stage": candidate_stage,
        }
        if adjudicator is not None:
            stats["adjudication"] = _adjudication_stats(adjudicator, adjudications)
        if not commit:
            store.close_run(run.run_id, "proposed")
            return RunResult(run.run_id, False, outcomes, None, changes, reviews,
                             relationship_candidates, stats)  # fmt: skip
        committed = store.commit(changes, actor=actor, canonicalizer=self.canonicalizer)
        return RunResult(run.run_id, True, outcomes, committed, changes, reviews,
                         relationship_candidates, stats)  # fmt: skip

    def _plan(self, profile, model, run, scope_set, result: ConsensusResult, folded,
              judgment_of, members, states, now, llm_strong=None, *, refs=None):  # fmt: skip
        """Consensus outcomes to operations, decisions and per-observation outcomes."""
        ops: list[Operation] = []
        outcomes: dict[str, ObservationOutcome] = {}
        decisions: list[ConsensusDecision] = []
        reviews: list[str] = []
        refs = refs or (self.consensus.ref.ref, self.edge_policy.ref.ref, model.ref.ref,
                        self.policy.ref.ref, profile.ref.ref)  # fmt: skip

        def decide(members_, outcome, target=None, flags=None, cut=None) -> str:
            d = ConsensusDecision(
                decision_id=self.ids.decision(),
                actor=Actor(kind="system", id="consensus"),
                run_id=run.run_id,
                members=tuple(members_),
                outcome=outcome,
                target_entity_id=target,
                flags=flags or {},
                suggested_cut=None
                if cut is None
                else SuggestedCut(keep=cut[0], move=cut[1], disagreements=0),
                component_refs=refs,
                created_at=now,
            )
            decisions.append(d)
            return d.decision_id

        def provisional_review(cluster, kind, decision_id):
            review_id = self.ids.review()
            # A node held at a weak point is reviewed with its strong neighbors.
            context = tuple(sorted(set(cluster.members) | set(cluster.linked)))
            ops.append(OpenReview(review_id=review_id, kind=kind, members=context,
                                  decision_id=decision_id))  # fmt: skip
            reviews.append(review_id)
            for obs in cluster.members:
                if obs in scope_set:
                    entity = self.ids.entity(profile.id_prefix)
                    ops.append(CreateEntity(entity_id=entity, entity_type=profile.entity_type,
                                            state="provisional", members=(obs,),
                                            decision_id=decision_id))  # fmt: skip
                    outcomes[obs] = ObservationOutcome(obs, "REQUIRES_REVIEW", entity,
                                                       "provisional", decision_id)  # fmt: skip

        for cluster in result.clusters:
            inside = set(cluster.members)
            strong_edges = sorted(
                judgment_of[i]
                for pair, (cls, idx) in folded.items()
                if cls == "strong" and pair[0] in inside and pair[1] in inside
                for i in idx
                if i in judgment_of
            ) + sorted(d for (a, b), d in (llm_strong or {}).items()
                       if a in inside and b in inside)  # fmt: skip
            flags = {
                "conflicts": [list(c) for c in cluster.conflicts],
                "bridges": [list(b) for b in cluster.bridges],
                "articulations": [[a, list(p)] for a, p in cluster.articulations],
                "held_out": cluster.held_out,
                "pending": cluster.pending,
                "edges": strong_edges,
            }
            scope_obs = [m for m in cluster.members if m in scope_set]
            outcome = cluster.outcome
            if outcome == ConsensusOutcome.NO_CHANGE:
                continue
            if outcome == ConsensusOutcome.ATTACH and self.policy.attach_needs_review(
                len(members.get(cluster.target, ())), len(scope_obs)
            ):
                flags["policy"] = "blast_radius"
                outcome = ConsensusOutcome.REVIEW_CLUSTER
            decision_id = decide(cluster.members, outcome,
                                 cluster.target if outcome not in _REVIEW else None,
                                 flags, cluster.suggested_cut)  # fmt: skip
            if outcome in _REVIEW:
                kind = "merge" if outcome == ConsensusOutcome.PROPOSE_MERGE else "cluster"
                provisional_review(cluster, kind, decision_id)
            elif outcome in (ConsensusOutcome.CREATE_ENTITY, ConsensusOutcome.CREATE_PROVISIONAL):
                state = "active" if outcome == ConsensusOutcome.CREATE_ENTITY else "provisional"
                entity = self.ids.entity(profile.id_prefix)
                ops.append(CreateEntity(entity_id=entity, entity_type=profile.entity_type,
                                        state=state, members=tuple(scope_obs),
                                        decision_id=decision_id))  # fmt: skip
                for obs in scope_obs:
                    outcomes[obs] = ObservationOutcome(obs, "CREATE_ENTITY", entity, state,
                                                       decision_id)  # fmt: skip
            else:  # ATTACH or MERGE_PROVISIONAL
                target = cluster.target
                merged = [n for n in cluster.members if n in states and n != target]
                if merged:
                    ops.append(Merge(survivor_id=target, merged_ids=tuple(merged),
                                     decision_id=decision_id))  # fmt: skip
                for obs in scope_obs:
                    ops.append(Attach(observation_id=obs, entity_id=target,
                                      decision_id=decision_id))  # fmt: skip
                    outcomes[obs] = ObservationOutcome(obs, "ATTACH_TO_EXISTING", target,
                                                       states[target], decision_id)  # fmt: skip

        for boundary in result.boundaries:
            nodes = sorted({n for pair in boundary.pairs for n in pair})
            flags = {
                "pairs": [list(p) for p in boundary.pairs],
                "clusters": [list(result.clusters[i].members) for i in boundary.clusters],
            }
            decision_id = decide(nodes, ConsensusOutcome.BOUNDARY_REVIEW, flags=flags)
            review_id = self.ids.review()
            ops.append(OpenReview(review_id=review_id, kind="boundary", members=tuple(nodes),
                                  decision_id=decision_id))  # fmt: skip
            reviews.append(review_id)
        return ops, outcomes, decisions, reviews

    def _held_nodes(self) -> set[str]:
        """Nodes under an open review: its members, and the current entities of its
        observation members."""
        held: set[str] = set()
        observations: list[str] = []
        for review in self.store.open_reviews():
            for node in review.members:
                held.add(node)
                if node.startswith("obs_"):
                    observations.append(node)
        held.update(self.store.entities_of(observations).values())
        return held

    def _pair_cases(self, profile, model, pairs, folded, rows, members):
        """What the adjudicator sees for each contested node pair: both nodes' records
        and the comparison results behind the pair, strongest first (design 05 §4.2)."""
        observations = {o.observation_id: o for o in self.store.observations(
            sorted({n for p in pairs for n in p if n.startswith("obs_")}))}  # fmt: skip

        def record(node: str) -> dict[str, Any]:
            if node in observations:
                obs = observations[node]
                return dict(obs.attributes or {})
            snapshot = self.get_entity(node)[1]
            out = dict(snapshot.attributes) if snapshot else {}
            out["observation_count"] = len(members.get(node, ()))
            return out

        definitions = dict(profile.attributes)
        definitions.setdefault("observation_count", AttributeDefinition(
            name="observation_count", data_type="integer"))  # fmt: skip
        cases = []
        for pair in pairs:
            _, idx = folded[pair]
            behind = sorted(idx, key=lambda i: -rows[i]["weight"])[:4]
            comparisons, keys = [], {}
            for i in behind:
                r = rows[i]
                for c in profile.comparisons:
                    label = r.get(f"level_{c.name}")
                    if label is None:
                        continue
                    local = f"E{len(comparisons) + 1}"
                    bits = model.level_weight(c.name, c.levels.index(label))
                    comparisons.append({"id": local, "comparison": c.name, "level": label,
                                        "bits": round(bits, 1)})  # fmt: skip
                    keys[local] = (r["left_id"], r["right_id"], c.name)
            cases.append(PairCase(nodes=pair, records=(record(pair[0]), record(pair[1])),
                                  comparisons=tuple(comparisons), evidence_keys=keys,
                                  weight=max(rows[i]["weight"] for i in idx)))  # fmt: skip
        return cases, definitions

    def _adjudication_records(self, adjudicator, adjudications, by_pair, run_id, stage_id, now):
        """Each answer as a pair judgment citing the evidence it named, plus its own
        ``llm_adjudication`` evidence item. Returns judgments, evidence, and the
        judgment id of every answer that set a strong edge."""
        judgments, evidence, strong = [], [], {}
        verdicts = {"same": Verdict.MATCH, "different": Verdict.NO_MATCH,
                    "related": Verdict.NO_MATCH, "unsure": Verdict.REQUIRES_REVIEW}  # fmt: skip
        for adjudication in adjudications:
            answer = adjudication.answer
            if answer is None:
                continue
            left, right = adjudication.case.nodes
            cited = []
            for local in answer.supporting + answer.contradicting:
                left_obs, right_obs, comparison = adjudication.case.evidence_keys[local]
                cited += [e.evidence_id for e in by_pair.get((left_obs, right_obs), ())
                          if e.attribute == comparison]  # fmt: skip
            item = EvidenceItem(
                evidence_id=self.ids.evidence(), left_id=left, right_id=right,
                kind="llm_adjudication", attribute="llm_adjudication", level=answer.decision,
                direction={"same": "supports", "different": "contradicts"}.get(
                    answer.decision, "neutral"),
                value={"decision": answer.decision, "relation": answer.relation,
                       "reason_codes": answer.reason_codes, "rationale": answer.rationale},
                method_ref=adjudicator.prompt.ref.ref, run_id=run_id, stage_id=stage_id,
                created_at=now,
            )  # fmt: skip
            evidence.append(item)
            judgment = PairJudgment(
                decision_id=self.ids.decision(),
                actor=Actor(kind="agent", id=adjudicator.model),
                run_id=run_id, stage_id=stage_id, left_id=left, right_id=right,
                verdict=verdicts[answer.decision],
                scores=(Score(kind="llm_decision", value=answer.decision),
                        Score(kind="llm_applied", value=adjudication.applied)),
                evidence_ids=(item.evidence_id, *sorted(set(cited))),
                component_refs=(adjudicator.ref.ref, adjudicator.prompt.ref.ref),
                invocation_id=adjudication.invocation_id,
                created_at=now,
            )  # fmt: skip
            judgments.append(judgment)
            if adjudication.applied and answer.decision == "same":
                strong[(left, right)] = judgment.decision_id
        return judgments, evidence, strong

    def _stage(self, run_id: str, kind: str, producer: str, family: str, now: datetime) -> str:
        stage = StageExecution(stage_id=self.ids.stage(), run_id=run_id, kind=kind,
                               producer_ref=producer, family=family, view="raw",
                               started_at=now)  # fmt: skip
        self.store.record_stage(stage)
        return stage.stage_id

    @staticmethod
    def _blocking_keys(profile, features, scope) -> frozenset[str]:
        exact = [g.feature for g in profile.candidate_generators if isinstance(g, ExactValue)]
        return frozenset(
            f"{feature}={value}"
            for obs in scope
            for feature in exact
            for value in features.get(obs, {}).get(feature, ())
        )

    # -- operator actions ---------------------------------------------------------------------

    def _operate(self, action: OperatorAction, subjects: Sequence[str], reason: str,
                 op_builder: Callable[[str], Operation], actor: Actor | None,
                 evidence_ids: Sequence[str] = ()) -> CommitResult:  # fmt: skip
        decision = OperatorDecision(
            decision_id=self.ids.decision(),
            actor=actor or Actor(kind="human", id="operator"),
            action=action,
            subject_ids=tuple(subjects),
            reason=reason,
            evidence_ids=tuple(evidence_ids),
            created_at=self.clock(),
        )
        self.store.record_decisions([decision])
        changes = ChangeSet(base_seq=self.store.head_seq(), ops=(op_builder(decision.decision_id),))
        return self.store.commit(changes, actor=decision.actor, canonicalizer=self.canonicalizer)

    def merge_entities(self, survivor: str, others: Sequence[str], *, reason: str,
                       expected_versions: Mapping[str, int] | None = None,
                       actor: Actor | None = None) -> CommitResult:  # fmt: skip
        return self._operate(
            OperatorAction.MERGE, [survivor, *others], reason,
            lambda d: Merge(survivor_id=survivor, merged_ids=tuple(others), decision_id=d,
                            expected_versions=dict(expected_versions) if expected_versions
                            else None),
            actor,
        )  # fmt: skip

    def split_entity(self, entity_id: str, observations: Sequence[str], *, reason: str,
                     into: str | None = None, expected_version: int | None = None,
                     actor: Actor | None = None) -> CommitResult:  # fmt: skip
        """Move observations out of an entity into a new one, or back into the retired
        entity ``into`` (an unmerge, which reactivates it)."""
        prefix = self.store.entity(entity_id).entity_id.split("_", 1)[0]
        target = into or self.ids.entity(prefix)
        return self._operate(
            OperatorAction.SPLIT, [entity_id, target], reason,
            lambda d: Split(source_id=entity_id, targets={target: tuple(observations)},
                            decision_id=d, expected_version=expected_version),
            actor,
        )  # fmt: skip

    def promote(self, entity_id: str, *, reason: str, actor: Actor | None = None) -> CommitResult:
        return self._operate(OperatorAction.PROMOTE, [entity_id], reason,
                             lambda d: Promote(entity_id=entity_id, decision_id=d), actor)  # fmt: skip

    def relate(self, subject: str, relation: str, object_: str, *, reason: str,
               evidence_ids: Sequence[str] = (), effective_from: date | None = None,
               effective_to: date | None = None, actor: Actor | None = None) -> str:  # fmt: skip
        relationship_id = self.ids.relationship()
        self._operate(
            OperatorAction.RELATE, [subject, object_], reason,
            lambda d: Relate(relationship_id=relationship_id, relation=relation,
                             subject_entity_id=subject, object_entity_id=object_,
                             evidence_ids=tuple(evidence_ids), effective_from=effective_from,
                             effective_to=effective_to, decision_id=d),
            actor, evidence_ids,
        )  # fmt: skip
        return relationship_id

    # -- reviews and constraints ----------------------------------------------------------------

    def reviews(self, kind: str | None = None):
        """Open reviews, most impactful first (design 04 §2.3)."""
        from thisness.reviews import list_reviews

        return list_reviews(self, kind)

    def review_case(self, review_id: str):
        """A review with everything a reviewer needs, from stored records (design 04 §2.2)."""
        from thisness.reviews import build_case

        return build_case(self, review_id)

    def resolve_review(self, review_id: str, resolution, *, groups=None, relation=None,
                       reason: str, actor: Actor | None = None,
                       recommendation_id: str | None = None) -> CommitResult:  # fmt: skip
        """Answer a review: one change set of merges, promotions, relationships,
        constraints and the closure (design 04 §2.4). ``recommendation_id`` names the
        model recommendation the reviewer was shown, if any (design 05 §5)."""
        from thisness.reviews import resolve

        return resolve(self, review_id, resolution, groups=groups, relation=relation,
                       reason=reason, actor=actor, recommendation_id=recommendation_id)  # fmt: skip

    def recommend(self, review_id: str, *, assistant=None):
        """Ask the review assistant for a recommendation and record it (design 05 §5)."""
        assistant = assistant or self.assistant
        if assistant is None:
            raise ThisnessError("no review assistant is configured")
        return assistant.recommend(self, review_id)

    def recommend_open(self, *, kind: str | None = None, limit: int = 50, assistant=None):
        """Recommendations for the most impactful open reviews that have none yet."""
        assistant = assistant or self.assistant
        if assistant is None:
            raise ThisnessError("no review assistant is configured")
        pending = [s.review.review_id for s in self.reviews(kind)[: max(0, limit)]
                   if not self.recommendations(s.review.review_id)]  # fmt: skip
        return assistant.recommend_many(self, pending)

    def recommendations(self, review_id: str):
        return self.store.recommendations(review_id)

    def assistant_agreement(self) -> dict[str, Any]:
        """How often reviewers' answers matched the recommendations they were shown."""
        from thisness.assistant import agreement

        recommendations = {d.decision_id: d for d in self.store.decisions_of_type("recommendation")}
        return agreement(self.store.decisions_of_type("review"), recommendations)

    def constraints(self, observation_ids: Sequence[str] | None = None):
        return self.store.constraints(observation_ids)

    def revoke_constraint(self, constraint_id: str, *, reason: str,
                          actor: Actor | None = None) -> CommitResult:  # fmt: skip
        from thisness.core.run import RevokeConstraint

        return self._operate(
            OperatorAction.REVOKE_CONSTRAINT,
            [constraint_id], reason,
            lambda d: RevokeConstraint(constraint_id=constraint_id, decision_id=d),
            actor,
        )  # fmt: skip

    def relationship_candidates(self, entity_id: str | None = None,
                                entity_type: str | None = None):  # fmt: skip
        """Entity pairs with relationship signals, not yet related (design 04 §2.6).
        Accepting one is ``relate``."""
        from thisness.relationships import relationship_candidates

        return relationship_candidates(self, entity_id, entity_type)

    def labels(self):
        """Observation pairs labeled by answered reviews (design 04 §2.7)."""
        from thisness.labels import labels

        return labels(self)

    def dossier(self, entity_id: str, *, members_offset: int = 0, members_limit: int = 50):
        """Everything Thisness believes about one entity, from stored records (design 04
        §3). A retired id is followed to the entity it was merged into."""
        from thisness.dossier import build

        return build(self, entity_id, members_offset=members_offset,
                     members_limit=members_limit)  # fmt: skip

    def entities(self, entity_type: str, *, state: str | None = None, query: str | None = None,
                 offset: int = 0, limit: int = 50):  # fmt: skip
        """A page of live entities, largest first, with open-review counts."""
        from thisness.browse import entities

        return entities(self, entity_type, state=state, query=query, offset=offset, limit=limit)

    def overview(self) -> dict[str, Any]:
        """Counts for a store at a glance."""
        from thisness.browse import overview

        return overview(self)

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.runs(limit)

    def observation(self, observation_id: str) -> Observation:
        from thisness.browse import observation

        return observation(self, observation_id)

    def invocation(self, invocation_id: str):
        """One model call: what was sent, what came back, what it cost (design 05 §3.2)."""
        return self.store.invocation(invocation_id)

    def invocations(self, *, run_id: str | None = None, limit: int = 500):
        return self.store.invocations(run_id=run_id, limit=limit)

    def search(self, q: str, limit: int = 8):
        """Entities by name, or any record by its id."""
        from thisness.browse import search

        return search(self, q, limit)

    # -- reads -------------------------------------------------------------------------------

    def names(self, entity_ids: Sequence[str]) -> dict[str, str]:
        """Each entity's current name, its id when it has none, in one query."""
        found = self.store.snapshot_names(list(entity_ids))
        return {e: found.get(e, e) for e in entity_ids}

    def get_entity(self, entity_id: str) -> tuple[Entity, EntitySnapshot | None]:
        entity = self.store.entity(entity_id)
        try:
            snapshot = self.store.snapshot(entity_id)
        except NotFound:
            snapshot = None
        return entity, snapshot

    def resolve_id(self, entity_id: str) -> list[str]:
        return self.store.resolve_id(entity_id)

    def entity_of(self, observation_id: str, as_of_seq: int | None = None) -> str | None:
        return self.store.entity_of(observation_id, as_of_seq)

    def members(self, entity_id: str, as_of_seq: int | None = None) -> list[str]:
        return self.store.members(entity_id, as_of_seq)

    def history(self, *, entity_id: str | None = None,
                observation_id: str | None = None) -> list[LedgerEvent]:  # fmt: skip
        return list(self.store.ledger(entity_id=entity_id, observation_id=observation_id))

    def explain(self, decision_id: str) -> dict[str, Any]:
        """A decision with the evidence and pair judgments it rests on, assembled from
        stored records; no model is consulted (PRD §32)."""
        found = self.store.decisions([decision_id])
        if not found:
            raise NotFound(f"decision {decision_id}")
        decision = found[0]
        out: dict[str, Any] = {"decision": decision.model_dump(mode="json")}
        out["evidence"] = [e.model_dump(mode="json")
                           for e in self.store.evidence(list(decision.evidence_ids))]  # fmt: skip
        if decision.invocation_id is not None:
            out["invocation"] = self.store.invocation(decision.invocation_id).model_dump(
                mode="json"
            )
        edges = getattr(decision, "flags", {}).get("edges", [])
        if edges:
            out["edges"] = [self.explain(d) for d in edges[:50]]
        return out


def yield_to_conflicts(
    edges: dict[tuple[str, str], str],
    model_same: set[tuple[str, str]],
    model_apart: set[tuple[str, str]] | None = None,
) -> set[tuple[str, str]]:
    """Revert to contested every model-set strong edge inside a component that the
    model's own "different" answers, or a cannot-link constraint, contradict; in place,
    returning the reverted pairs. The evidence alone then decides those components; the
    model's answers stay recorded. With ``model_apart`` None, any negative pair counts.

    Only the model's own "different" counts, not the evidence's negative edges: a
    company's members often disagree on an old address, and a right "same" across that
    is the model doing its job (organizations: recall .983 against .964 when every
    negative edge counted)."""
    from thisness.consensus import components

    nodes = {n for pair in edges for n in pair}
    strong = [pair for pair, c in edges.items() if c == "strong"]
    member_of = {}
    for i, component in enumerate(components(nodes, strong)):
        for n in component:
            member_of[n] = i

    def conflicting(pair: tuple[str, str], c: str) -> bool:
        if c == "cannot_link":
            return True
        return c == "negative" and (model_apart is None or pair in model_apart)

    contradicted = {member_of[a] for (a, b), c in edges.items()
                    if conflicting((a, b), c) and member_of.get(a) == member_of.get(b)}  # fmt: skip
    yielded = {pair for pair in model_same if member_of.get(pair[0]) in contradicted}
    for pair in yielded:
        edges[pair] = "contested"
    return yielded


def _adjudication_stats(adjudicator, adjudications) -> dict[str, Any]:
    client = adjudicator.client
    answered = [a for a in adjudications if a.answer is not None]
    return {
        "mode": adjudicator.mode,
        "model": adjudicator.model,
        "pairs": len(adjudications),
        "answered": len(answered),
        "decisions": dict(Counter(a.answer.decision for a in answered)),
        "unanswered": dict(Counter(a.status for a in adjudications if a.answer is None)),
        "applied": sum(a.applied for a in adjudications),
        "calls": client.calls,
        "spent_usd": round(client.spent_usd, 6),
    }


def _representatives(members: list[str], features: Mapping[str, Mapping[str, list[str]]],
                     k: int) -> list[str]:  # fmt: skip
    """Up to ``k`` members with distinct feature signatures, most recent first."""
    seen, out = set(), []
    for m in members:
        signature = tuple(sorted((f, tuple(v)) for f, v in features.get(m, {}).items()))
        if signature in seen:
            continue
        seen.add(signature)
        out.append(m)
        if len(out) == k:
            break
    return out
