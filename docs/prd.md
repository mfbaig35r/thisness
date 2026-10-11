# Thisness
## General-Purpose Identity and Resolution Engine
### Product Requirements & Technical Architecture — Draft v0.1

## 1. Executive summary

**Thisness** is an open-source Python framework for resolving imperfect observations into persistent identities.

It is intended to solve a broader problem than traditional record linkage or fuzzy matching.

The fundamental abstraction is:

> Given one or more imperfect observations, determine whether they refer to an existing underlying thing, a new thing, a related thing, or an unresolved thing — while preserving the evidence and history behind that conclusion.

A "thing" may be:

- a person;
- a company or organization;
- a supplier;
- a product;
- a physical location;
- a document or contract;
- a legal obligation;
- a risk;
- a biological specimen or species;
- a video action or concept;
- a software function;
- or an arbitrary user-defined entity.

Thisness should therefore **not hard-code the semantics of identity**.

Instead, it provides a generic identity-resolution substrate into which users can plug domain-specific:

- attributes;
- normalizers;
- candidate generators;
- evidence functions;
- constraints;
- resolvers;
- relationships;
- canonicalization rules;
- review policies.

The package should work locally as a normal Python library, expose a first-class inspection UI, scale into production persistence and search systems, and eventually expose a thin MCP interface for agentic systems.

---

# 2. Product thesis

Most entity-resolution systems concentrate on:

> "Do these two records match?"

Thisness instead focuses on:

> "What does this observation refer to, what evidence supports that conclusion, what is the system's current belief about the underlying identity, and how did that belief evolve?"

That distinction drives the architecture.

Thisness is therefore **not primarily a matcher**.

It is an **identity state, evidence, and resolution framework**.

---

# 3. Core design philosophy

The following principles are foundational.

### 3.1 Observations are not entities

A document saying:

```text
IBM Corp.
```

is an observation.

It is not the entity IBM.

Likewise:

```text
M. Khan
```

in a contract is not intrinsically a person entity.

It is an observation that may refer to one.

---

### 3.2 Identity is durable; descriptions are mutable

An entity should have a stable internal ID:

```text
org_01K...
person_01K...
contract_01K...
```

Its name, attributes, aliases, relationships, and current canonical representation can evolve without changing its identity.

---

### 3.3 Similarity is not identity

These concepts must remain distinct:

```text
similarity
candidate likelihood
evidence
identity probability
identity decision
relationship
canonicalization
```

An embedding similarity of `0.95` cannot inherently mean a 95% chance of identity.

---

### 3.4 Candidate generation is not resolution

Thisness should aggressively ask:

> "What could this observation refer to?"

Then conservatively ask:

> "What does the available evidence justify?"

Candidate recall and identity precision are separate optimization problems.

---

### 3.5 Evidence must be first-class

The system should preserve why it believes something.

Not:

```python
score = 0.94
```

but:

```text
name similarity: supportive
email exact match: strongly supportive
employer match: supportive
date-of-birth conflict: strongly contradictory
```

Every material resolution should be explainable from machine-readable evidence.

---

### 3.6 Models may reason; models do not own identity

Rules, probabilistic models, classifiers, embeddings, LLMs, and humans may all contribute.

None should directly mutate canonical identity state without going through policy.

The conceptual sequence is:

```text
model output
    ↓
ResolutionDecision
    ↓
policy / constraints
    ↓
state transition
```

---

### 3.7 Identity and relationship must never be conflated

These are different:

```text
same_entity
subsidiary_of
brand_of
parent_of
successor_to
amends
located_at
implements
variant_of
```

A high degree of relatedness is not evidence that two things are identical.

---

### 3.8 Merges must be reversible

Thisness should not implement merges by destructively overwriting source records.

If the system later discovers that one entity was actually two, it should be possible to redistribute memberships while preserving complete history.

---

### 3.9 Domain semantics are pluggable

The core engine must not assume that entities are people or organizations.

Instead:

```text
core engine
    +
resolution profile
    =
domain-specific resolution
```

---

### 3.10 Identity must be inspectable

Thisness should provide a lightweight local UI analogous philosophically to dbt's documentation/lineage experience.

Users should be able to visually inspect:

- entities;
- observations;
- candidates;
- evidence;
- decisions;
- relationships;
- merge/split history;
- resolution profiles;
- review queues.

The system should never exist only as an opaque collection of Python objects.

---

# 4. Primary use cases

## 4.1 Person resolution

Input observations:

```text
Mohammad Ali Khan
M. A. Khan
Mohammed Khan
M Khan
```

with context such as:

```text
email
phone
employer
address
date
source document
```

Thisness determines whether the observations refer to:

```text
Person A
Person B
...
```

while preserving uncertainty and provenance.

---

## 4.2 Organization resolution

Examples:

```text
IBM
IBM Corp.
International Business Machines
International Business Machines Corporation
```

should potentially resolve to one organization.

But:

```text
IBM Consulting
IBM Research
IBM subsidiary X
```

may represent related but distinct entities.

---

## 4.3 Contract resolution

Different copies or versions may refer to the same underlying agreement.

```text
Agreement_Final.pdf
Agreement_EXECUTED.pdf
scan_2384.pdf
```

may resolve to:

```text
contract_123
```

An amendment should instead be:

```text
contract_456
    amends → contract_123
```

not merged into the underlying agreement.

---

## 4.4 Risk resolution

Observations:

```text
single-source supplier dependency
sole-source component exposure
dependency on one manufacturer
```

may resolve to the same canonical risk concept.

The risk itself remains separate from:

```text
risk severity
likelihood
exposed supplier
affected contract
affected product
```

---

## 4.5 Video/action resolution

A video-indexing system might produce observations such as:

```text
vehicle entering scene
weapon reload sequence
aircraft takeoff
door breach
```

Candidate retrieval could use multimodal embeddings and temporal features.

Evidence could include:

```text
motion signatures
object tracks
temporal sequence
co-occurring objects
visual embeddings
```

The resolved entity could represent an action concept or recurring sequence.

---

## 4.6 Code resolution

A function or module becomes an observation.

Features may include:

```text
AST structure
function signature
semantic embedding
dependencies
test behavior
runtime outputs
control-flow structure
```

Resolution profiles could answer different questions:

```text
same implementation
functionally equivalent
variant of same implementation
same algorithmic concept
```

These are intentionally different identity semantics.

---

## 4.7 Scientific/custom-domain resolution

Example biological profile:

```text
entity_type = "species"

attributes:
    morphology
    wing pattern
    DNA marker
    geographic region
    host plant
```

The core engine should require no modification.

Only a new resolution profile should be necessary.

---

# 5. Core domain model

The Thisness architecture should revolve around approximately ten core objects.

## 5.1 Observation

An immutable representation of something observed in a source.

```python
Observation(
    observation_id="obs_...",
    entity_type="person",
    attributes={
        "name": "M. Khan",
        "phone": "+16025551234",
        "employer": "Acme Corp",
    },
    source=SourceRef(
        type="document",
        id="doc_456",
        locator={"page": 3},
    ),
    context={},
    observed_at=None,
)
```

Important: `attributes` must be **dynamic**.

There should not be a requirement that `PersonObservation` always has exactly:

```text
name
email
phone
employer
```

Those fields may be supplied by a default profile, but the underlying object should support arbitrary attributes.

---

# 6. Resolution profiles

This is one of the most important extensibility mechanisms in Thisness.

A `ResolutionProfile` defines what identity means for one domain.

Conceptually:

```python
class ResolutionProfile:
    entity_type: str

    attributes: dict[str, AttributeDefinition]

    normalizers: list[Normalizer]
    candidate_generators: list[CandidateGenerator]
    evidence_providers: list[EvidenceProvider]
    constraints: list[Constraint]
    resolver: Resolver
    policy: ResolutionPolicy

    relationships: list[RelationshipDefinition]
    canonicalizer: Canonicalizer
```

Thisness may ship with built-in profiles such as:

```text
Person
Organization
Product
Address
Document
```

but these are conveniences.

They are not privileged inside the core engine.

A user should be able to declare:

```python
species = ResolutionProfile(
    entity_type="species",
    ...
)
```

without editing Thisness itself.

---

# 7. Attribute system

Attributes should have semantics.

Example:

```python
AttributeDefinition(
    name="tax_id",
    data_type="string",
    cardinality="single",
    identity_strength="strong",
    sensitivity="restricted",
)
```

Potential metadata:

```text
name
type
normalizer
nullable
multi-valued
sensitivity
temporal
candidate usage
evidence usage
display behavior
```

But **identity strength should not itself automatically determine match weight**.

For example, a tax identifier may be strong in one domain but unreliable in another.

---

# 8. Candidate

A `Candidate` means:

> This entity is worth considering.

Nothing more.

Example:

```python
Candidate(
    observation_id="obs_101",
    entity_id="person_22",
    generator="fuzzy_name",
    rank=2,
    score=0.88,
    score_type="jaro_winkler",
)
```

A second generator might return the same entity:

```python
Candidate(
    observation_id="obs_101",
    entity_id="person_22",
    generator="employer_index",
    rank=1,
)
```

The engine should union and reconcile these retrieval paths.

---

# 9. Candidate generators

Built-in generators should eventually include:

```text
ExactMatchCandidateGenerator
NormalizedExactCandidateGenerator
BlockingCandidateGenerator
FuzzyStringCandidateGenerator
TFIDFCandidateGenerator
PhoneticCandidateGenerator
VectorCandidateGenerator
GraphCandidateGenerator
ExternalIndexCandidateGenerator
ResolutionMemoryCandidateGenerator
```

The protocol should remain simple:

```python
class CandidateGenerator(Protocol):

    def generate(
        self,
        observation: Observation,
        context: ResolutionContext,
        limit: int,
    ) -> Iterable[Candidate]:
        ...
```

---

# 10. Evidence

Evidence should be immutable and typed.

```python
EvidenceItem(
    evidence_id="ev_100",
    observation_id="obs_101",
    candidate_entity_id="person_22",

    kind="phone_exact_match",

    direction="supports_match",

    value=True,

    strength="strong",

    method="normalized_e164_compare",
    method_version="1.0",

    metadata={},
)
```

Possible evidence kinds:

```text
exact_identifier_match
identifier_conflict
normalized_name_match
fuzzy_name_similarity
token_similarity
address_overlap
email_domain_match
temporal_conflict
geographic_proximity
embedding_similarity
graph_neighbor_overlap
behavioral_equivalence
LLM_contextual_assessment
human_attestation
```

---

# 11. Score semantics

Thisness should intentionally reject:

```python
confidence: float
```

as the universal scoring abstraction.

Instead:

```python
Score(
    kind="calibrated_match_probability",
    value=0.992,
)

Score(
    kind="embedding_cosine",
    value=0.88,
)

Score(
    kind="bm25",
    value=6.92,
)

Score(
    kind="fellegi_sunter_weight",
    value=12.4,
)
```

The system may offer convenience properties such as:

```python
result.match_probability
```

only where a resolver genuinely produces a calibrated probability.

---

# 12. Resolution decision

A resolver evaluates one observation against one candidate.

Core decision vocabulary:

```text
MATCH
NO_MATCH
POSSIBLE_MATCH
REQUIRES_REVIEW
```

Example:

```python
ResolutionDecision(
    decision_id="dec_...",
    observation_id="obs_...",
    candidate_entity_id="person_22",

    decision="MATCH",

    scores=[...],
    evidence_ids=[...],

    resolver="weighted_person_resolver",
    resolver_version="1.2",

    created_at=...
)
```

---

# 13. Resolution outcome

The orchestrator considers all candidate decisions and produces an outcome.

```text
ATTACH_TO_EXISTING
CREATE_ENTITY
DEFER
REQUIRES_REVIEW
```

Example:

```python
ResolutionResult(
    outcome="ATTACH_TO_EXISTING",
    entity_id="person_22",
    observation_id="obs_101",
    decisions=[...],
    committed=True,
)
```

This intentionally separates:

```text
MATCH
```

from:

```text
ATTACH_TO_EXISTING
```

The former is a candidate judgment.

The latter is an identity-state operation.

---

# 14. Identity membership

Membership represents the system's current accepted belief:

```text
obs_101 → person_22
```

Example:

```python
IdentityMembership(
    observation_id="obs_101",
    entity_id="person_22",
    basis_decision_id="dec_990",
    valid_from=...,
    valid_to=None,
)
```

Membership may change.

The historical decision that caused it does not.

---

# 15. Entity

An entity should intentionally contain very little mutable business data.

```python
Entity(
    entity_id="person_22",
    entity_type="person",
    state="active",
    created_at=...
)
```

That is the actual identity object.

---

# 16. Entity snapshot

An `EntitySnapshot` represents what Thisness currently believes about the entity.

Example:

```python
EntitySnapshot(
    entity_id="person_22",
    version=11,
    attributes={
        "canonical_name": "Mohammad Ali Khan",
        "primary_email": "mkhan@example.com",
        "employer": "Acme Corp",
    },
    derived_from=[
        "obs_101",
        "obs_118",
        "obs_302",
    ],
)
```

This produces a powerful conceptual distinction:

> **Observation:** what a source told us.  
> **Entity:** what exists.  
> **EntitySnapshot:** what we currently believe about it.

---

# 17. Relationships

Relationships are first-class entities/assertions.

Example:

```python
RelationshipAssertion(
    subject_entity_id="org_1",
    relationship="subsidiary_of",
    object_entity_id="org_2",
    evidence_ids=[...],
    valid_from=...,
    valid_to=None,
)
```

Relationship definitions should be pluggable.

Examples:

```text
subsidiary_of
parent_of
brand_of
amends
supersedes
successor_to
located_at
implements
variant_of
member_of
exposed_to
```

---

# 18. Resolution policy

Resolvers should **recommend**.

Policies should determine what can happen automatically.

Example:

```python
ResolutionPolicy(
    auto_match_threshold=0.995,
    review_threshold=0.80,

    hard_constraints=[
        registration_id_conflict,
        incompatible_birth_date,
    ],
)
```

More sophisticated policies should be possible:

```text
IF authoritative ID conflict:
    NO_MATCH

IF two independent authoritative IDs agree:
    MATCH

IF probability > .995
AND no contradictions:
    AUTO ATTACH

IF .8 < probability <= .995:
    REVIEW

ELSE:
    DEFER
```

---

# 19. Constraints

Constraints are stronger than ordinary evidence.

Example:

```text
same tax ID → strong positive evidence
different tax ID → potential hard identity contradiction
```

Other examples:

```text
incompatible species taxonomy
mutually exclusive date ranges
different immutable serial numbers
different legal-registration jurisdictions + identifiers
```

Constraint semantics must be profile-specific.

---

# 20. Resolution memory

Thisness should avoid re-solving known identity problems.

Resolution memory should support approximately four levels.

### Tier 1 — authoritative mappings

```text
(source, external_id) → entity_id
```

### Tier 2 — confirmed aliases

```text
"IBM Corp" + organization context → org_123
```

### Tier 3 — learned candidate acceleration

```text
blocking keys
vector representations
graph fingerprints
```

### Tier 4 — cached decisions

```text
input fingerprint
+ resolver version
+ policy version
→ prior decision
```

All memory must be revocable.

A split or corrected resolution must be able to invalidate dependent memory.

---

# 21. Resolution ledger

Thisness should maintain an append-only history of consequential identity operations.

Example:

```python
LedgerEvent(
    event_id="evt_...",
    event_type="MEMBERSHIP_ACCEPTED",

    observation_id="obs_101",
    entity_id="person_22",

    basis_decision_id="dec_990",

    actor="resolver:person_v2",

    timestamp=...
)
```

Possible events:

```text
OBSERVATION_INGESTED
ENTITY_CREATED
MEMBERSHIP_ACCEPTED
MEMBERSHIP_REJECTED
MEMBERSHIP_REVOKED
ENTITY_MERGED
ENTITY_SPLIT
ENTITY_RETIRED
RELATIONSHIP_CREATED
RELATIONSHIP_REVOKED
HUMAN_REVIEW_ACCEPTED
HUMAN_REVIEW_REJECTED
```

Current state should be materialized for performance.

Users should not need to replay the entire ledger for ordinary reads.

---

# 22. Merge behavior

Merge must not destroy identity history.

Conceptually:

```text
Entity A
Entity B
    ↓
merge decision
    ↓
Entity A retained
Entity B retired
Entity B → redirect → Entity A
```

Observations remain intact.

Membership history remains intact.

The operation becomes auditable and reversible.

---

# 23. Split behavior

Example:

Originally:

```text
entity_A
 ├── obs_1
 ├── obs_2
 ├── obs_3
 └── obs_4
```

After review:

```text
entity_A
 ├── obs_1
 └── obs_2

entity_B
 ├── obs_3
 └── obs_4
```

Thisness should:

1. create or activate the appropriate identity;
2. end old memberships;
3. create new memberships;
4. invalidate relevant resolution memory;
5. generate ledger events;
6. regenerate affected snapshots;
7. update candidate indexes.

---

# 24. Default Python API

The simple API should require very little knowledge of Thisness internals.

```python
from thisness import Thisness

engine = Thisness()

result = engine.resolve(
    entity_type="person",
    attributes={
        "name": "M. Khan",
        "phone": "+1 602 555 1234",
        "employer": "Acme Corp",
    },
    source={
        "type": "document",
        "id": "doc_481",
        "page": 7,
    },
)
```

Response:

```python
result.outcome
# ATTACH_TO_EXISTING

result.entity_id
# person_01K...

result.decision
# MATCH

result.evidence
# [...]
```

---

# 25. Batch API

```python
results = engine.resolve_many(
    observations,
)
```

The API should support:

```text
streaming
iterators
dataframes
Arrow
eventual async support
```

without making Pandas mandatory.

---

# 26. Advanced API

Advanced users should be able to execute each layer independently.

```python
candidates = engine.find_candidates(observation)

evidence = engine.collect_evidence(
    observation,
    candidates,
)

decisions = engine.resolve_candidates(
    observation,
    candidates,
    evidence,
)

proposal = engine.apply_policy(
    observation,
    decisions,
)

engine.commit(proposal)
```

The high-level API should internally call these exact interfaces.

There should not be a separate hidden implementation.

---

# 27. Custom profile API

Example:

```python
from thisness import ResolutionProfile
from thisness.attributes import Attribute

species = ResolutionProfile(
    name="species",

    attributes={
        "scientific_name": Attribute(...),
        "dna_marker": Attribute(...),
        "wing_pattern": Attribute(...),
        "region": Attribute(...),
    },

    candidate_generators=[
        ScientificNameSearch(),
        DNACandidateGenerator(),
        MorphologyVectorSearch(),
    ],

    evidence_providers=[
        DNAEvidence(),
        MorphologyEvidence(),
        GeographicEvidence(),
    ],

    resolver=SpeciesResolver(),

    policy=SpeciesPolicy(),
)
```

Then:

```python
engine.register(species)
```

No modification to Thisness core.

---

# 28. Plugin architecture

The following should be explicit extension points:

```text
ResolutionProfile
Normalizer
CandidateGenerator
EvidenceProvider
Resolver
Constraint
ResolutionPolicy
Canonicalizer
RelationshipDefinition
StorageAdapter
SearchAdapter
VectorAdapter
ReviewProvider
ModelProvider
```

Plugin discovery could eventually use Python entry points:

```toml
[project.entry-points."thisness.profiles"]
species = "biology_plugin:SpeciesProfile"
```

This would allow third-party packages such as:

```text
thisness-biomed
thisness-legal
thisness-code
thisness-video
```

without bloating core.

---

# 29. Built-in resolution profiles

Initial core distribution should likely include:

### Organization

Evidence examples:

```text
legal name
trade name
domain
registration ID
tax ID
address
jurisdiction
parent relationships
```

### Person

Evidence examples:

```text
name
email
phone
address
employer
DOB
external identifier
```

Longer term:

```text
Address
Product
Document
```

But V0 should resist shipping too many domains.

---

# 30. UI: Thisness Workbench

Thisness should ship with a lightweight visual inspection environment.

Working command:

```bash
thisness ui
```

or:

```bash
thisness serve
```

This launches a local application.

---

# 31. UI — entity explorer

A user should be able to select:

```text
person_01K...
```

and see:

```text
Current Snapshot

Mohammad Ali Khan
Acme Corp
...

Backing Observations

doc_123 page 4
"M. Khan"

crm_204
"Mohammad A Khan"

email_record_942
"Mohammed Khan"

Resolution History

obs_1 → matched
obs_2 → human reviewed
obs_3 → automatically matched

Relationships

works_at → Acme Corp
```

---

# 32. UI — resolution inspection

A decision view should expose:

```text
Observation
Candidate entity
Decision
Resolver
Policy
Supporting evidence
Contradictory evidence
Scores
Model versions
Source records
```

The UI should explain actual machine-readable evidence.

It should not depend on an LLM to generate an explanation.

---

# 33. UI — resolution graph

Visualize:

```text
observations
    ↓
candidate entities
    ↓
accepted identity
    ↓
relationships
```

The graph is primarily an inspection tool, not the canonical storage model.

---

# 34. UI — profile/schema explorer

This is the dbt-inspired element.

A user should be able to inspect a resolution profile visually:

```text
Organization

Attributes
 ├── name
 ├── domain
 ├── registration_id
 └── address

Candidate Generators
 ├── exact registration
 ├── alias lookup
 ├── fuzzy name
 └── embedding

Evidence Providers
 ├── legal name similarity
 ├── domain
 ├── registration ID
 └── address

Resolver
 └── HybridOrganizationResolver

Policy
 └── OrganizationPolicyV2
```

This makes the identity architecture understandable without reading source code.

---

# 35. UI — review queue

Potential view:

```text
Needs Review                          42

M. Khan
Candidate: Mohammad Ali Khan

Name similarity             +0.82
Same employer               strong support
Same city                   weak support
Phone unavailable
Conflicting middle initial  weak contradiction

[ MATCH ] [ NOT MATCH ] [ DEFER ]
```

Review decisions become ledger entries and potential training data.

---

# 36. Storage architecture

Thisness should support several modes without changing the domain model.

### In-memory

For:

```text
unit tests
examples
small scripts
```

### DuckDB

Recommended local default.

Use for:

```text
observations
entities
ledger
blocking
batch evidence computation
analytics
```

### PostgreSQL

Recommended production backend.

Use for:

```text
concurrent workloads
transactions
services
durable ledger writes
operational indexes
```

---

# 37. Optional indexes

These are extensions, not sources of truth.

Possible:

```text
OpenSearch
Elasticsearch
pgvector
FAISS
Qdrant
Pinecone
Neo4j
other graph stores
```

Canonical state remains in the Thisness persistence layer.

Indexes should be rebuildable.

---

# 38. Data architecture

Initial logical schema might include:

```text
observations
entities
entity_snapshots
memberships

candidates
evidence
resolution_decisions

relationships

review_items
review_decisions

ledger_events

resolution_memory

profile_versions
resolver_versions
```

Whether `candidates` need permanent persistence should remain configurable.

---

# 39. Async and concurrency

The engine should allow independent observations to resolve concurrently.

Each resolution job should maintain isolation through explicit IDs rather than global mutable state.

Potential abstraction:

```python
async for result in engine.resolve_stream(observations):
    ...
```

Resolvers may be:

```text
synchronous
asynchronous
remote
batch
```

The core protocols should eventually support both.

This does not need to be fully optimized in the MVP, but the object model must not prevent it.

---

# 40. LLM integration

LLMs should be optional.

Core Thisness must run without:

```text
OpenAI
Anthropic
Gemini
local model
```

LLM uses:

### Contextual evidence extraction

```text
"Acme Holdings acquired Beta Corp in 2024"
```

→

```text
relationship = acquired
subject = Acme Holdings
object = Beta Corp
```

### Ambiguous adjudication

Given:

```text
observation
candidate
evidence
contradictions
profile rules
```

return structured identity reasoning.

### Review assistance

Summarize complex evidence.

---

# 41. LLM resolver contract

An LLM should receive constrained input and produce constrained output.

Example:

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

    proposed_relationship: str | None
```

It should not be able to return arbitrary entity-state mutations.

---

# 42. Model provenance

Every consequential model execution should capture:

```text
model provider
model name
model version if available
prompt/template version
input fingerprint
schema version
execution timestamp
latency
token usage
cost where available
```

This enables drift analysis and reproducibility.

---

# 43. MCP interface

MCP should be a thin adapter over the Python API.

The initial tool set should likely be:

```text
resolve_entity
find_candidates
get_entity
search_entities
explain_resolution
get_resolution_history

propose_merge
propose_split
propose_relationship

review_resolution
```

State-changing operations should eventually be separated from proposal operations.

---

# 44. Agent safety model

An agent should be allowed to ask:

> Are these entities probably identical?

It should not automatically gain:

```text
set_entity_id
delete_entity
merge_nodes
rewrite_history
```

High-impact identity operations should support:

```text
proposal
policy evaluation
authorization
approval
commit
```

---

# 45. Evaluation framework

Evaluation should be built into Thisness rather than added later.

Metrics should operate at several levels.

### Candidate retrieval

```text
recall@K
mean candidates per observation
latency
```

### Pairwise resolution

```text
precision
recall
F1
false positive rate
false negative rate
```

### Entity/clustering

```text
B-cubed precision
B-cubed recall
cluster purity
fragmentation
```

### Identity safety

Especially:

```text
false merge rate
false split rate
largest erroneous cluster
contaminated observations
```

### Operational

```text
review rate
average review time
p50/p95 latency
throughput
cost per resolution
LLM cost
```

---

# 46. Evaluation philosophy

Automatic resolution should generally optimize for:

> **high precision before high recall.**

In most identity systems:

```text
false merge
```

is more damaging than:

```text
temporary unresolved identity
```

This should be configurable, but it is a sensible default.

---

# 47. Benchmarking

Thisness should eventually ship a standardized benchmark harness.

Use established datasets where useful.

Also build a Thisness adversarial benchmark containing:

```text
people with identical names
aliases
abbreviations
corporate hierarchies
subsidiaries
parent organizations
contract amendments
duplicate documents
temporal identity changes
relationship-vs-identity traps
OCR errors
embedding false positives
```

Third-party profiles should be able to contribute benchmark suites.

---

# 48. Security and privacy

Resolution frequently uses sensitive information.

Attributes should support metadata such as:

```python
Attribute(
    name="ssn",
    sensitivity="restricted",
    log_policy="redact",
    external_model_policy="forbidden",
)
```

Capabilities should eventually include:

```text
redaction
hashing/tokenization
PII-safe logging
per-field retention
remote-model restrictions
encrypted persistence hooks
audit trail
```

Core architecture must not require sensitive attributes to be exposed to model providers.

---

# 49. Package architecture

Initial proposal:

```text
thisness/
│
├── core/
│   ├── observation.py
│   ├── entity.py
│   ├── membership.py
│   ├── relationship.py
│   ├── evidence.py
│   ├── decision.py
│   └── scores.py
│
├── profiles/
│   ├── base.py
│   ├── person.py
│   └── organization.py
│
├── normalize/
│
├── candidates/
│   ├── exact.py
│   ├── blocking.py
│   ├── fuzzy.py
│   └── lexical.py
│
├── evidence/
│
├── resolvers/
│   ├── deterministic.py
│   ├── weighted.py
│   └── probabilistic.py
│
├── policy/
│
├── memory/
│
├── ledger/
│
├── review/
│
├── canonicalize/
│
├── storage/
│   ├── memory.py
│   ├── duckdb.py
│   └── postgres.py
│
├── eval/
│
├── integrations/
│   ├── llm/
│   ├── vector/
│   ├── search/
│   ├── graph/
│   └── mcp/
│
├── ui/
│
└── cli/
```

One package initially.

Do **not** prematurely split Thisness into many separately installed packages.

---

# 50. Dependency philosophy

Core should stay lightweight.

Possible core dependencies:

```text
Pydantic
typing_extensions where required
rapidfuzz
DuckDB optional/default local extra
```

Heavy dependencies should be optional extras:

```bash
pip install thisness[duckdb]
pip install thisness[postgres]
pip install thisness[vector]
pip install thisness[llm]
pip install thisness[mcp]
pip install thisness[ui]
pip install thisness[all]
```

Avoid forcing users to install:

```text
PyTorch
Transformers
OpenSearch
Neo4j
LLM SDKs
```

for basic resolution.

---

# 51. CLI

Basic commands should eventually include:

```bash
thisness init
thisness resolve
thisness ingest
thisness ui
thisness eval
thisness profile show
thisness history
```

Example:

```bash
thisness ui ./thisness.duckdb
```

should give someone immediate visibility into their resolution state.

---

# 52. Developer experience

A major success metric should be:

> A competent Python developer can understand the Thisness conceptual model in fifteen minutes and run a useful example in five.

README should lead with something approximately this simple:

```python
from thisness import Thisness

engine = Thisness()

result = engine.resolve(
    "IBM Corp.",
    entity_type="organization",
)

print(result.entity)
```

Then progressively reveal richer context:

```python
result = engine.resolve(
    entity_type="organization",
    attributes={
        "name": "IBM Corp.",
        "domain": "ibm.com",
    },
)
```

Then custom profiles.

---

# 53. Non-goals

Thisness V1 is **not** intended to be:

- a complete MDM platform;
- an enterprise workflow suite;
- a graph database;
- a vector database;
- a search engine;
- a full ontology platform;
- a labeling platform;
- a document extraction system;
- an LLM orchestration framework;
- a generic agent runtime;
- a data warehouse;
- a full data-quality platform.

It should integrate with those things.

It should not become them.

---

# 54. MVP scope

The MVP should prove the architecture.

### Required domain model

Implement:

```text
Observation
Entity
EntitySnapshot
Candidate
EvidenceItem
Score
ResolutionDecision
ResolutionResult
IdentityMembership
RelationshipAssertion
LedgerEvent
ResolutionProfile
```

### Required resolution capabilities

```text
normalization
exact candidate generation
blocking
fuzzy lexical candidate generation
attribute evidence
weighted resolution
hard constraints
resolution policy
```

### Required profiles

```text
Organization
Person
```

### Required persistence

```text
in-memory
DuckDB
```

### Required operations

```text
resolve
resolve_many
find_candidates
get_entity
explain_resolution
review
merge
split
history
```

### Required inspection

Minimal local UI capable of displaying:

```text
entities
observations
evidence
resolution decisions
history
```

### Required testing

```text
unit tests
profile tests
resolution regression tests
basic benchmark harness
```

---

# 55. Explicitly out of MVP

Do not block the first release on:

```text
LLM resolution
transformer models
embedding search
MCP
Neo4j
Elasticsearch
distributed execution
multi-node services
enterprise authentication
large review workflow UI
active learning
plugin marketplace
```

The interfaces should permit them.

The MVP does not need to contain them.

---

# 56. V1

Add:

```text
PostgreSQL
vector candidate generator
TF-IDF retrieval
Fellegi-Sunter resolver
calibration
resolution memory
incremental index updates
structured review queues
profile plugin discovery
better UI
MCP server
```

Potential first meaningful release:

```text
Thisness 0.1
```

could arguably include only a subset of these.

---

# 57. V2

Add:

```text
LLM evidence extraction
LLM constrained adjudication
transformer resolver
active learning
shadow-model evaluation
graph adapters
cluster-level reasoning
temporal identity
advanced review workflows
privacy-preserving matching
```

---

# 58. Success criteria

Thisness should be considered successful if it demonstrates that the same identity kernel can cleanly support very different resolution domains.

For example, without changing core engine code:

### Test 1

```text
Person resolution
```

### Test 2

```text
Organization resolution
```

### Test 3

One deliberately nontraditional domain:

```text
code functions
```

or:

```text
video actions
```

If these three can be expressed naturally through profiles and plugins, the abstraction is probably correct.

If the third requires hacking the core, the abstraction needs to be revisited.

---

# 59. Architectural invariants

These should be treated almost like constitutional rules for the project.

1. **No observation is silently destroyed by resolution.**

2. **No similarity score inherently represents identity confidence.**

3. **Every accepted identity assignment must be traceable to a decision.**

4. **Every decision must be traceable to evidence and resolver/policy versions.**

5. **Entity IDs are stable and unrelated to display names.**

6. **Relationships cannot implicitly create identity.**

7. **Merges must preserve lineage.**

8. **Splits must be possible without reconstructing raw source data.**

9. **Domain-specific semantics must live outside the core identity engine.**

10. **A model may propose identity but may not bypass policy.**

11. **Indexes are rebuildable projections, not canonical truth.**

12. **Inspection must be supported through public APIs rather than private database knowledge.**

13. **The UI consumes the same public interfaces available to external applications.**

14. **Core Thisness must function without a foundation model.**

15. **Custom entity types must not require modifying Thisness source code.**

---

# 60. The conceptual model in one diagram

```text
                      DOMAIN / RESOLUTION PROFILE
                    ┌────────────────────────────┐
                    │ attributes                 │
                    │ normalization              │
                    │ evidence semantics         │
                    │ constraints                │
                    │ policy                     │
                    └─────────────┬──────────────┘
                                  │
                                  ▼
                         ┌─────────────────┐
                         │   Observation   │
                         └────────┬────────┘
                                  │
                              normalize
                                  │
                                  ▼
                         ┌─────────────────┐
                         │   Candidates    │
                         └────────┬────────┘
                                  │
                           produce evidence
                                  │
                                  ▼
                         ┌─────────────────┐
                         │    Evidence     │
                         └────────┬────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    │             │             │
                  Rules       ML / Prob.      LLM
                    │             │             │
                    └─────────────┼─────────────┘
                                  ▼
                         ResolutionDecision
                                  │
                               policy
                                  │
                  ┌───────────────┼────────────────┐
                  │               │                │
                MATCH          REVIEW          NO MATCH
                  │
                  ▼
                         IdentityMembership
                                  │
                                  ▼
                           ┌─────────────┐
                           │   Entity    │
                           └──────┬──────┘
                                  │
                     EntitySnapshot / aliases
                                  │
               ┌──────────────────┼──────────────────┐
               │                  │                  │
             Search             Graph             Agents
               │                  │                  │
               └──────────────────┴──────────────────┘

Every material decision/state change
                │
                ▼
        ┌───────────────────┐
        │ Resolution Ledger │
        └───────────────────┘
```

---

# 61. Product identity

The one-sentence description I'd use for now is:

> **Thisness is an extensible Python identity-resolution engine that turns imperfect observations into durable, explainable, and reversible identities.**

A slightly more developer-oriented version:

> **Thisness provides the primitives for building entity resolution systems: observations, candidates, evidence, resolvers, stable identities, relationships, review, and resolution history.**

And the shortest possible conceptual explanation:

> **Thisness answers what things actually are—not merely what they look similar to.**
