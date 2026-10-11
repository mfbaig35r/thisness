# Design 05: M3 (The LLM Layer)

Status: accepted 2026-10-07 (all seven decisions as recommended).
Builds on: PRD appendix A, design 01 (runs, proposals, votes and consensus for name-only resolution) and M2 as built (commit 018178e).

---

## 1. Scope

M1 and M2 resolve organizations and people from structured attributes with Fellegi-Sunter, and send what they cannot settle to people. M3 adds models as a third source of judgment, under the rule appendix A states and the code must enforce structurally: **a model may reason about identity; only Thisness decides whether that reasoning changes identity state.**

Four capabilities, from appendix A.21:

1. **Provider layer.** A provider protocol, two adapters, durable invocation records, caching and replay, prompts as versioned components, privacy filtering by attribute policy, and cost limits.
2. **Adjudicator.** A model judges the pairs Fellegi-Sunter left contested, as one more pair-resolver stage (appendix A.7, A.11).
3. **Review assistant.** A model's recommendation on each open review, shown beside the case and measured against what the reviewer decides (A.15).
4. **Name-only resolution.** The overlapping multi-pass workflow of A.9, as design 01 §6 specified it: collapse, a raw grouping pass, a chained pass, a verify pass, and votes with `min_votes = 2`.

Not in M3: evidence extraction from free text (A.6), multi-model ensembles beyond two families, distillation, embeddings, MCP, PostgreSQL, the third test domain.

## 2. What M2 leaves for a model

| Benchmark | Records | Reviews opened | Records left provisional |
|---|---|---|---|
| Organizations, 300 | 628 | 90 | 3 |
| People, 400 | 940 | 159 | 5 |
| Organizations, 805 | 1,725 | 135 | 8 |
| People, 4,000 | 9,502 | 1,096 | 284 |

Most reviews are boundaries: contested neighborhoods where Fellegi-Sunter's weight fell between "negative" (0 bits) and "strong" (12 bits). They are the escalation target. A pair of "Premier Svcs GmbH, Cologne" and "PREMIER, SDVCS INC, Calgary" needs no person; "Zalbalt Consulting L.L.C." and "Za1baslt Consulting L.L.C." in Phoenix probably needs none either.

## 3. Provider layer

### 3.1 Protocol

```python
class ModelProvider(Protocol):
    name: str                      # "anthropic", "openai_compatible", "replay", "simulated"
    def invoke(self, request: ModelRequest) -> ModelResponse: ...

class ModelRequest(Model):
    model: str
    system: str
    messages: tuple[Message, ...]
    response_schema: dict          # JSON Schema of the expected output
    max_output_tokens: int
    temperature: float = 0.0
```

Adapters return the parsed structured output, token counts, latency and the provider's model build where it exposes one. Structured output uses each provider's native mechanism (tool use with a forced tool for Anthropic, `response_format` JSON Schema for OpenAI-compatible servers). The response is validated against the Pydantic model before anything else sees it; a validation failure is a recorded outcome, retried once, never coerced.

### 3.2 Invocation records

Migration 0003 adds `model_invocations`: `invocation_id` (`llm_` prefix), provider, model, model build, prompt ref (a `ComponentRef`), response schema version, generation settings, `cache_key`, the request payload as sent (already filtered by privacy policy, §3.5), the raw response, the parsed output, status (`ok`, `invalid`, `refused`, `error`, `cached`), retries, input and output tokens, latency, cost, run and stage ids, and created time. Pair judgments, proposals and review recommendations cite `invocation_id`; `explain` follows the link.

### 3.3 Cache and replay

The cache key is a hash of provider, model, prompt ref, schema version, generation settings and the filtered payload. A hit returns the stored response as a new invocation with status `cached`, so a rerun is free and reproducible, and any change to the prompt, model or evidence misses (A.13). `ReplayProvider` serves recorded responses by cache key and fails on a miss: the test suite runs every LLM path against recorded responses, offline and free.

### 3.4 Prompts as components

A prompt is a package resource (`thisness/prompts/adjudicate_pair.v1.md` and so on) with a declared response model. Its `ComponentRef` hashes the text and the schema, so editing a prompt is a new version, as with normalizers. Prompts are written for this project from appendix A; none is carried over from earlier work (decision 6).

### 3.5 Privacy

Every request is built from a profile's attributes through one function that applies `external_model_policy` per attribute: `allow` sends the value, `hash` and `redact` transform it, `local_only` sends it only to a provider marked local, `forbidden` never sends it. This is the rule `to_context` already applies (design 04 §9.2); M3 moves it into one shared function and adds the provider's `local` flag. A profile can require local execution outright. The filtered payload is what is stored in the invocation record, so the record never holds more than the model saw.

### 3.6 Cost limits

`LLMPolicy` per run: maximum calls, maximum input tokens per call, a budget in dollars (from a per-model price table), concurrency, and what to do when the budget runs out (stop escalating; everything not yet judged stays contested and goes to review). Spend and calls per resolved observation are reported in the run's stats and in evaluation.

## 4. The adjudicator

### 4.1 Where it sits

A pair-resolver stage after Fellegi-Sunter, in its own family (design 01 §5.4). It sees only node pairs whose folded edge is **contested** (and, optionally, **unverified**). Strong, negative and cannot-link edges are not sent: no spend where the evidence is already decisive, and a model is never asked to override a hard constraint.

### 4.2 What it is given

For each pair: the two nodes rendered as compact records (an observation, or an entity's dossier context trimmed to its canonical attributes, support counts and contradictions), the Fellegi-Sunter evidence items with their ids, levels and weights, the profile's relationship vocabulary, and the decision vocabulary. Pairs are batched several to a call (design note: RPM-bound work batches records, it does not throttle concurrency), each with its own answer slot.

### 4.3 What it returns

```python
class PairAnswer(Model):
    pair: str                                   # the pair's slot id
    decision: Literal["same", "different", "related", "unsure"]
    relation: str | None                        # from the profile's relations, when related
    supporting: tuple[str, ...]                 # evidence ids it was given
    contradicting: tuple[str, ...]
    reason_codes: tuple[str, ...]               # a fixed vocabulary per prompt
    rationale: str | None                       # explanatory only
```

Validation rejects an answer that cites an evidence id it was not given (A.23 invariant 3) or a relation outside the profile's list. Each valid answer becomes a `PairJudgment` (actor kind `agent`, the invocation id, verdict MATCH, NO_MATCH, POSSIBLE_MATCH or REQUIRES_REVIEW) and its own evidence item `llm_adjudication`.

### 4.4 What it may change (decision 3)

Two modes, set per profile and per run:

- **shadow** (shipped default): judgments are recorded and shown; edges do not change. Reviews carry the model's view (§5).
- **decide:** a contested edge becomes strong when the model says `same` and the Fellegi-Sunter weight is at least a floor (default 6 bits, half the strong threshold), and negative when it says `different`. `related` stays contested and adds a relationship candidate. `unsure` changes nothing. Consensus validation, holdout and the blast-radius policy apply unchanged, so a model's `same` still cannot pull a weak point into a component.

The default moves from shadow to decide only when the benchmark shows decide adds no false merges on both profiles at the 4,000 scale, the same rule design 01 set for `min_votes`.

## 5. The review assistant

For an open review, `engine.recommend(review_id)` asks a model the review's own question with its case (design 04 §2.2) and records the answer as a decision with actor kind `agent`: a partition of the members, a relation where it applies, reason codes and a rationale, all citing evidence ids. The Workbench shows it on the review board as a suggestion that can be applied to the columns in one click, never submitted on its own. Agreement between recommendations and reviewers' answers is a reported metric (labels, design 04 §2.7, gain the recommendation they were shown).

## 6. Name-only resolution

Design 01 §6 specified it; M3 builds it as written, with two clarifications.

- **It is a run plan, not a profile.** `resolve_many("organization", plan="name_only")` runs collapse, an offset-alphabetical raw grouping pass, a chained pass ordered by the first pass's labels, and a verify pass over proposed clusters with distractors and two shared anchors; `VoteEdgePolicy(min_votes=2)` replaces the Fellegi-Sunter edge policy for that run. The entity type stays `organization`, so a supplier list resolved by name alone and invoices resolved with identifiers end up in the same entities.
- **Stability is evidence, not probability.** Co-assignment rate, label convergence and cross-family agreement are typed scores (A.9.4, A.9.5) on the pair; none is presented as a match probability.

A planner protocol (`BatchPlanner`) ships with offset-alphabetical, by-upstream-label and proposed-cluster planners; others (n-gram neighborhoods, embeddings) can be added without engine changes.

## 7. Evaluation without spending

Three providers make the layer testable at three costs:

| Provider | Cost | Used for |
|---|---|---|
| `SimulatedProvider` | free | Pipeline mechanics at scale: an oracle that reads the benchmark's ground truth and answers with configurable error, abstain and "fold the subsidiary into the parent" rates. Tests policy and consensus under a model that is wrong in known ways. |
| `ReplayProvider` | free | The test suite: real responses recorded once, replayed by cache key. |
| Anthropic, OpenAI-compatible | real | Evaluation runs, started by hand with a budget, results recorded in the implementation notes. |

Benchmarks: the contested pairs of the Organization and Person benchmarks (adjudicator), their review queues (assistant), and a supplier-names benchmark built from the Organization generator with names only and the A.20 traps (account suffixes, acronyms, subsidiaries, a different company sharing the brand). Metrics are appendix A.16's: precision, recall and false merges with and without the model, review escalation rate, human-model agreement, schema failures, refusals, stability across repeated runs, tokens, cost per resolved observation, latency.

## 8. Workbench

- The model's view on the review board: its answer, reason codes and rationale, the evidence it cited highlighted in the pair table, and "apply suggestion".
- An LLM tab in a run's page: calls, cache hits, failures, tokens, cost, and each invocation's filtered request and response.
- Decision panels for agent decisions link to their invocation.

## 9. Order

1. Provider layer: protocol, Anthropic and OpenAI-compatible adapters, migration 0003, cache and replay, prompts as components, privacy filter, cost policy, `SimulatedProvider`.
2. Adjudicator in shadow and decide modes, with benchmark numbers from the simulated provider and one real run per profile.
3. Review assistant and the Workbench views.
4. Name-only plan, the supplier-names benchmark, and its evaluation.

## 10. Decisions

| # | Question | Recommendation |
|---|---|---|
| 1 | Order | As in §9: the provider layer and adjudicator first (small, and they reuse the whole M2 pipeline), name-only last (largest, and it needs the provider layer and the vote machinery) |
| 2 | Providers | Anthropic (native SDK) and one OpenAI-compatible HTTP adapter, which also covers OpenAI, vLLM, Ollama and open-lease deployments and is the path for `local_only` data. Each behind its own extra |
| 3 | What the adjudicator may change | Shadow by default; decide mode available, made the default only when the benchmark shows no added false merges |
| 4 | Testing without spending | Simulated oracle for mechanics, recorded replays for the suite, real providers only in budgeted evaluation runs |
| 5 | Name-only as a run plan on the Organization profile | Yes, so name-only and structured records resolve into the same entities |
| 6 | Prompts and fixtures | Written new for Thisness from appendix A's published description; no prompt text or data carried over from earlier client work; fixtures synthetic |
| 7 | Default models | Claude Sonnet 5 for adjudication and verify, Claude Haiku 4.5 for grouping passes; any model is configurable per stage |

## 11. Implementation notes

### 11.1 Step 1: the provider layer

- **Package.** `thisness.llm`: `types` (the provider protocol, `ModelRequest`, `ModelResponse`, `ProviderError`), `prompts`, `privacy`, `prices`, `client`, and `providers` (`function`, `replay`, `anthropic`, `openai_compatible`). Nothing in the core resolves through it; the dossier imports only the privacy rule. The SDKs are imported when a provider is constructed, so `thisness` installs and runs without them: `thisness[anthropic]` adds the Anthropic SDK, `thisness[openai]` adds httpx.
- **Records.** Migration 0003 adds `model_invocations`; IDs take the reserved `llm_` prefix. A record holds the filtered payload, the raw response, the validated output, status (`ok`, `invalid`, `refused`, `error`, `cached`), attempts, tokens, latency and cost. Decisions gained an optional `invocation_id`; `explain` includes the invocation. A new invariant checks that every invocation a decision, proposal or cache hit cites is recorded.
- **The client** (`LLMClient.call`) registers the prompt as a component, computes the cache key (provider, model, prompt ref, settings, payload), serves an earlier successful call as a `cached` invocation, checks limits before sending anything, invokes, validates against the prompt's response model, retries once on an invalid answer, prices the call, and records it. Provider errors and refusals are recorded and returned as no answer; they are never cached. Limits (`LLMPolicy`): calls, estimated input tokens per call, and a dollar budget checked against the worst case (estimated input, maximum output) with concurrent calls' reservations counted. A run with a dollar budget refuses a model with no listed price.
- **Prompts** are package files with `=== system ===` and `=== user ===` sections and a `{payload}` placeholder. The prompt's component ref hashes the system text, the user text and the response model's JSON Schema; the test suite checks that changing any of them changes the ref, so a cached answer to an old prompt is never reused.
- **Adapters.** Anthropic answers by forced tool use (the answer schema is the only tool's input schema); temperature is sent only when set. The OpenAI-compatible adapter sends `response_format` with the JSON Schema and checks the HTTP status before reading the body, since some gateways report a rate limit as an empty choice list. Rate limits and server errors are marked retryable; the Anthropic SDK's own retries run first.
- **Offline providers.** `FunctionProvider` (a Python function as a model: the simulated oracle of step 2 builds on it), and `RecordingProvider` and `ReplayProvider`, which write and read JSON Lines cassettes keyed by the request. A replay miss is a recorded error that names the fix.
- **Prices** (US dollars per million tokens, list prices checked 2026-10-07): Haiku 4.5 at 1 in and 5 out, Sonnet 5 at 3 in and 15 out. Pass a table for other models or rates.
- **Not yet exercised against a live API:** there is no API key in this environment. The adapters are tested against the SDK's response shapes and an HTTP mock; the first live call is part of step 2's evaluation run.
- **Tests:** `test_llm.py` (prompts, privacy, the client's cache, retries, failures and limits, both adapters, record and replay, the invocation invariant). 478 tests.

### 11.2 Step 2: the adjudicator

- **Where it runs.** `Thisness(adjudicator=...)` or `resolve_many(..., adjudicator=...)`. After the final scoring pass, every node pair the edge policy left contested goes to the adjudicator, except pairs a person is already deciding: pairs under a reviewer's constraint, and pairs touching a node held by an open review. Pairs go strongest first, eight to a call, up to `max_pairs` (500 by default); the rest are recorded as skipped and stay contested.
- **What the model sees** (`_pair_cases`, the `adjudicate_pairs` v1 prompt): each record's attributes through the shared privacy filter (an entity is its snapshot plus an observation count), the names of withheld fields, and the comparison results behind the pair (up to four record pairs, strongest first), each with a local id (E1, E2, ...), its level and its weight in bits. Records carry opaque refs. The prompt asks for same, different, related (from the profile's relations) or unsure, cited ids, reason codes from a fixed list, and prefers unsure to a guess.
- **What is kept.** An answer that cites an id it was not given, names a relation the profile does not offer, or is missing for its slot is discarded and counted; a failed call or an exhausted budget leaves its pairs unanswered. Each kept answer becomes a `PairJudgment` by an `agent` actor, citing its invocation and the Fellegi-Sunter evidence it named, plus an `llm_adjudication` evidence item carrying the decision, relation, reason codes and rationale. A `related` answer is a relationship candidate (signal `model_said_related`); `relationships.py` now accepts evidence that names entities directly.
- **Decide mode** sets an edge strong on `same` when the strongest Fellegi-Sunter weight behind the pair is at least 6 bits, and negative on `different`. Consensus decisions cite the model's judgments behind the edges it set, so `explain` reaches the model's answer from the entity.
- **Measured with the simulated oracle** (`benchmark/oracle.py`; `thisness eval --adjudicator oracle`), all contested pairs adjudicated:

  | Benchmark | Mode | Precision | Recall | False merges | Reviews |
  |---|---|---|---|---|---|
  | 805 organizations | off or shadow | 0.9994 | 0.887 | 1 | 136 |
  | | decide, a right model | 1.0000 | 0.983 | 0 | 31 |
  | | decide, wrong 5% of the time | 1.0000 | 0.875 | 0 | 47 |
  | | decide, folds 20% of related pairs | 0.9992 | 0.983 | 1 | 30 |
  | | decide, unsure 10% of the time | 0.9992 | 0.980 | 1 | 43 |
  | 1,000 people | off or shadow | 0.9983 | 0.722 | 4 | 350 |
  | | decide, a right model | 0.9996 | 0.950 | 1 | 57 |
  | | decide, wrong 5% of the time | 0.9996 | 0.856 | 1 | 105 |
  | | decide, folds 20% of related pairs | 0.9969 | 0.945 | 4 | 53 |
  | | decide, unsure 10% of the time | 0.9996 | 0.943 | 1 | 82 |

  Shadow changes nothing, as designed. A right model cuts reviews by about 80% and raises recall by 10 to 23 points. Random errors cost recall, not precision: a wrong "different" is a negative edge, which consensus turns into a split or a review, and a wrong "same" is held back by the 6-bit floor and by validation. The dangerous error is systematic: a model that folds related entities into one, as a model might with subsidiaries or household members, adds false merges on people. That is the error the real-model run has to measure before decide can become the default (decision 3).
- **Cost.** Contested pairs run to about 1,900 for 805 organizations and 1,000 people. At eight pairs per call and Sonnet 5 list prices, the simulated token counts put a 500-pair run near $0.70 and a full 805-organization run near $2.50. Real counts come from the first live run.
- **Live runs:** `thisness eval --type organization --size 300 --adjudicator anthropic --mode shadow --budget 2 --record org300.jsonl`. The cassette makes the run replayable offline.
- **Tests:** `test_adjudicate.py` (shadow changes nothing and records everything; decide with a right model resolves more and merges no more; the floor; discarded answers; failures and budgets; related answers become candidates; what a hosted and a local model may see) and an eval case in `test_cli.py`.

### 11.3 Step 3: the review assistant and the Workbench views

- **A recommendation is its own decision type** (`ReviewRecommendation`, `assistant.py`): resolution, a grouping of the review's members, a relation when it applies, reason codes, a rationale, and the judgments it cited, with its invocation. It never closes a review, never commits, and is not a label. `labels()` reads only reviewers' decisions. `ReviewDecision` gained `recommendation_id`, the suggestion the reviewer was shown.
- **What the model is sent** (the `recommend_review` v1 prompt): the review's question, the profile's relations, every member as a short ref (R1, R2, ...) with its privacy-filtered fields and source system, the current grouping, and up to 40 scored comparisons between members (strongest first, by the resolver or an adjudicator), each with an id, both refs, its weight and its levels. An entity member stands for all its observations. The answer is `same`, `different` (keep the current grouping), `groups` (an explicit partition), `related` (exactly two current groups) or `unsure`. It is discarded unless the grouping covers every member exactly once and the cited ids and the relation are ones it was given.
- **API.** `engine.recommend(review_id)`, `engine.recommend_open(kind, limit)` (the most impactful open reviews that have no recommendation yet), `engine.recommendations(review_id)`, `engine.assistant_agreement()` (over answered reviews that were shown a recommendation: the share where the reviewer chose exactly the suggested grouping, and pairwise agreement on "same entity or not").
- **Workbench.** A review shows the latest suggestion above the board, with its resolution, rationale and reason codes, a link to the exchange, and "Apply to the board", which moves the cards into the suggested columns and highlights the matching answer; the reviewer still writes the reason and submits. The comparisons the suggestion relied on are marked and listed first in the pair table, and adjudicator answers show as "model: same" there. With an assistant configured and no suggestion yet, "Ask the model" requests one. A new Model calls page lists every call with status, tokens, cost and latency, filterable by run, and opens each exchange: what was sent after privacy filtering, and the answer. Decision panels link to their model call and, for a reviewer's answer, to the suggestion they saw. The suggestion box first sat below the board; the 31-record review in the screenshot pass showed it out of view, so it moved above.
- **CLI.** `thisness ui --provider anthropic --budget 2` gives the Workbench an assistant; `thisness recommend --limit 20 --budget 2 --record reviews.jsonl` recommends answers for the most impactful open reviews in one batch.
- **Measured with the oracle** on the 300-organization queue: 90 recommendations, all usable, about $0.004 each at simulated token counts and Sonnet 5 list prices.
- **Tests:** `test_assistant.py` (recorded but changes nothing, never a label; agreement measured; each kind of unusable answer; failures and limits; what a hosted model may see; the Workbench flow from asking to answering with the suggestion recorded).

### 11.4 Step 4: name-only resolution

- **As built** (`name_only.py`, the `group_names` v1 prompt): `resolve_many(type, plan=NameOnlyPlan(...))`, or `thisness resolve --plan name-only --budget 5`. Design 01 §6's four stages, in order: collapse; a raw grouping pass over the nodes in name order, in windows of 60 overlapping by half, with Haiku 4.5 by default; a chained pass over the first pass's labels; and a verify pass that shows every component of positive, uncontradicted edges again with raw names and three nearby distractors, twice with different distractors, with Sonnet 5 by default. Large components go in sub-batches sharing their two best-connected members. Each batch is a `GroupProposal` recording exactly what the model was shown, and pair statistics are computed from proposals as design 01 §5 specifies: one vote per stage per pair (overlapping windows that disagree are not a co-assignment), derived votes that propose without counting, echoes, and label convergence. `VoteEdgePolicy(min_votes=2)` classifies edges; reviewer constraints and holds apply as in any run; validated-components consensus decides. Each classified pair is a `PairJudgment` with `llm_votes` and `llm_coassignment_rate` scores and an `llm_cluster_stability` evidence item, and consensus decisions cite the strong ones. A model answer that names an unknown item or puts one item in two groups is discarded whole.
- **Collapse keeps the legal form.** The first version stripped it, as design 01 sketched, and six different companies named "Premier Services" (GmbH, LLC, AG, PLC, Corp) became one node before any model saw them: a false merge no model could prevent. The key is now the name tokens without account numbers, plus the legal form ("amazon|inc"), with numbers removed before the legal form is read, so "Amazon, Inc. 4411" and "Amazon Inc" collapse while "Amazon" and "Amazon Inc" are judged by the model. This answers design 01 §12 question 2.
- **Existing entities.** A collapse key that matches a member of an existing entity joins that entity; such entities are nodes in the batches, shown by their canonical name. A later "Harjas Foods PLC - 998877" attaches to the entity its earlier records formed.
- **Never shown, never active.** A name that no successful batch showed (calls failed, the budget ran out) was compared with nothing, so it gets a provisional entity rather than an active one, and later evidence can merge it without review.
- **Supplier-names benchmark** (`organizations.names_only`; `thisness eval --names-only --plan name-only`): the Organization benchmark's records with only their names, a quarter of them with an appended account number. Measured with the simulated oracle as the grouping and verify model:

  | Organizations | Plan | Precision | Recall | False merges | Reviews | Calls | Cost at list prices |
  |---|---|---|---|---|---|---|---|
  | 300 | Fellegi-Sunter on names | 1.0000 | 0.478 | 0 | 127 | 0 | |
  | | name-only, a right model | 0.9968 | 1.000 | 1 | 0 | 229 | $0.58 |
  | | name-only, wrong 5% of the time | 0.9968 | 0.922 | 1 | 34 | 228 | $0.59 |
  | | name-only, folds 20% of related | 0.9889 | 1.000 | 4 | 0 | 233 | $0.59 |
  | 805 | Fellegi-Sunter on names | 1.0000 | 0.540 | 0 | 298 | 0 | |
  | | name-only, a right model | 0.9818 | 0.993 | 6 | 0 | 629 | $1.59 |
  | | name-only, wrong 5% of the time | 0.9811 | 0.918 | 7 | 85 | 613 | $1.60 |
  | | name-only, folds 20% of related | 0.9753 | 0.993 | 12 | 0 | 635 | $1.60 |

- **What limits precision.** With a right model, every remaining false merge is distinct companies with identical names after collapse ("Premier Services AG" twice, "Premier Services" thirteen times): the benchmark's 25 "Premier Services" companies. From names alone they cannot be told apart; Fellegi-Sunter declines them through its common-name adjustment, which is why its precision is 1.0 and its recall half. The plan has no such guard: a key shared by distinct entities collapses. Systematic folding of related companies adds false merges as it did for the adjudicator. Both are measurements a real model run should repeat; the oracle groups common names by truth, which a real model told to treat common names with suspicion may not.
- **Tests:** `test_name_only.py` (windows; the collapse key; one vote per stage, echoes and derived votes; recall far above Fellegi-Sunter on names with the oracle; later records joining existing entities; discarded answers and an exhausted budget leaving names provisional; folding related companies).
