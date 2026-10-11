# Appendix A — LLM-Assisted Resolution Requirements for Thisness

## Purpose

This appendix defines the requirements for the **LLM-assisted resolution layer** in Thisness.

The goal is not to make Thisness dependent on large language models. The goal is to make LLMs a first-class, optional capability for cases where deterministic rules, lexical similarity, probabilistic linkage, or structured machine learning are insufficient.

LLMs should be treated as **reasoning and evidence-generation components**, not as the authority that owns identity state.

The core principle is:

> **Thisness may ask a model to reason about identity, but only Thisness may decide whether that reasoning changes identity state.**

This distinction is foundational.

---

## A.1 Design goals

The LLM layer should support several classes of resolution tasks:

1. **Evidence extraction** from unstructured text.
2. **Contextual adjudication** between an observation and one or more candidate entities.
3. **Name-only canonicalization** where very little structured evidence exists.
4. **Cluster review** for groups of highly similar observations.
5. **Relationship classification** where two entities are related but not identical.
6. **Human review assistance** through concise evidence summaries.
7. **Offline teacher/distillation workflows** where an expensive model creates labeled examples for cheaper deterministic or ML resolvers.

The LLM layer must remain:

- optional;
- provider-agnostic;
- schema-constrained;
- auditable;
- replayable;
- versioned;
- privacy-aware;
- unable to directly mutate canonical identity state.

---

## A.2 Non-goals

The LLM layer should **not**:

- directly write identity memberships;
- directly merge entities;
- directly split entities;
- invent external identifiers;
- silently add canonical aliases;
- override hard constraints;
- silently replace deterministic evidence;
- expose a universal model confidence score as truth;
- require OpenAI, Anthropic, Gemini, or any other provider for core Thisness functionality.

Thisness should run successfully with the LLM package extras entirely absent.

---

## A.3 Recommended architecture

The LLM component should be split into distinct interfaces.

```text
                      ┌──────────────────────────┐
                      │      Thisness Core       │
                      └─────────────┬────────────┘
                                    │
          ┌─────────────────────────┼─────────────────────────┐
          │                         │                         │
          ▼                         ▼                         ▼
  LLMEvidenceExtractor       LLMAdjudicator           LLMReviewAssistant
          │                         │                         │
          ▼                         ▼                         ▼
   EvidenceItem(s)       ResolutionDecision        Review Summary
                                    │
                                    ▼
                           Resolution Policy
                                    │
                                    ▼
                            State Transition
```

A fourth component should support the name-only workflow described later:

```text
LLMCanonicalizationResolver
```

This should be a specialized resolver rather than a hidden special case.

---

## A.4 Provider abstraction

Thisness should never bind directly to one model SDK inside core resolution logic.

A generic provider interface should exist.

```python
class ModelProvider(Protocol):
    def invoke(
        self,
        *,
        messages: list[Message],
        response_schema: type[BaseModel],
        model: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ModelInvocationResult:
        ...
```

Provider implementations may include:

```text
OpenAIProvider
AnthropicProvider
GeminiProvider
LocalModelProvider
CustomHTTPProvider
```

The rest of Thisness should operate against the `ModelProvider` protocol.

A resolution profile should therefore be able to configure:

```python
resolver = LLMAdjudicator(
    provider=my_provider,
    prompt_template="organization_resolution_v3",
)
```

without changing the engine.

---

## A.5 Model invocation record

Every consequential LLM invocation must generate a durable invocation record.

Minimum fields:

```python
ModelInvocationRecord(
    invocation_id="llm_...",
    provider="anthropic",
    model="...",
    model_version=None,
    prompt_template="organization_resolution_v3",
    prompt_template_version="3.1",
    response_schema_version="2",
    input_fingerprint="...",
    created_at=...,
    latency_ms=...,
    input_tokens=...,
    output_tokens=...,
    cost=None,
    status="success",
)
```

Where possible, preserve:

- exact prompt template version;
- structured request payload or reproducible input references;
- provider;
- model name;
- model version/build if exposed;
- temperature and decoding settings;
- token usage;
- latency;
- cost;
- response schema version;
- raw structured response;
- parsing/validation errors;
- retry history.

This record should be linked to:

```text
EvidenceItem
ResolutionDecision
ReviewItem
```

as appropriate.

---

## A.6 LLM evidence extraction

An LLM should be able to transform unstructured material into typed evidence.

Example source sentence:

```text
"International Business Machines Corporation, formerly referred to herein as IBM, maintains its principal offices in Armonk, New York."
```

Possible output:

```json
{
  "evidence": [
    {
      "kind": "alias_assertion",
      "value": "IBM",
      "direction": "supports_match"
    },
    {
      "kind": "legal_name",
      "value": "International Business Machines Corporation"
    },
    {
      "kind": "location",
      "value": "Armonk, New York"
    }
  ]
}
```

These outputs should become ordinary `EvidenceItem` records.

The LLM output itself should not be treated as the final identity decision.

---

## A.7 LLM adjudication

The main adjudicator should receive:

- one observation;
- one candidate entity snapshot;
- relevant evidence;
- applicable constraints;
- relationship types;
- optional local graph context;
- explicit decision vocabulary.

Example interface:

```python
class LLMAdjudicator(Resolver):
    def resolve(
        self,
        observation: Observation,
        candidate: EntitySnapshot,
        evidence: Sequence[EvidenceItem],
        context: ResolutionContext,
    ) -> ResolutionDecision:
        ...
```

The structured model output should resemble:

```python
class LLMResolutionOutput(BaseModel):
    decision: Literal[
        "MATCH",
        "NO_MATCH",
        "POSSIBLE_MATCH",
        "REQUIRES_REVIEW",
    ]

    supporting_evidence_ids: list[str]
    contradicting_evidence_ids: list[str]

    reason_codes: list[str]

    proposed_relationship: str | None = None
```

Optional:

```python
class LLMResolutionOutput(BaseModel):
    ...
    rationale_summary: str | None = None
```

The rationale is explanatory metadata only.

The actual decision must reference machine-readable evidence.

---

## A.8 Policy remains authoritative

An LLM result is a **proposal**.

Example:

```text
LLM:
    MATCH

But:
    authoritative tax identifiers conflict

Policy:
    NO_MATCH
```

or:

```text
LLM:
    MATCH

Probability model:
    0.93

Policy:
    requires >= 0.995 for automatic identity state changes

Outcome:
    REQUIRES_REVIEW
```

The model cannot bypass policy.

This should be enforced structurally, not merely by prompt wording.

---

# A.9 Name-only entity resolution

## A.9.1 Problem definition

A common real-world case contains almost no structured identity evidence.

Example:

Across PO, AP, contract, ERP, and supplier datasets, the system may contain:

```text
Amazon
Amazon.com
Amazon COM Inc
Amazon - 812773
AMAZON WEB SERVICES
AWS
AWS 019283
Amazon Web Services LLC
```

In some environments, the only reliable field available for resolution is the **entity name itself**.

This is substantially weaker than resolving against:

- registration number;
- tax identifier;
- email domain;
- phone;
- address;
- known relationship graph.

Thisness should support this case explicitly rather than forcing users to pretend stronger evidence exists.

---

## A.9.2 Existing proven workflow

A practical workflow previously used successfully for supplier harmonization consisted of:

```text
50,000–60,000 distinct supplier names
        │
        ▼
alphabetical sort
        │
        ▼
small local batches (~100 observations)
        │
        ▼
LLM proposes normalized/canonical groups
        │
        ▼
new consolidated list
        │
        ▼
repeat 2–3 times
with shifted / overlapping batch boundaries
        │
        ▼
increasingly harmonized supplier universe
```

The important feature is the **overlapping multi-pass batching strategy**.

Passes intentionally expose observations to different local neighborhoods.

For example:

```text
Pass 1
records 1–100
records 101–200
records 201–300

Pass 2
records 51–150
records 151–250
records 251–350

Pass 3
records 26–135
records 136–245
...
```

This gives borderline entities repeated opportunities to appear alongside relevant variants.

The model is effectively performing local semantic clustering and canonicalization.

---

## A.9.3 Thisness interpretation

Thisness should formalize this workflow as a specialized resolver mode.

Working name:

```text
LLMCanonicalizationResolver
```

or:

```text
NameOnlyResolutionPipeline
```

The workflow should not directly rewrite all names.

Instead, each pass should generate **cluster proposals** and **membership evidence**.

Example:

```python
ClusterProposal(
    proposal_id="cluster_...",
    canonical_label="Amazon Web Services",
    observation_ids=[
        "obs_101",
        "obs_102",
        "obs_118",
        "obs_129",
    ],
    model_invocation_id="llm_...",
    pass_number=1,
)
```

The engine then compares proposals across passes.

---

## A.9.4 Multi-pass stability as evidence

Repeated model agreement should itself become evidence.

Suppose:

```text
obs_101 = "AWS"
obs_102 = "Amazon Web Services LLC"
obs_103 = "AWS 4831092"
```

If three differently offset passes independently place all three in the same proposed cluster, Thisness can derive:

```python
EvidenceItem(
    kind="llm_cluster_stability",
    value=1.0,
    metadata={
        "agreeing_passes": 3,
        "total_passes": 3,
    },
)
```

If only one out of three passes groups them:

```python
EvidenceItem(
    kind="llm_cluster_stability",
    value=0.33,
    metadata={
        "agreeing_passes": 1,
        "total_passes": 3,
    },
)
```

This creates an explicit representation of the intuition behind the overlapping-batch technique.

---

## A.9.5 Pairwise co-assignment matrix

For a batch of observations, Thisness may derive a pairwise co-assignment matrix.

For observations \(i\) and \(j\):

\[
S(i,j) =
\frac{\text{number of passes where i and j share a proposed cluster}}
{\text{number of passes where both were visible}}
\]

Example:

```text
                         Amazon     AWS       AWS-123
Amazon                     1.00     0.92       0.88
AWS                        0.92     1.00       0.97
AWS-123                    0.88     0.97       1.00
Amazon Bank                0.08     0.03       0.02
```

This score is **not automatically a calibrated match probability**.

It is a typed score:

```python
Score(
    kind="llm_coassignment_rate",
    value=0.92,
)
```

It may then contribute to a resolver or review policy.

---

## A.9.6 Why alphabetical sorting helps

Alphabetical sorting is not identity logic.

It is a simple candidate-locality heuristic.

Name variants often share lexical prefixes:

```text
Amazon
Amazon.com
Amazon LLC
Amazon Web Services
```

Sorting therefore creates local neighborhoods with surprisingly high candidate density.

However, alphabetical sorting can miss:

```text
AWS
Amazon Web Services
```

which is one reason repeated passes and additional candidate-generation strategies are valuable.

Thisness should therefore treat lexical ordering as one possible **batch construction strategy**, not a universal algorithm.

Other batch strategies may include:

```text
normalized prefix
TF-IDF nearest neighbors
phonetic key
embedding neighborhood
character n-gram similarity
blocking key
external alias memory
```

---

## A.9.7 Batch planner interface

The batching mechanism should itself be pluggable.

```python
class BatchPlanner(Protocol):
    def plan(
        self,
        observations: Sequence[Observation],
        *,
        batch_size: int,
        pass_number: int,
    ) -> Iterable[ResolutionBatch]:
        ...
```

Possible implementations:

```text
AlphabeticalBatchPlanner
OffsetAlphabeticalBatchPlanner
LexicalNeighborhoodBatchPlanner
EmbeddingNeighborhoodBatchPlanner
RandomizedOverlapBatchPlanner
HybridBatchPlanner
```

The existing supplier workflow maps naturally to:

```text
OffsetAlphabeticalBatchPlanner
```

---

## A.9.8 Canonical label proposal

The model may propose a preferred label:

```text
Amazon Web Services
```

but that label is **not the entity identity**.

A stable entity still receives:

```text
org_01K...
```

The label becomes an `EntitySnapshot` property or `AliasAssertion`.

Example:

```python
EntitySnapshot(
    entity_id="org_01K...",
    canonical={
        "name": "Amazon Web Services",
    },
)
```

The model's canonical label must therefore be separable from:

```text
cluster membership
entity identity
alias claims
```

This prevents a renamed string from becoming a destructive merge primitive.

---

## A.9.9 Iterative consolidation

The name-only pipeline should support repeated passes.

Pseudo-workflow:

```python
state = engine.begin_resolution_run(
    entity_type="organization",
    observations=observations,
)

for pass_config in [
    PassConfig(batch_size=100, offset=0),
    PassConfig(batch_size=100, offset=50),
    PassConfig(batch_size=75, offset=25),
]:
    proposals = llm_canonicalizer.run_pass(
        state=state,
        config=pass_config,
    )

    state.record(proposals)

result = state.build_consensus()
```

The `build_consensus()` stage should not blindly accept the latest pass.

It should aggregate:

```text
co-assignment frequency
canonical label agreement
proposal stability
conflicts
candidate neighborhood
prior resolution memory
```

---

## A.9.10 Conflict handling

The model may produce inconsistent proposals across passes.

Example:

```text
Pass 1:
Amazon Web Services LLC → AWS

Pass 2:
Amazon Web Services LLC → Amazon

Pass 3:
Amazon Web Services LLC → AWS
```

Thisness should preserve all three proposals.

A consensus stage may conclude:

```text
AWS membership stability: 0.67
Amazon membership stability: 0.33

Outcome:
REQUIRES_REVIEW
```

The system should never silently discard disagreement.

---

## A.9.11 Cluster-level model prompt

The model should be given a constrained cluster task.

Conceptually:

```text
You are resolving organization names.

Each input row is an observation, not a canonical truth.

Group only observations that are highly likely to represent
the same underlying organization.

Do not merge:
- parent and subsidiary companies,
- brands and legal entities unless clearly equivalent,
- semantically related companies,
- companies with similar names but distinct identities.

For each group:
- return member observation IDs;
- propose a canonical label;
- identify uncertain members;
- identify potential relationships that are not identity;
- do not invent information not present in the input.

Return only the provided schema.
```

The exact prompt should be versioned and tested like code.

---

## A.9.12 Example structured response

```json
{
  "groups": [
    {
      "canonical_label": "Amazon Web Services",
      "members": [
        "obs_103",
        "obs_104",
        "obs_119"
      ],
      "uncertain_members": [
        "obs_121"
      ],
      "reason_codes": [
        "ACRONYM_EXPANSION",
        "ACCOUNT_SUFFIX_REMOVED",
        "LEGAL_SUFFIX_VARIATION"
      ]
    }
  ]
}
```

These results become proposal records.

They do not directly create memberships.

---

## A.10 LLM ensemble behavior

Thisness should allow multiple model-assisted passes or models.

Examples:

```text
Claude + GPT
same model, different prompts
same model, different batch neighborhoods
same model, multiple temperatures/seeds
LLM + probabilistic resolver
LLM + deterministic constraints
```

Consensus should be explicit.

Potential evidence:

```text
model_agreement_count
cross_model_agreement
cross_pass_agreement
decision_stability
```

Again, these are evidence types, not universal confidence.

---

## A.11 Escalation path

A model-assisted resolution should be able to follow this path:

```text
cheap deterministic rules
        │
        ▼
lexical / candidate generation
        │
        ▼
probabilistic or weighted resolver
        │
   ambiguous only
        ▼
LLM adjudicator
        │
   still ambiguous / high-risk
        ▼
human review
```

LLMs should generally be used where their reasoning value is worth the cost.

This keeps latency and token usage under control.

---

## A.12 Cost controls

Every LLM-enabled profile should support:

```text
maximum model calls per observation
maximum candidate count sent to model
maximum tokens per request
batch size
concurrency
budget per resolution run
provider/model fallback
cache policy
```

Example:

```python
LLMPolicy(
    max_candidates=5,
    max_calls_per_observation=2,
    max_input_tokens=12_000,
    budget_usd=25.00,
)
```

The system should expose cost metrics in evaluation.

---

## A.13 Caching and replay

A model call may be cached using:

```text
input fingerprint
prompt version
model identifier
response schema version
generation settings
```

If these remain unchanged, Thisness may reuse the prior result.

Changing:

```text
model
prompt
evidence
candidate snapshot
policy-relevant context
```

should invalidate the cached result.

---

## A.14 Privacy requirements

Each attribute should declare whether it may be exposed to remote models.

Example:

```python
AttributeDefinition(
    name="ssn",
    sensitivity="restricted",
    external_model_policy="forbidden",
)
```

The LLM adapter must build its request from only permitted fields.

Possible policies:

```text
allow
redact
hash
local_models_only
forbidden
```

A profile should be able to require:

```text
LLM execution = local only
```

for sensitive workflows.

---

## A.15 Human review integration

The LLM should help the reviewer, not replace them.

A review item may include:

```text
Observation
Top candidate
Supporting deterministic evidence
Contradictory evidence
LLM adjudication
LLM rationale summary
Prior model disagreement
Cluster impact
```

A reviewer decision becomes an immutable `ReviewDecision`.

It should be possible to compare:

```text
LLM recommendation
vs
human final decision
```

for evaluation and future resolver improvement.

---

## A.16 Evaluation requirements

The LLM layer must be evaluated independently from the overall engine.

Metrics should include:

```text
pairwise precision
pairwise recall
false merge rate
false split rate
review escalation rate
human-model agreement
schema-validation failures
model refusal rate
decision stability across repeated runs
decision stability across model versions
token usage
cost per resolved observation
latency
```

For name-only multi-pass resolution, additionally measure:

```text
co-assignment stability
cluster stability across passes
canonical-label stability
cross-batch boundary error rate
```

---

## A.17 Drift testing

Because hosted model behavior can change over time, Thisness should maintain fixed evaluation fixtures.

Example:

```text
fixtures/
    person_resolution/
    organization_resolution/
    name_only_suppliers/
    relationship_vs_identity/
```

A new model or prompt version should be run in shadow mode before production use.

Compare:

```text
new decision
old decision
human gold label
evidence references
```

Particular attention should be paid to **new false merges**.

---

## A.18 Proposed package structure

```text
thisness/
│
├── models/
│   ├── base.py
│   ├── provider.py
│   ├── invocation.py
│   ├── registry.py
│   └── prompts.py
│
├── evidence/
│   └── llm.py
│
├── resolvers/
│   ├── llm_adjudicator.py
│   └── llm_canonicalizer.py
│
├── canonicalize/
│   ├── batch_planner.py
│   ├── consensus.py
│   └── llm.py
│
└── integrations/
    └── llm/
        ├── openai.py
        ├── anthropic.py
        ├── gemini.py
        └── local.py
```

Provider packages may ultimately live behind optional extras.

---

## A.19 Example: person adjudication

```python
result = engine.resolve(
    entity_type="person",
    attributes={
        "name": "Bob Smith",
        "employer": "Acme Industrial",
        "title": "VP Procurement",
    },
)
```

Candidate:

```text
Robert J. Smith
Head of Strategic Sourcing
Acme Industrial
```

Evidence:

```text
nickname-compatible name
exact employer
semantically compatible title
no email available
no contradictory identifiers
```

The LLM may return:

```json
{
  "decision": "POSSIBLE_MATCH",
  "supporting_evidence_ids": [
    "ev_name",
    "ev_employer",
    "ev_title"
  ],
  "contradicting_evidence_ids": [],
  "reason_codes": [
    "NICKNAME_COMPATIBLE",
    "EMPLOYER_MATCH",
    "ROLE_COMPATIBLE"
  ]
}
```

Policy may then require review because no strong identifier exists.

---

## A.20 Example: supplier name-only resolution

Input observations:

```text
Amazon
Amazon.com Inc.
Amazon - 008712
Amazon Web Services
AWS
AWS 8721
Amazon Bank
```

A pass may propose:

```text
Cluster A
    Amazon
    Amazon.com Inc.
    Amazon - 008712

Cluster B
    Amazon Web Services
    AWS
    AWS 8721

Unmatched
    Amazon Bank
```

A second shifted pass may produce the same grouping.

A third pass may disagree on whether `Amazon Web Services` should be consolidated into the parent organization.

Thisness should interpret that disagreement as evidence that identity semantics require caution.

It may produce:

```text
Amazon ↔ Amazon.com Inc.
    stable identity proposal

AWS ↔ Amazon Web Services
    stable identity proposal

Amazon ↔ AWS
    related organization / possible hierarchy
    not accepted as identity
```

This is exactly why the resolution layer must separate:

```text
similarity
canonical label
identity
relationship
```

---

## A.21 Required MVP behavior

The LLM layer does not need to ship in the very first core MVP, but the core interfaces must support it.

The first LLM-enabled milestone should include:

- `ModelProvider`;
- one hosted provider adapter;
- `LLMAdjudicator`;
- structured output validation;
- model invocation logging;
- evidence referencing;
- policy-mediated commit;
- basic prompt versioning;
- cost/token metadata;
- name-only `LLMCanonicalizationResolver`;
- overlapping batch planner;
- multi-pass proposal aggregation;
- consensus/stability evidence;
- review escalation.

---

## A.22 Later capabilities

Future releases may add:

```text
multi-model ensembles
local model support
automatic model routing
LLM-generated candidate blocking rules
LLM-generated evidence functions
distillation into smaller models
active-learning selection
graph-context adjudication
cluster-level reasoning
multimodal image/video evidence
agent-assisted human review
prompt optimization against benchmark suites
```

These should remain optional extensions.

---

## A.23 Architectural invariants for LLM use

1. **LLM output is never synonymous with ground truth.**
2. **Every consequential model result must be versioned and auditable.**
3. **A model may only reference evidence it was actually given.**
4. **Hard constraints may override model judgment.**
5. **No LLM may directly mutate identity state.**
6. **Name-only resolution must explicitly represent weak evidence.**
7. **Repeated model agreement may become evidence, but not an assumed calibrated probability.**
8. **Conflicting model passes must remain visible.**
9. **Canonical labels and identity membership are separate concepts.**
10. **Provider choice must remain swappable.**
11. **Remote-model exposure must respect attribute-level privacy policy.**
12. **Human review remains a first-class resolver path.**
13. **LLM functionality must be optional to the Thisness core package.**
14. **Every prompt used for resolution should be treated as a versioned software artifact.**
15. **The evaluation harness must measure model drift and false-merge regressions.**

---

## A.24 Summary

The LLM layer in Thisness should not be a chat wrapper around entity matching.

It should be a disciplined reasoning subsystem.

Its role is to help Thisness turn ambiguous, heterogeneous, or minimally structured observations into:

```text
structured evidence
cluster proposals
typed identity decisions
relationship proposals
review assistance
```

while leaving the durable mechanics of identity to the core engine.

The supplier-resolution workflow described above is a particularly valuable reference implementation because it demonstrates that LLMs can perform useful entity harmonization even when the available input is effectively **name-only**.

Thisness should preserve the strengths of that approach:

```text
semantic reasoning
local grouping
iterative passes
overlapping context
progressive harmonization
```

while adding what an enterprise-grade identity substrate requires:

```text
stable entity IDs
explicit evidence
multi-pass stability
proposal history
policy
human review
reversibility
model provenance
evaluation
```

The result is not:

> “Ask an LLM which names match.”

It is:

> **Use LLMs as one disciplined source of reasoning inside a persistent, explainable, model-agnostic identity system.**
