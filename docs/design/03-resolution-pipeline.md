# Design 03: Resolution Pipeline (Normalize, Block, Compare, Fellegi-Sunter)

Status: accepted 2026-10-07, including the six decisions in §17.
Builds on: design 01 (runs, consensus) and design 02 (model, storage).
Sketch: `docs/design/sketches/03_fs_sketch.py` (synthetic organizations with ground truth; output in `03_fs_sketch.out`, identical across Python hash seeds).

---

## 1. Scope

How a run turns observations into pair judgments and edge classes for the Organization profile in M1: normalizers and features, candidate generators, comparisons and evidence, the deterministic rules resolver, Fellegi-Sunter (u from random pairs, m and λ by EM, term-frequency adjustment), the edge policy that feeds design 01's consensus, thresholds and the resolution policy.

The person profile (M2) reuses every mechanism here with different normalizers and comparisons.

## 2. Summary

Every item below was checked in the sketch. Four of them exist because the first version of the sketch failed.

1. **Blocking is a union of cheap generators.** Six generators find 96.3% of true pairs and connect 99.5% of organizations, comparing 1.2% of all pairs. Character-trigram blocking contributes most; identifier blocking finds pairs nothing else can.
2. **EM runs in blocking sessions.** A comparison's m is learned only from candidates that were not blocked on it. A single EM over all candidates puts "different country" at 15% of true matches (truth: 0%) and "same legal form" at 77% (truth: 100%). Sessions get both exactly.
3. **Correlated attributes form one comparison** *(found by the sketch)*. As separate comparisons, country, city and postal code double-count location; that contributed to linking two different Phoenix manufacturers. They are one `location` comparison.
4. **Name comparison is token-aware** *(found by the sketch)*. Whole-string Jaro-Winkler rewards a shared prefix, and in company names the prefix is the brand: "Libid Holdings" scored as a near-match to "Libid Solutions".
5. **Learned weights can be capped by the profile** *(found by the sketch)*. Fellegi-Sunter has two classes, match and non-match, and no class for "related". Random-pair u almost never samples sibling companies, so "same brand, different descriptor" and "same domain" look like strong identity evidence. Uncapped, 8 clusters merged group companies; capped, none did.
6. **Term-frequency adjustment uses entity frequency** and penalizes near-matches on common names too. Without it, the label-free threshold puts two of the 25 different "Premier Services" companies in one cluster.
7. **Consensus holds outliers instead of rejecting whole clusters** *(found by the sketch; amends D1)*. One typo'd record sent a 34-observation supplier cluster to review. Holding a small conflict cover (at most 10% of the component) and accepting the uncontradicted rest raises heavy-supplier recall from 0.56 to 0.92 (same settings otherwise) with no new false merges.
8. **Thresholds are tuned on cluster-level false merges, not pair precision.** A threshold tuned to 99.7% pair precision produced 3 false-merge clusters; the label-free threshold (posterior ≥ 0.999) produced none.
9. **The Fellegi-Sunter model is a versioned component trained offline**, never inside a run.

Best configuration on the synthetic set (entity TF, caps, holdout, label-free threshold): **B-cubed precision 1.000, recall 0.914, zero false merges, 46 of 1,673 observations in review.**

---

## 3. Pipeline

```text
observations
  -> normalize        features (rebuildable projection, design 02 §6.1)
  -> candidates       union of generators, obs<->obs and obs<->entity pairs
  -> compare          one level per comparison per pair  -> EvidenceItem
  -> constraints      cannot-link / must-link
  -> rules resolver   decisive rules (PairJudgment, Score kind "rule")
  -> Fellegi-Sunter   weight per pair (PairJudgment, Score kind "fellegi_sunter_weight")
  -> edge policy      strong | negative | contested | cannot_link  (+ relationship candidates)
  -> consensus        validated components with holdout (design 01 + §10)
  -> policy           what auto-commits (§12)
```

Each arrow is a stage in design 01's `RunPlan`, recorded as a `StageExecution` with its component ref.

## 4. Normalizers and features

A `Normalizer` is a pure, versioned function from an observation to feature rows `(feature, ordinal, value, value_num)`. Features are a projection: changing a normalizer means a new component version and a rebuild, never an identity change.

Organization features (M1):

| Feature | Derivation |
|---|---|
| `name.tokens` | NFKC, casefold, `&` to "and", collapse dotted initials (`l.l.c.` to `llc`), split on non-alphanumerics, drop legal-form tokens, expand abbreviations from the profile dictionary (`intl` to `international`, `mfg` to `manufacturing`) |
| `name.key` | tokens joined |
| `name.acronym` | initials, for names of 3+ tokens |
| `name.trigram` | character 3-grams of `name.key` |
| `legal_form` | canonical form of the dropped legal tokens (`incorporated` to `inc`), kept as its own attribute |
| `reg_id` | `SCHEME:value`, whitespace removed; compared only within a scheme |
| `domain` | lowercase registrable domain from website or email |
| `country`, `city`, `postal` | ISO country, casefolded city, alphanumeric postal code |

Abbreviation and legal-form dictionaries are profile data, versioned with the normalizer.

## 5. Candidate generators

```python
class CandidateGenerator(Protocol):
    ref: ComponentRef
    blocks_on: frozenset[str]      # attributes it blocks on (drives EM sessions, §8.3)
    def generate(self, run: ResolutionRun, store: Store) -> pa.Table: ...
        # columns: left_id, right_id, generator_ref, rank, score_kind, score_value
```

Organization generators, with the sketch's contribution of true pairs no other generator found:

| Generator | Blocks on | Rule | Unique true pairs |
|---|---|---|---|
| `identifier` | reg_id | same `SCHEME:value`, block cap 50 | 27 |
| `domain` | domain | same domain, cap 50 | 10 |
| `name_key` | name | same `name.key`, cap 100 | 0 |
| `rare_token` | name | share one of each observation's 2 rarest tokens (df ≤ 60) | 4 |
| `acronym` | name | `name.acronym` of one equals `name.key` of the other | 3 |
| `trigram` | name | share ≥ 60% of the shorter key's trigrams (df ≤ 150) | 303 |

Generators run in the store as SQL where possible (`blocking_pairs`, design 02 §8). Their union is deduplicated to `left_id < right_id`. Block caps prevent quadratic blow-up on common values; capped blocks are reported, not silently dropped.

Candidate recall is measured separately from decision quality (PRD §45): pair recall (96.3%) and **entity connectivity** (99.5%: the fraction of true entities whose observations are connected by candidate pairs). Connectivity is the number that matters for clustering, because consensus can join observations through other members.

`name_key` found no unique pairs here, but it is cheap and catches exact duplicates in one join; it stays.

## 6. Comparisons and evidence

A `Comparison` maps a pair to exactly one level from an ordered list, or `null` when either side lacks the attribute. Each (pair, comparison) becomes one `EvidenceItem` with `level` set (design 02 §6.2). Fellegi-Sunter consumes levels; the UI and `explain()` show them.

### 6.1 Organization comparisons

| Comparison | Levels (in order) |
|---|---|
| `name` | `exact_key`, `token_typo` (same token count, each token equal or Jaro-Winkler ≥ 0.90), `token_set_0.92`, `acronym`, `brand_only` (same first token, different descriptor), `else` |
| `reg_id` | `exact`, `else` (same scheme only; other schemes are null) |
| `domain` | `exact`, `else` |
| `location` | `postal`, `city`, `city_fuzzy`, `country_only`, `different_country` |
| `legal_form` | `same`, `different` |

### 6.2 Rules for writing comparisons

1. **Levels within a comparison are exclusive**, so correlation inside one attribute is never double-counted.
2. **Correlated attributes share one comparison.** City implies country. When they were separate, "loose name match + same city + same country" linked "Calkra Mfg" and "Calvex Mfg Inc", two different Phoenix companies. The single `location` comparison and token-aware names (rule 3) landed together and the pair no longer links; the sketch does not separate their contributions.
3. **Names are compared token by token.** The `brand_only` level exists so a shared brand with a different descriptor is visible as its own signal, not mistaken for a typo.
4. **Identifiers compare only within a scheme.** A mismatch within a scheme is a hard constraint (§7). Where a scheme has check digits (LEI, some VAT numbers), the normalizer validates them, and an invalid identifier is dropped rather than allowed to create a conflict.

Evidence `direction` comes from the level's declared polarity (`exact` supports, `else` contradicts); `strength` is filled after scoring from the level's weight band. Both are display aids; the weight is the quantity used.

## 7. Constraints and the rules resolver

**Constraints** run before any resolver:

- `reg_id` differs within the same scheme: **cannot-link**.
- Prior human `NO_MATCH` review decisions: cannot-link. Prior human `MATCH`: must-link.

**Rules resolver.** Deterministic rules produce decisive `PairJudgment`s with `Score(kind="rule", value=<rule name>)`, e.g. same `reg_id` in the same scheme and no location conflict: `MATCH`. If a rule fires, Fellegi-Sunter is still computed and stored (for comparison and shadow evaluation) but does not change the verdict. Cannot-link overrides everything.

## 8. Fellegi-Sunter

### 8.1 Model

For each pair, `weight = Σ log2(m_level / u_level)` over non-null comparisons, where m is P(level | match) and u is P(level | non-match). A null comparison contributes 0. The pair's judgment carries `Score(kind="fellegi_sunter_weight", value=weight, model_ref=...)`. The posterior under λ is reported as `Score(kind="fs_posterior")` and is **not** labeled a calibrated probability (PRD §11): calibration is V1.

### 8.2 u from random pairs

u is estimated from a uniform random sample of observation pairs (300,000 in the sketch), which are almost all non-matches. Random sampling is unaffected by blocking.

### 8.3 m and λ by EM, in blocking sessions

EM over candidate pairs with u fixed. The trap: candidates blocked on name all agree on name, so EM on them learns that name agreement is common among non-matches too, and distorts every other comparison to compensate.

Each generator declares `blocks_on`. Training runs one EM session per group of generators. In a session, comparisons on blocked attributes are frozen (excluded from the likelihood), and each comparison takes its m from a session that did not block on it. M1 uses two sessions: identifier/domain generators (learn name, location, legal form) and name generators (learn reg_id, domain, location, legal form).

Sketch, P(level | match):

| Level | Truth | Sessions | Single EM |
|---|---|---|---|
| reg_id `else` | 0.031 | 0.064 | 0.107 |
| location `different_country` | 0.000 | 0.000 | 0.147 |
| legal_form `same` | 1.000 | 1.000 | 0.766 |
| name `exact_key` | 0.670 | 0.688 | 0.692 |

EM runs over **level-pattern counts**, not pairs: patterns number in the hundreds, so training is fast at any candidate volume.

When a comparison is used by every generator (name-only data), no session can learn its m. That profile belongs on the LLM path (appendix A.9), or needs labeled pairs.

### 8.4 Training is offline

`engine.fit(entity_type)` trains on the store (or a sample) and registers a new Fellegi-Sunter component version with its m, u, λ and the term-frequency table version. Runs use a fixed model version; a run never retrains, so a run's result never depends on its own scope. A new model version can be evaluated in shadow against the ledger before it is used.

On an initial load with no model: ingest, `fit`, then resolve.

### 8.5 Term-frequency adjustment

For `name.exact_key`, the weight is adjusted by `log2(u_exact / f(value))`: rare names gain, common names lose. Two choices, both tested:

- **Fuzzy levels get the penalty, never the boost.** Without it, "Premier Servces L.L.C." (a typo) escaped the common-name penalty and merged with another Premier Services. `token_typo` and `token_set` add `min(0, log2(u_exact / max(f_a, f_b)))`.
- **Frequency is counted over entities, not observations.** A supplier with 35 invoices is one company, not a common name. Per-observation counting penalizes heavy suppliers. The store's entities provide entity frequency; on an initial load, a first pass without TF supplies it (two-pass).

Sketch (holdout on, caps on, label-free threshold):

| TF variant | B3 precision | B3 recall | Pair recall | Heavy-supplier recall | Premier mixed |
|---|---|---|---|---|---|
| none | 0.9992 | 0.926 | 0.808 | 0.917 | 1 |
| per observation | 1.0000 | 0.905 | 0.641 | 0.828 | 0 |
| per distinct record | 1.0000 | 0.906 | 0.644 | 0.828 | 0 |
| per entity (two-pass) | 1.0000 | 0.914 | 0.737 | 0.862 | 0 |

Entity frequency is the best of the three adjusted variants on every column. No adjustment has higher recall, but puts two Premier Services companies together; at a precision-first default that is the wrong trade. The differences between the three adjusted variants are modest on this data; the argument for entity frequency is strongest where a few suppliers dominate the volume, which is common in real spend data.

### 8.6 Weight caps: the two-class limitation

Fellegi-Sunter models two populations. Real data has three: same entity, related entity (sibling, parent, brand), and unrelated. Random-pair u describes the unrelated population, so a level that mostly fires for *related* entities looks like strong identity evidence: EM gave `brand_only` +8 bits.

The profile can therefore cap a level's weight. Organization defaults:

| Level | Cap | Why |
|---|---|---|
| `name.brand_only` | 0 bits (neutral) | relationship signal, not identity |
| `domain.exact` | +4 bits | group companies share domains; supportive, not sufficient |

Sketch: uncapped, 8 accepted clusters merged group companies; capped, none. Of 299 `brand_only` candidate pairs, 155 were sibling companies and 139 were the same company, which is why the level is neither positive nor negative evidence.

Pairs whose name level is `brand_only` additionally produce a **relationship candidate** (a proposal, not an assertion) for M2's relationship review. That is where the PRD's identity-versus-relationship separation becomes concrete.

Cap values are profile data and must be re-set against the benchmark, not taken from this sketch.

## 9. Edge policy

`FellegiSunterEdgePolicy` maps a pair to design 01's edge classes:

| Class | Rule |
|---|---|
| `cannot_link` | any constraint |
| `strong` | rule verdict MATCH, or weight ≥ T_high |
| `negative` | weight ≤ T_low (default 0 bits: evidence against outweighs evidence for) |
| `contested` | otherwise |

There is no `unverified` class for Fellegi-Sunter: every candidate pair is compared, so provisional entities (D5) arise only from the LLM path. Contested edges between accepted clusters still produce `BOUNDARY_REVIEW` (design 01 §7.4).

**Observation-to-entity edges.** An entity is compared through its members, not its snapshot (a snapshot holds one value per attribute and loses the second email or the old address):

- compare the observation with up to K representative members (distinct feature signatures, most recent first; K = 20);
- the edge's weight is the **best** member weight;
- if **any** member has a cannot-link or negative edge with the observation, the edge carries that contradiction.

Best support, any contradiction: an observation cannot attach to a large entity through one fringe member while contradicting its core.

## 10. Consensus amendment: hold outliers (amends D1)

Design 01's rule sends a whole component to review if any internal pair conflicts. In the sketch, one typo'd record ("Ycrbri Chemicals") or one mistyped registration number sent 33- and 34-observation supplier clusters to review.

Amendment: when a component fails only on conflicts (no load-bearing bridge or articulation node):

1. take a greedy vertex cover of its conflict graph (most conflicts first, ties by id);
2. if the cover is at most **10% of the component**, hold the cover (each held observation goes to review, merged nowhere) and run validation again on what remains;
3. otherwise the whole component goes to review, as before.

Nothing contradicted is ever merged; only uncontradicted observations are accepted. Components under 10 observations can never qualify, so a small contested cluster (design 01's Apple case) still goes wholly to review.

Sketch, no TF, caps on:

| | B3 recall | Heavy-supplier recall | Obs in review | False-merge clusters |
|---|---|---|---|---|
| whole-component review (D1 as written), label-free | 0.892 | 0.557 | 117 | 1 |
| with holdout, label-free | 0.926 | 0.917 | 53 | 1 |
| whole-component review, tuned | 0.807 | 0.512 | 90 | 0 |
| with holdout, tuned | 0.837 | 0.828 | 30 | 0 |

Holdout never added a false merge in any configuration.

## 11. Thresholds

- **T_low = 0 bits.**
- **T_high, label-free default**: the weight at which the posterior reaches 0.999 under the name-session λ (11.4 bits in the sketch).
- **T_high, tuned**: from the benchmark (PRD §47) once it exists, chosen to minimize false-merge clusters at acceptable recall.

The sketch's warning: tuning T_high to 99.7% pair precision gave 8.1 bits and **3 false-merge clusters**; the label-free 11.4 bits gave **none**. Pair precision averages over pairs; one bad edge contaminates a whole cluster. The evaluation harness therefore reports and tunes on cluster-level metrics: false-merge clusters, contaminated observations, largest erroneous cluster (PRD §45 identity safety), with pair precision as a diagnostic only.

## 12. Resolution policy

After consensus, `ResolutionPolicy` decides what commits automatically:

| Outcome | Default |
|---|---|
| `CREATE_ENTITY`, `ATTACH`, `MERGE_PROVISIONAL`, `CREATE_PROVISIONAL` | auto-commit |
| `PROPOSE_MERGE`, `SPLIT_REVIEW`, `REVIEW_CLUSTER`, `BOUNDARY_REVIEW` | open a review item |
| `ATTACH` that would grow an entity by more than 50% or by more than 25 observations in one commit | review (blast-radius gate) |

Policy is a versioned component, recorded on every `ConsensusDecision`.

## 13. Organization profile (M1)

| Attribute | Type | Cardinality | Sensitivity | Notes |
|---|---|---|---|---|
| `name` | string | single | public | required |
| `registration_ids` | string | multi | internal | `SCHEME:value` |
| `domain` | string | multi | public | from website or email |
| `country`, `city`, `postal_code` | string | single | public | |
| `address_line` | string | single | internal | display only in M1 |

Generators §5, comparisons §6.1, constraints §7, caps §8.6, edge policy §9, holdout §10, policy §12.

## 14. Sketch results

Dataset: 805 organizations (700 ordinary, 25 named "Premier Services", 5 heavy suppliers with 25-40 observations, 20 groups of 3 related companies sharing a domain 60% of the time, 15 with acronym variants), 1,673 observations with typos, abbreviations, legal-form variants, casing, missing fields and 1% identifier typos.

Full table in `03_fs_sketch.out`. The configurations that matter:

| Configuration | B3 P | B3 R | False-merge clusters | Obs in review |
|---|---|---|---|---|
| D1 as written, no TF | 0.9992 | 0.892 | 1 | 117 |
| + holdout | 0.9992 | 0.926 | 1 | 53 |
| + holdout, entity TF | **1.0000** | **0.914** | **0** | 46 |
| same, caps off | 0.9904 | 0.916 | 8 | 71 |
| same, tuned to 99.7% pair precision | 0.9969 | 0.972 | 3 | 22 |

## 15. Limits of this evidence

- **Synthetic data.** The generator encodes my assumptions about corruption. Real supplier data has failure modes it lacks (OCR, transliteration, mergers, recycled identifiers).
- **Parameters were chosen on the same data they were measured on**: the 10% holdout share, the two caps, the block caps. They are defaults to re-set on the benchmark, not findings.
- **Not exercised:** observation-to-entity edges (§9), incremental runs against an existing store, the rules resolver, privacy policies.
- The fact that each fix came from a sketch failure is the strongest argument for building the adversarial benchmark early in M1.

## 16. Tradeoffs

- **Caps are hand-set domain knowledge on top of a learned model.** They are visible, versioned and few, but they are not learned. The alternative (a three-class model with a "related" class) needs labeled relationships.
- **Holdout accepts part of a contradicted component.** The held observations wait for review; the accepted core may later turn out to need one of them.
- **Entity-frequency TF needs a first pass on initial load**, roughly doubling scoring cost for that load.
- **Comparing through representative members** costs up to K comparisons per observation-entity pair.

## 17. Decisions (accepted 2026-10-07)

| # | Decision | Status |
|---|---|---|
| 1 | Amend D1: hold a conflict cover of at most 10% of a component and accept the rest | Accepted |
| 2 | Profile-declared weight caps (`brand_only` ≤ 0, `domain.exact` ≤ +4) | Accepted; values re-set on benchmark |
| 3 | TF over entities, two-pass on initial load; fuzzy levels penalized, never boosted | Accepted |
| 4 | Label-free T_high (posterior ≥ 0.999) until the benchmark exists; tune on cluster-level false merges | Accepted |
| 5 | Observation-to-entity edges: best member support, any member contradiction, K = 20 representatives | Accepted |
| 6 | `brand_only` pairs produce relationship candidates (proposals) for M2 review | Accepted |

## 18. Implementation notes (M1 step 3)

Where the normalizers and generators, as built, sharpen §4 and §5:

- **Candidate scope.** Every candidate query pairs the run's scope with *all* stored observations of the run's entity type, and every pair has at least one in-scope side. On an initial load the scope is everything; on an incremental run it is the new observations, and their candidates include observations already resolved. Observations of other entity types are never candidates.
- **Generators are generic.** Four store queries (`blocking_pairs`, `rare_value_pairs`, `cross_feature_pairs`, `overlap_pairs`) and four wrapper classes (`ExactValue`, `RareValue`, `CrossFeature`, `Overlap`) implement all six organization generators. Nothing in them knows about organizations.
- **Rare-token ranking skips values no other observation holds.** A token on one observation can pair with nothing, so ranking it wastes a slot. On the design 03 dataset this changed nothing (same recall, 55 more pairs out of 16,407); with one slot per observation it is the difference between finding a brand match and not.
- **Legal forms are stripped only from the end of a name**, and never when that would leave nothing: "A.G. Barr plc" keeps `ag`, "Co-operative Bank" keeps `co`, "Company" stays `company`. Multi-token forms are kept whole ("Barr GmbH & Co. KG" has legal form `gmbh co kg`). Dotted initials collapse ("S.A.", "I.B.M.", "J.P.").
- **Abbreviations map to one canonical token** (`tech`, `technology` and `technologies` are all `technology`). The dictionaries are part of each normalizer's config hash, so editing one is a new component version.
- **Domains** come from `domain`, `website` and `email`, reduced to the registrable domain (with a small two-level suffix list such as `co.uk`). Free mail providers are dropped: `gmail.com` says nothing about an employer.
- **Registration ids** are `SCHEME:VALUE` with separators removed. LEIs are checked with ISO 7064 MOD 97-10 and dropped when invalid, so a typo cannot create a hard conflict.
- **Unknown country spellings are dropped**, not guessed.
- **Recall reproduces the sketch exactly** on the design 03 dataset: 96.3% of true pairs, 99.5% of organizations connected, the same unique contribution per generator. Normalizing and generating candidates for 1,673 observations takes about 0.1 s.

## 19. Implementation notes (M1 step 4)

Where comparisons and Fellegi-Sunter, as built, sharpen §6-§8:

- **Comparisons read features, not attributes**, so they agree with blocking by construction. Each declares the source attributes it covers (which decide the EM sessions it is frozen in) and the features it reads (shown as evidence inputs).
- **Blocking sessions are generic.** Generators are grouped by the attributes they block on; each group is one EM session in which comparisons over those attributes are frozen. A comparison's m is pooled across every session that could learn it, weighted by that session's expected matches (λ times pairs). For the Organization profile this yields three sessions (identifier, domain, name). Legal form is derived from the name, so it is frozen in the name session too.
- **The match rate λ** is re-estimated over the union of all candidates with m and u fixed, and is what the label-free threshold uses.
- **m has a floor of 0.001** *(found in step 4)*. On the design 03 dataset, true matches never disagree on domain or country, so EM learned m = 0 and those disagreements weighed about -28 bits: one second domain or one mistyped country would veto any match on real data. The floor caps a single disagreement near -10 bits. It also turned the acronym level from -12 bits (m learned as 0 from three examples) to +7.2.
- **The model is a versioned component.** Its full parameters are the registered config, `load_model` refuses a config that does not reproduce its ref, and training is deterministic for a given seed.
- **Result on the design 03 dataset**, production code end to end with the sketch's consensus: B-cubed precision 1.000, recall 0.935, no false merges, 23 observations in review (sketch best: 1.000, 0.914, 0, 46). Learned m tracks the ground truth closely for every level.
- **The label-free threshold depends on λ.** On a small dataset with a narrow vocabulary (the unit-test set), shared industry words flood the candidates, λ falls to about 0.09, and T_high rises to about 13 bits. Precision stays at 1.000 and pair recall at the threshold is partial by design; consensus recovers the rest through transitivity.

## 20. Implementation notes (M1 step 5)

Where the engine, as built, sharpens §9-§12 and design 01:

- **One code path.** `resolve` is `resolve_many` with one observation. A run scopes unresolved observations, fits a model if the type has none (`min_observations_to_fit`, default 200: below that it refuses rather than learn meaningless weights), generates candidates, scores, classifies, runs consensus, plans, records decisions and evidence, and commits. A `CommitConflict` closes the run as abandoned and reruns once from current state.
- **Observation-to-entity edges** fold every pair between an in-scope observation and an entity's members (candidates, plus up to 20 representative members with distinct feature signatures) into one edge: cannot-link if any member conflicts, contested if the best support is strong but some member is negative, otherwise the best class. Partners that are unresolved and outside the scope are dropped; they meet this run's entities in their own run.
- **Boundary reviews are grouped by neighborhood** *(found in step 5)*. One review per connected group of clusters joined by contested edges, not one per cluster pair. On the design 03 dataset this cut reviews from 295 to 83 with identical results; 25 clusters sharing a common name become one question instead of up to 300. Only a quarter of boundary pairs there are true matches; the rest are common-name collisions and sibling companies, which is what these reviews exist to ask about.
- **Incremental common-name frequency counts resolved entities only** *(found in step 5)*. Counting the run's own unresolved observations as separate entities made a supplier with three new records look like four companies and penalized exactly the matches the run should make. Bulk runs (scope over half the type) use the two-pass cluster frequency instead.
- **Review clusters and merge proposals** give each new observation its own provisional entity and open one review; nodes under open reviews (and the entities of their observations) are held, so strong edges to them are downgraded to contested.
- **Blast-radius gate**: an attach goes to review when it adds more than 25 observations, or more than half of an entity that already has at least 10. A flat 50% rule would review every second record of a new company.
- **What is recorded.** Pair judgments and evidence for every edge that is strong, contested or cannot-link, and for negative edges inside a final cluster (the conflicts a reviewer needs). Negative pairs between different clusters are not stored; they are reproducible from the model version.
- **Result on the design 03 dataset**, engine end to end: B-cubed precision 1.000, recall 0.940, no false merges, 83 reviews, invariants clean, 2.7 s for fit and resolution of 1,670 observations.
- **Small corpora get a strict threshold.** The rare-token generator's df cap is absolute (60). In a 300-observation set, industry words fall under it, flood the candidates, pull λ to about 0.1 and raise T_high to about 13 bits: precision stays 1.000 but recall drops to about 0.62. The cap should probably scale with corpus size; to be settled on the benchmark (step 6), since changing it now would move the validated design 03 numbers.

## 21. Implementation notes (M1 step 6): benchmark, tuning, cold start

The benchmark generator (`thisness.benchmark.organizations`, promoted from the §14 sketch and extended with OCR substitutions, country spellings and email domains) and harness (`thisness.benchmark.evaluate`, `python -m thisness.benchmark run`) measured the engine at 400, 805 and 4,000 organizations and two seeds. What changed because of it:

- **Minimum strong weight (12 bits for Organization).** The label-free threshold moves with λ (0.24 to 0.52 across runs), and λ tracks how cleanly blocking prunes, not how hard the data is: at 4,000 organizations it fell to 9.9 bits and produced 13 false-merge clusters. Following decision 4 (tune on cluster false merges), the profile sets a floor. An explicit `t_high` still wins.
- **The counting pass ignores the floor.** In a two-pass load, the first pass only counts how many entities share each name. With the floor, heavy suppliers fragmented into dozens of first-pass clusters, which made their own names look common and fragmented them further.
- **Weak points are split, not reviewed whole** *(second amendment to D1)*. A component that fails only on structure holds its load-bearing articulation nodes (reviewed with their strong neighbors), cuts its load-bearing bridges (reviewed as a boundary), and validates each part on its own. Nothing is merged across a weak point. A 23-record heavy supplier joined through one record had been sent to review as 23 singletons; at 400 organizations heavy-supplier recall rose from 0.385 to 0.615 and observations in review fell from 31 to 4, with no new false merges anywhere.
- **Same city, different postal code** is its own location level (location comparison v2). It was being scored as a plain city match.
- **Name frequency is looked up on a confusable-folded key** (`name.tf_key`: rn→m, 0→o, 1→l, vv→w, 5→s). Two different "Premier Services" both OCR'd to "Prernier" had matched on a rare-looking key and gained a frequency boost.
- **No frequency adjustment under 200 entities.** With four companies, every name is a quarter of the market.
- **Frequency caps stay absolute.** Raising them with corpus size lifted 4,000-organization connectivity from 0.949 to 0.996 but recall only from 0.919 to 0.928, while false merges rose from 2 to 15, pairs tenfold and run time from 11 s to 77 s. Recall is limited by the threshold, not by candidates.
- **Starter model.** `organization_starter.json` (2.8 KB) is trained on a 4,000-organization benchmark with a seed distinct from the evaluation seeds, and ships in the wheel. A store with fewer than 200 observations of a type uses it; the first run with enough data fits its own model and replaces it. A starter records the comparison components it was trained with and is refused if they change (`python -m thisness.benchmark starter` rebuilds it). On the benchmark it is competitive with a fitted model, which flatters it: it was trained on the same generator.

Results, engine end to end:

| Organizations (records) | B3 precision | B3 recall | False-merge clusters | Obs in review |
|---|---|---|---|---|
| 400 (864) | 1.0000 | 0.790 | 0 | 4 |
| 805 (1,725), seed 11 | 0.9994 | 0.888 | 1 | 8 |
| 805 (1,642), seed 12 | 0.9990 | 0.917 | 1 | 16 |
| 4,000 (8,734) | 0.9995 | 0.941 | 4 | 51 |

At the start of step 6 (no floor) the same 805 and 4,000 runs had 3 and 13 false-merge clusters at recall 0.936 and 0.949. The remaining false merges are near-identical synthetic brands ("Torgran"/"Yorgran") with thin corroboration, which the syllable-built names make far more frequent than real company names. `tests/test_benchmark.py` holds the 400-organization run as a regression floor.

## 22. Next

With designs 01 to 03 accepted, M1 implementation can start in this order: core models and IDs, DuckDB store and invariant suite (design 02), Organization normalizers and generators, comparisons and Fellegi-Sunter training, consensus and commit, then the evaluation harness and the adversarial benchmark generator (promoted from this sketch).
