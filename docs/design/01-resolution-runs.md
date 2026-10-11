# Design 01: Resolution Runs, Proposals, and Consensus

Status: decisions D1 to D6 accepted 2026-10-07; D1 amended by design 03 §10 (holdout). Open questions in §12.
Covers: batch semantics (the gap behind `resolve_many`) and independence of repeated model passes (appendix A.9.4 to A.9.10).
Sketch: `docs/design/sketches/consensus_sketch.py` (stdlib only; output in `consensus_sketch.out`).

---

## 1. Problems

**Batch resolution is unspecified.** The requirements define `resolve()` for one observation and name `resolve_many()` without semantics. Implementing it as a loop of single attaches makes the outcome depend on input order: the first "M. Khan" seeds an entity, later ones are judged against a snapshot that has already moved, and two new observations that refer to each other (and to no existing entity) are never compared directly. The LLM appendix separately introduces `begin_resolution_run`, `ClusterProposal` and `build_consensus`, but only for the name-only LLM path.

**Repeated passes are counted as independent when they are not.** The name-only workflow works because each pass consumes the previous pass's output: "AWS 8721" is relabeled "Amazon Web Services" in pass 1, re-sorts next to the other Amazon Web Services names, and is grouped with them in pass 2. That feedback is the mechanism. But A.9.4 then counts pass-2 agreement as a stability vote, which is the model agreeing with its own earlier output. A.9.5's co-assignment rate also divides by "passes where both were visible", which is 0/0 for exactly the acronym and alias pairs the workflow exists to catch, with no way to distinguish "never shown together" from "shown together and kept apart".

## 2. Summary

1. **One batch model for every resolver.** All resolution happens inside a `ResolutionRun`. `resolve(obs)` is a run with one observation. `resolve_many(obs)` is a run with many. There is no second code path (requirements §26).
2. **Producers emit proposals, not state.** Pair resolvers emit pair judgments; clusterers (LLM batches, connected components, humans) emit group proposals. Every group proposal records what the producer *saw*, not only what it grouped.
3. **Consensus is engine-level and signal-agnostic.** A profile's `EdgePolicy` turns all signals on a pair into one edge class. Consensus consumes only edge classes.
4. **One vote per stage per pair, and only raw views vote.** A stage shown upstream-derived labels can discover edges but cannot confirm them.
5. **"Never compared" is a state, not a zero.** A positive signal with no independent confirmation leaves a boundary unsettled.
6. **Validated components (D1).** A component of strong edges is accepted only if it has no internal negative or cannot-link pair, no load-bearing bridge and no load-bearing articulation node. Failures go to review with the weak point shown, and, where there is contradicting evidence, a suggested cut. A component that fails only on conflicts may hold a small conflict cover and accept the rest (§7.2, amended by design 03 §10).
7. **Entities are atomic inside a run (D2).** A run never splits an existing entity. It emits split proposals for review.
8. **Cluster outcomes have their own decision type (D3).** `ConsensusDecision`, alongside a `ResolutionDecision` generalized to observation-or-entity on both sides.
9. **Two independent looks before acting, per profile (D4).** Name-only default `min_votes=2`; the shipped default is to be confirmed or lowered by benchmark.
10. **Unsettled clusters become provisional entities (D5).** Every observation leaves a run with an ID. Provisional entities merge without review when later evidence is strong; active entities never do.
11. **Large clusters: collapse, then anchor (D6).** Deterministic variant collapse before any model sees the names; verify sub-batches share at least two anchors.
12. **Commit is optimistic against a base ledger position.** Commit checks what changed since the run's base and rebases only the affected clusters.

---

## 3. The resolution run

```text
ResolutionRun
  run_id
  profile, profile_version
  scope            observation ids being resolved
  base_seq         ledger sequence number the run reads from
  plan             ordered stages (from the profile)
  status           planned | executing | proposed | committed | abandoned
```

**Nodes.** A run operates over a node set: every observation in scope, plus every existing entity that candidate generation surfaced (active or provisional, §7.4). Existing entities are **atomic nodes**: a run can attach to them, merge provisional entities into them, propose merging two active ones, or flag them, but it cannot split them (D2). Splits stay explicit operations (requirements §23).

**Stages.** A plan is a list of stages. Each stage execution is recorded:

```text
StageExecution
  stage_id, run_id
  kind             candidates | pair_resolver | group_proposer | verify | human
  producer         name + version (resolver, model + prompt version, method)
  family           independence class (see 5.4)
  view             raw | derived
  conditioned_on   stage_ids whose output shaped this stage's input
```

The advanced API (requirements §26) maps onto stages: `find_candidates` and `collect_evidence` are stages, `resolve_candidates` is a pair-resolver stage, `apply_policy` runs after consensus, `commit` applies the result.

**Example plans.**

```python
# Person, Organization with structured attributes: no LLM.
RunPlan(
    stages=[
        CandidateStage(generators=[exact, blocking, fuzzy]),   # obs->entity and obs<->obs pairs
        EvidenceStage(providers=[...]),
        PairResolverStage(resolver=FellegiSunter(...)),
    ],
    edge_policy=FellegiSunterEdgePolicy(...),
    consensus=ValidatedComponents(),
)

# Name-only suppliers (appendix A.9).
RunPlan(
    stages=[
        CollapseStage(normalizer=org_name_key),                 # §6.4, deterministic
        GroupStage(proposer=LLMGrouper("grouper-v1"), planner=Alphabetical(size=100, overlap=50), view="raw"),
        GroupStage(proposer=LLMGrouper("grouper-v1"), planner=ByUpstreamLabel(of="p1"), view="derived"),
        VerifyStage(proposer=LLMVerifier("verifier-v1"),
                    planner=ProposedClusters(distractors=5, anchors=2), repeats=2),
    ],
    edge_policy=VoteEdgePolicy(min_votes=2),
    consensus=ValidatedComponents(),
)
```

**API.**

```python
run = engine.start_run(entity_type="organization", observations=obs)   # plan from profile
run.execute()                       # runs stages, persists proposals; no identity change
result = run.consensus()            # clusters, outcomes, boundaries; no identity change
changes = engine.apply_policy(result)
engine.commit(changes)              # checks base_seq, rebases if needed, appends ledger events

engine.resolve_many(obs)            # = all of the above, commit only if policy allows
engine.resolve(obs)                 # = resolve_many([obs])
```

Because consensus sees the whole scope at once, two new observations that co-refer are linked to each other directly, and the result does not depend on input order (verified in the sketch under repeated shuffles of proposal order, for every scenario).

---

## 4. Proposals

Two shapes, plus constraints.

**PairJudgment.** The existing `ResolutionDecision`, generalized so either side can be an observation or an entity (D3):

```text
PairJudgment
  left, right      NodeRef (observation | entity)
  decision         MATCH | NO_MATCH | POSSIBLE_MATCH | REQUIRES_REVIEW
  scores, evidence_ids, resolver, resolver_version, stage_id
```

This changes requirements §12, where the right side is always `candidate_entity_id`. Inside a batch, observation-to-observation judgments are necessary.

**GroupProposal.** Generalizes the appendix's `ClusterProposal`:

```text
GroupProposal
  proposal_id, stage_id, batch_id
  shown            node -> the exact string or view the producer received
  groups           list of node sets (a partition of a subset of shown)
  uncertain        nodes the producer flagged
  labels_out       node -> proposed canonical label (optional)
  relations_out    proposed non-identity relations (optional)
  invocation_id    ModelInvocationRecord, when a model produced it
```

`shown` is mandatory. It is what makes "kept apart" distinguishable from "never seen together", and what lets echo detection work (5.2). Nodes in `shown` but in no group are singletons, which means "kept apart from everything else in this batch".

**Constraints.** Must-link and cannot-link pairs from hard constraints (profile), authoritative identifiers, prior human review decisions and resolution memory. Cannot-link always wins in consensus.

---

## 5. Votes and independence

### 5.1 One vote per stage per pair

Overlapping windows put the same pair in two batches of the same pass. Those are not two votes. For each (pair, stage): if every batch that showed both nodes grouped them, the stage votes *co-assigned*; if any batch separated them, it votes *not co-assigned*. A stage that disagrees with itself across overlapping windows is not agreement.

### 5.2 Raw views vote; derived views discover

A stage with `view="raw"` was shown source values. Its votes count toward stability.

A stage with `view="derived"` was shown labels produced upstream. Its output is recorded, and it is how chained passes discover cross-batch edges, but:

- **Echo.** If two nodes were shown the *identical* upstream label, grouping them adds nothing. That pair is counted as an echo, not a vote. (In the AWS example, pass 2 groups "AWS" with "Amazon Web Services" because both were shown the label "Amazon Web Services".)
- **Derived votes** on pairs with different shown labels are recorded separately, are invalidated if the upstream labels they were conditioned on are revoked, and do not count toward stability.

### 5.3 Label convergence

When one stage independently assigns the same proposed label to two nodes in *different* batches, that is real, weak evidence: the model reached the same canonical form without seeing the two together. It is recorded as its own evidence kind, `llm_label_convergence`, and makes a pair worth verifying. It is not a vote.

### 5.4 Families

Repeated votes from the same model and prompt share its systematic biases (for example, a tendency to fold subsidiaries into parents). Agreement within a family measures sensitivity to batch context, not correctness. So:

- every stage declares a `family` (model + prompt version, or method name);
- pair statistics are kept per family;
- agreement across families (an LLM and Fellegi-Sunter, or two different models) is a separate evidence kind, `cross_family_agreement`;
- none of these is a probability until calibrated against labeled pairs (appendix invariant 7).

The sketch exercises raw/derived, echo and label convergence. It does not yet exercise families.

### 5.5 Pair statistics

```text
PairStats(pair)
  raw_votes        stage -> co-assigned?      n = len, k = sum
  derived_votes    stage -> co-assigned?
  echoes           int
  label_convergence bool
  per_family       family -> (n, k)
```

`n < min_votes` with a positive signal is the state **unverified**. It is not stability 0.

### 5.6 Storage

A 60,000-name list in batches of 100 with overlap 50 is about 1,200 batches per pass and about 6 million pair co-visibilities per pass. Pair rows are not stored. The durable record is the proposals (about 120,000 membership rows per pass). Pair statistics are a reproducible view computed in DuckDB on demand. `EvidenceItem` rows are materialized only for pairs with at least one positive signal, and for any pair a decision cites. Separated pairs remain derivable from the proposals.

---

## 6. Name-only: discover by chaining, confirm by co-presentation

### 6.1 Stages

1. **Collapse.** Deterministic variant collapse (§6.4).
2. **Raw grouping pass.** Alphabetical (or other locality) planner, raw names. Produces votes for pairs in the same window, and labels.
3. **Chained pass(es).** Batches built by sorting on pass-1 labels. Discovers edges across the original windows (AWS meets Amazon Web Services). Votes are derived; identical-label pairs are echoes.
4. **Verify.** Batches built from proposed clusters (components over all positive, non-negative edges), shown with **raw** values, plus the nearest non-members as distractors (so "Amazon Bank" is in the batch that decides about "Amazon"). Run at least twice with different distractor sets, or with a second family.

Verify cost scales with the number of nodes that have a positive but unconfirmed edge, not with the list. In typical supplier lists most names have no positive edge at all.

### 6.2 Edge policy

`VoteEdgePolicy(min_votes=m)`, default `m = 2` (D4):

| Edge class | Rule |
|---|---|
| cannot_link | any constraint |
| strong | n ≥ m and k = n |
| negative | n ≥ m and k/n < 0.5 |
| contested | n ≥ m, otherwise (mixed independent votes) |
| unverified | n < m and any positive signal (k > 0, a derived co-vote, or label convergence) |
| none | no positive signal |

Other profiles supply their own `EdgePolicy` (for example, Fellegi-Sunter match weight bands plus constraints). Consensus does not care how classes were reached.

### 6.3 min_votes (D4)

With `m = 2` the verify stage is what produces automatic outcomes. With `m = 1` a single raw grouping acts. The sketch shows the difference on the Amazon example without verify:

- `m = 2`: the existing entity is unchanged, every Amazon and AWS variant becomes its own provisional entity, Amazon Bank becomes active.
- `m = 1`: the Amazon variants attach to the existing entity; "Amazon Web Services" + "LLC" and "AWS" + "AWS 8721" become two provisional fragments.

`m` is a profile setting. The shipped default stays 2 until the adversarial benchmark (requirements §47) shows single-look precision meets the target; then it may drop to 1. This is a measurement, not a preference.

### 6.4 Clusters larger than one verify batch (D6)

**Collapse first.** Before any model call, observations are grouped by a deterministic, profile-specific key (case, punctuation, legal-suffix tokens) and each group is represented by one member. In the sketch, 300 raw spellings of Amazon with 15 account suffixes collapse to 30 representatives. Members keep their own observation ids; the collapse is recorded as `normalized_exact` evidence and its rule is versioned like any normalizer. The key is a profile decision with known risk: stripping legal-suffix tokens also equates "Amazon Inc" and "Amazon LLC", which may be distinct legal entities (§12).

**Then anchor.** When a proposed cluster still exceeds a verify batch, it is split into sub-batches that **all share at least two anchor nodes** (the cluster's highest-degree members). The sketch, with 30 representatives and a batch of 12 including one distractor:

| Sub-batch plan | Result |
|---|---|
| Plain partition | Three provisional fragments, linked only by unverified edges |
| One shared anchor, articulation check off | Accepted as one 30-member entity, held together by a single node |
| One shared anchor | `REVIEW_CLUSTER`: articulation node holds together parts (10, 10, 9) |
| Two shared anchors | Accepted as one 30-member entity |

The one-anchor row is why §7.2 now checks articulation nodes. Without that check, one node holding three groups together passes validation, which is the node-shaped version of the chain error. In this data the cluster happens to be correct, but its structure is indistinguishable from a wrong one. Two anchors remove the single point of failure without any special "verified through anchors" rule.

---

## 7. Consensus: validated components (D1)

### 7.1 Components

Take components over **strong** edges only (deterministic union-find on sorted ids).

### 7.2 Validation

A component goes to `REVIEW_CLUSTER` if it has any of:

- **an internal negative or cannot-link pair** (the chain error: A≈B, B≈C, A≠C);
- **a load-bearing bridge**: a strong edge whose removal splits the component into two parts of size ≥ 2;
- **a load-bearing articulation node**: a node whose removal leaves two or more parts of size ≥ 2.

A single observation hanging off one strong edge, or a hub with single-observation leaves, is normal and not flagged.

**Holdout (amendment, design 03 §10).** If a component fails only on conflicts (no load-bearing bridge or articulation node), take a greedy vertex cover of its conflict graph, most conflicts first, ties by id. If the cover is at most 10% of the component, the covered observations are held (each to review, merged nowhere) and validation runs again on the rest; otherwise the whole component goes to review. Nothing contradicted is ever merged. Components under 10 observations never qualify, so the Apple scenario below still goes wholly to review. In design 03's sketch this raised heavy-supplier recall from 0.56 to 0.92 with no new false merges.

### 7.3 What the reviewer sees

- **Contradicting evidence present:** a suggested cut, computed as the minimum-disagreement two-way partition (correlation clustering restricted to one cut). Offered, never applied. In the Apple scenario it suggests moving "Apple Inc." out and leaving "Apple" with the bank names, which is exactly the judgment a reviewer should make rather than a solver.
- **Structural flag only (bridge or articulation):** the weak point itself. With only positive edges, the minimum-disagreement answer is "do not cut", so a suggested cut would be meaningless.

Correlation clustering is not a default consensus strategy. It remains the natural second `ConsensusStrategy` for profiles that need it.

### 7.4 Outcomes and entity states

`Entity.state` is `active | provisional | retired`.

| Accepted component contains | Outcome |
|---|---|
| two or more **active** entities | `PROPOSE_MERGE` (policy-gated; default review) |
| one active entity and ≥ 1 provisional | `MERGE_PROVISIONAL` into the active entity |
| no active, ≥ 2 provisional | `MERGE_PROVISIONAL` into the oldest provisional |
| one entity (either state) plus observations | `ATTACH` |
| one entity alone | no change |
| observations only, boundary settled | `CREATE_ENTITY` (active) |
| observations only, boundary unsettled | `CREATE_PROVISIONAL` |

**Unsettled boundary.** A component has an unsettled boundary if any member has an *unverified* edge to a node outside it. Inside a run with a verify stage, such components are marked `NEEDS_VERIFICATION` and feed that stage. At the end of the run, the rule above applies (D5): every observation leaves with an ID, but an unsettled new cluster gets a provisional one.

**Why provisional.** Deferring leaves rows with no ID, which is a hole in spend totals and in any entity dossier. Creating active entities would make every later correction a merge of two confirmed identities, which always goes to review. A provisional entity has a real, stable ID immediately, and later strong evidence merges it without review (sketch scenario 4: three provisional Amazon entities and a new observation merge into the active Amazon entity, while two active AWS entities still go to `PROPOSE_MERGE`).

**Promotion.** A provisional entity becomes active (`ENTITY_PROMOTED`) when a verify stage covering it leaves no unverified edges at its boundary. A merged provisional entity is retired with a redirect, exactly like any merge. IDs are never reused.

**Boundaries.** Contested edges between two accepted components produce a `BOUNDARY_REVIEW`: "same entity, or related (for example `subsidiary_of`)?" This is the appendix's Amazon/AWS outcome, routed to relationship review instead of an identity merge. As built, boundaries are grouped by neighborhood: one review per connected group of clusters joined by contested edges, not one per pair (design 03 §20).

### 7.5 Split proposals for existing entities (D2)

A run never splits an entity. But when a verify stage shows members of an existing entity and keeps some of them apart (a negative edge between two members), the run emits a `SPLIT_REVIEW` with the members, the conflicting pairs and a suggested cut (sketch scenario 5: "Acme Bank" kept apart from "Acme Corp" and "ACME Corporation" twice). The entity is unchanged until a reviewer acts.

### 7.6 ConsensusDecision (D3)

Every outcome produces a `ConsensusDecision`, which is the basis for the memberships and merges it causes:

```text
ConsensusDecision
  decision_id, run_id
  members          node ids
  outcome          CREATE_ENTITY | CREATE_PROVISIONAL | ATTACH | MERGE_PROVISIONAL
                   | PROPOSE_MERGE | REVIEW_CLUSTER | SPLIT_REVIEW
  target           entity id, for ATTACH / MERGE_PROVISIONAL
  flags            conflicts, bridges, articulations (for review outcomes)
  suggested_cut    for review outcomes with contradicting evidence
  edge_evidence    evidence ids of the strong edges, plus the PairStats view version
  consensus        strategy + version
  edge_policy      name + version
```

`IdentityMembership.basis_decision_id` may point at a `PairJudgment` (single-candidate path) or a `ConsensusDecision`. That keeps architectural invariant 3 ("every accepted identity assignment must be traceable to a decision") without forcing a group outcome into a pairwise record. `explain()` and the UI render both shapes.

---

## 8. Commit

```text
ChangeSet
  run_id, base_seq
  ops              CreateEntity | CreateProvisional | AttachMembership | MergeProvisional
                   | ProposeMerge | ProposeSplit | PromoteEntity | OpenReview
  read_set         entity ids read + blocking keys queried during candidate generation
```

On commit, the engine checks ledger events after `base_seq`:

- an event touching an entity in the run's node set, or
- an entity created under one of the run's blocking keys

is a conflict. Only the clusters containing affected nodes are rebased: candidate generation reruns for those observations against current state, and consensus reruns on that component. Cached model results are reused (appendix A.13), so a rebase rarely re-calls a model.

The second rule closes the race where two concurrent single-observation runs both create an entity for the same thing: neither touched an existing entity, but both queried the same blocking key.

For the MVP, commits are serialized per store (DuckDB is single-writer anyway), so the conflict check is one ledger query. PostgreSQL can use the same check under an advisory lock per profile.

Ledger additions: `RUN_STARTED`, `RUN_PROPOSED`, `RUN_COMMITTED`, `RUN_ABANDONED`, `PROVISIONAL_CREATED`, `PROVISIONAL_MERGED`, `ENTITY_PROMOTED`, `SPLIT_PROPOSED`. Every event gains `run_id`.

---

## 9. Sketch results

`python3 docs/design/sketches/consensus_sketch.py` (full output in `consensus_sketch.out`). Every scenario returns identical results under repeated shuffles of proposal order.

**1. Amazon / AWS (appendix A.20).** Pass 1 raw, pass 2 chained, two raw verify stages, one of which dissents and folds AWS into Amazon. One active entity, "Amazon.com, Inc.".

```text
ATTACH              [Amazon.com, Inc. [active], Amazon, Amazon.com Inc., Amazon - 008712]
CREATE_ENTITY       [Amazon Web Services, Amazon Web Services LLC, AWS, AWS 8721]
CREATE_ENTITY       [Amazon Bank]
BOUNDARY_REVIEW     cluster 0 <-> 1: 8 contested pairs
AWS / Amazon Web Services: raw votes 2/2, echoes 1, label convergence True
Amazon / Amazon Web Services: raw votes 1/3
```

**1b. Same, without verify, `min_votes=2` and `min_votes=1`.** See §6.3.

**2. Chain through overlapping windows.** "Apple" sits in two windows of the same pass, grouped with "Apple Inc." in one and with the Apple Bank names in the other. Seen together, Apple Inc. and Apple Bank are kept apart.

```text
REVIEW_CLUSTER      [Apple Inc., Apple, Apple Bank, Apple Bank NA]
    conflict: Apple Inc. / Apple Bank (negative)
    suggested cut: [Apple, Apple Bank, Apple Bank NA] | [Apple Inc.]
```

**3. Load-bearing bridge, no contradicting evidence.**

```text
REVIEW_CLUSTER      [J. Smith (Acme), John Smith (Acme), John Smith (Globex), Jon Smith (Globex)]
    bridge: John Smith (Acme) / John Smith (Globex) splits (2, 2)
```

**4. The next run, after 1b left provisional entities.**

```text
MERGE_PROVISIONAL   [Amazon.com, Inc. [active], Amazon [provisional], Amazon.com Inc. [provisional],
                     Amazon - 008712 [provisional], AMAZON.COM INC]  -> into Amazon.com, Inc. [active]
PROPOSE_MERGE       [Amazon Web Services LLC [active], AWS [active], AWS 8721 [provisional]]
```

**5. Verify contradicts an existing entity.**

```text
NO_CHANGE           [Acme Corp [active]]
SPLIT_REVIEW        Acme Corp [active] (entity unchanged)
    suggested: [Acme Corp, ACME Corporation] | [Acme Bank]
```

**6. Large cluster.** See §6.4.

History: the first version of the rules created every unconfirmed name as its own active entity (fixed by the boundary rule, now provisional). The second version accepted a cluster held together by one shared node (fixed by the articulation check).

---

## 10. Tradeoffs

- **Name-only needs two independent looks before acting by default.** The verify stage is effectively mandatory for automatic output, at roughly one extra pass over the proposed clusters.
- **A batch run cannot repair a bad historical merge.** It can only propose the split.
- **Provisional entities are a second tier consumers must understand.** Every API that returns entities exposes `state`; downstream users must decide whether provisional counts.
- **Validated components over-flag sparse clusters.** A real entity whose observations were each compared only to a neighbor trips the bridge or articulation check. Profiles with rich pairwise resolvers may need correlation clustering or a larger part-size threshold.
- **Pair evidence for separated pairs is computed, not stored.** "Why weren't these two merged?" is answered by recomputing from proposals. Reproducible, but slower than a lookup.

## 11. Decisions (accepted 2026-10-07)

| # | Decision | Where |
|---|---|---|
| D1 | Validated components as default consensus; correlation clustering only as a suggested cut for reviewers, and as an optional second strategy. Amended by design 03 §10: hold a conflict cover of at most 10% and accept the rest. Amended by design 03 §21: split at weak points (hold articulation nodes, cut bridges) and validate the parts | §7.2, §7.3 |
| D2 | Runs never split existing entities; they emit split proposals | §3, §7.5 |
| D3 | New `ConsensusDecision`; `ResolutionDecision` generalized to `NodeRef` on both sides | §4, §7.6 |
| D4 | `min_votes` per profile, name-only default 2; revisit by benchmark | §6.2, §6.3 |
| D5 | Unsettled clusters become provisional entities, not deferred | §7.4 |
| D6 | Deterministic variant collapse, then verify sub-batches sharing ≥ 2 anchors | §6.4 |

## 12. Open questions

| # | Question | Leaning |
|---|---|---|
| 1 | Do `get_entity`, search and dossiers include provisional entities by default? | Include, with `state` on every result; coverage is the point of D5 |
| 2 | Collapse key for organizations: strip legal-suffix tokens or not? | Strip for collapse only, and record the suffix as an attribute that a constraint can use ("Inc" vs "LLC" with other differences becomes cannot-link) |
| 3 | Anchor choice when the highest-degree nodes are themselves doubtful | Choose anchors only among members with all-strong edges; if fewer than two exist, verify cannot proceed and the cluster stays provisional |
| 4 | Promotion timing for provisional entities never covered by a verify stage | Remain provisional; a scheduled sweep run verifies the oldest provisionals |
| 5 | Part-size threshold for bridge and articulation checks (currently 2) | Keep 2 for name-only; make it a profile setting and tune on person data |

## 13. Properties to test

- **Order independence:** shuffling scope and proposal order gives the same consensus.
- **Echo:** a chained stage that only sees identical labels adds no votes.
- **Never-compared:** a pair with label convergence and no raw co-visibility is `unverified` and leaves both boundaries unsettled.
- **Chain error:** intransitive grouping across overlapping windows yields `REVIEW_CLUSTER` with a suggested cut.
- **Bridge:** a single strong edge joining two strong groups yields `REVIEW_CLUSTER`.
- **Articulation:** a single shared node joining groups of size ≥ 2 yields `REVIEW_CLUSTER`; a hub with single leaves does not.
- **Provisional:** unsettled new clusters get provisional IDs; later strong evidence merges them without review; two active entities still go to `PROPOSE_MERGE`.
- **Promotion:** a provisional entity with no unverified boundary edges after verify becomes active.
- **Split proposal:** verify evidence against an existing entity's members yields `SPLIT_REVIEW` and leaves the entity unchanged.
- **Collapse:** members of a collapsed group keep their own observation ids and memberships.
- **Rebase:** a conflicting commit after `base_seq` rebases only the affected clusters and reuses cached model results.
- **Create race:** two concurrent single-observation runs over the same blocking key cannot both create.
