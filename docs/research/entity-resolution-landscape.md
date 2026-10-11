# General-Purpose Entity Resolution Engine for Graphs, Agents, and Enterprise Data

## Foundations

A modern entity-resolution system should not be designed as “a better fuzzy matcher.” The fundamental problem is **identity under imperfect observation**: heterogeneous records, document mentions, events, graph nodes, and external identifiers provide incomplete and sometimes contradictory observations of real-world entities, and the system must decide which observations belong to the same identity without erasing the evidence that led to that conclusion. This framing is consistent with the classic record-linkage literature, modern probabilistic systems, collective entity-resolution research, and current production ER platforms. The U.S. Census Bureau, for example, describes record linkage as matching or linking records among datasets and continues to maintain methodological work spanning probabilistic and machine-learning techniques; collective-ER research explicitly formalizes the problem as discovering latent underlying entities from references that may also be relationally connected. citeturn17search8turn19search7

**The vocabulary matters because several neighboring tasks are routinely conflated.**

| Term | Best working definition for this architecture | Material distinction |
|---|---|---|
| **Normalization** | Convert an observed representation into standardized forms: case-folding, Unicode normalization, punctuation removal, address parsing, phone normalization, legal-suffix handling. | Transforms representation; makes **no identity claim**. |
| **Name matching / fuzzy matching** | Measure similarity between names or other strings. | Produces similarity evidence, not identity. |
| **Record linkage** | Determine whether records, often from different datasets, refer to the same underlying entity. | Traditionally record-centric and often pairwise. The Python Record Linkage Toolkit itself separates indexing, comparison, classification, and evaluation. citeturn3search23turn17search20 |
| **Deduplication** | Find multiple representations of the same entity, usually within one logical dataset. | Same identity problem with a within-source scope. |
| **Entity matching** | Usually the pairwise question “do A and B denote the same entity?” | Often used in ML research; Ditto, for example, casts matching as classification over pairs of serialized records. citeturn5search0turn5search8 |
| **Probabilistic record linkage** | Estimate match evidence/probability from agreements and disagreements across fields. | Fellegi-Sunter is the foundational family; contemporary systems such as Splink operationalize this approach. citeturn20search1turn20search9turn1search0 |
| **Clustering / entity resolution** | Partition many observations into sets believed to represent latent entities. | Pairwise predictions must be converted into globally coherent groups. |
| **Canonicalization** | Choose or construct preferred attributes/labels for a resolved entity. | Comes **after or alongside identity resolution**; “canonical name” and “identity” are different questions. |
| **Identity resolution** | Enterprise-oriented continuous resolution of fragmented identity evidence, commonly for people or organizations. | Usually emphasizes persistent identity across systems and time. |
| **MDM** | Governance and lifecycle management around master entities, typically including matching plus survivorship, stewardship, synchronization, hierarchy, and data quality. | ER is a component of MDM, not equivalent to all of MDM. Microsoft Customer Insights, for example, exposes distinct Map, Match, Merge, ProfileStore and Relationship stages. citeturn16search2 |
| **Coreference resolution** | Determine which mentions in discourse refer to the same discourse entity. | Context-heavy NLP problem, often document-scoped rather than enterprise identity. |
| **Entity linking** | Link a mention to an already-defined external entity or knowledge-base ID. | The target universe is generally given; open-world ER must also decide that no existing entity fits. |
| **Graph entity resolution** | Resolve identity using attributes plus graph structure/relationships. | Relational evidence can alter identity judgments collectively. citeturn19search7 |
| **Relationship resolution** | Determine how two *different* entities are related. | **Not identity resolution.** `subsidiary_of` must never accidentally become `same_entity`. |

The cleanest conceptual decomposition is therefore:

\[
\text{representation normalization}
\neq
\text{similarity}
\neq
\text{identity evidence}
\neq
\text{identity decision}
\neq
\text{canonicalization}
\neq
\text{relationship inference}.
\]

That separation should be reflected directly in the Python object model rather than hidden inside a monolithic `match()` function.

A general-purpose engine must specifically distinguish the six operations in the prompt:

**String normalization.** `"IBM Corp." → "ibm corp"` is a deterministic representation transform.

**Pairwise identity reasoning.** `"IBM Corp."` and an existing entity are compared using names, identifiers, addresses, context, relationships, and other evidence.

**Clustering.** Many observations are assigned to a stable latent entity.

**External linking.** The internal entity may separately map to, for example, a company registry, Wikidata entity, LEI, supplier master, or customer master.

**Relationship resolution.** IBM and a subsidiary may be confidently related without being identical.

**Temporal identity maintenance.** Evidence arriving tomorrow may support, contradict, merge, or split yesterday's assignments.

This distinction is particularly important because collective entity resolution has long shown that relational information can improve disambiguation: two ambiguous references can become distinguishable from the entities around them. But collective ER also introduces dependencies among decisions, which makes unrestricted transitive inference dangerous. The classical work by Bhattacharya and Getoor explicitly treats ER as relational clustering in which attribute and neighborhood evidence interact. citeturn19search7

**The methodological landscape supports a layered rather than single-model design.**

| Approach | Strongest use | Principal advantages | Principal weaknesses / failure modes | Labels, auditability, incremental suitability |
|---|---|---|---|---|
| Exact deterministic rules | Authoritative IDs, emails, registration numbers, composite business rules | Fast, predictable, highly explainable | Brittle to missing/corrupt identifiers; shared/recycled identifiers can cause catastrophic merges | No labels; excellent auditability; excellent incremental operation |
| Normalized exact matching | Names, phones, addresses after canonical formatting | Very cheap and reproducible | Normalization can destroy discriminative distinctions | No labels; excellent auditability |
| Edit/Jaro/Jaro-Winkler/character similarity | Typographical corruption, OCR, short names | Cheap, transparent | Similar strings need not be identical entities; poor semantic/contextual understanding | No labels; strong auditability |
| Token/Jaccard/TF-IDF/sparse similarity | Company/product names and multi-token fields | Handles token reordering and common-word weighting | Weak on aliases with different vocabularies; field-specific tuning required | No labels; strong auditability |
| Phonetic matching | Person names and transliterations in appropriate languages | Finds spelling variants based on pronunciation | Language-dependent and collision-prone | No labels; strong auditability |
| Fellegi-Sunter probabilistic linkage | Structured multi-field records | Interpretable evidence weighting; excellent baseline where labels are limited | Independence assumptions are imperfect; calibration depends on candidate population/model assumptions | Can be unsupervised/weakly supervised; very auditable. Splink operationalizes this family at scale. citeturn20search9turn1search0 |
| Logistic regression / gradient boosting / traditional ML | Structured heterogeneous features | Strong accuracy with engineered evidence; inexpensive inference | Requires representative labels; scores can drift; feature interactions can obscure explanations | Good production choice if calibrated and versioned |
| Active-learning classifiers | Domains where labels are expensive | Focuses human labeling on informative cases | Labeling bias and changing distributions must be managed | Dedupe explicitly uses ML for structured ER and is associated with interactive training workflows. citeturn3search24turn3search0 |
| Siamese / contrastive neural models | Learned representations and retrieval | Can learn domain-specific similarity and scale candidate retrieval | Similarity objective does not automatically equal identity; training data matters heavily | Moderate explainability |
| Transformer pair models | Rich records with textual fields | Strong contextual comparison; minimal manual feature engineering | Expensive relative to classical methods; labeled-data/domain-shift issues | Ditto showed large gains across standard entity-matching benchmarks using pretrained Transformers. citeturn5search0 |
| Dense embeddings | Candidate retrieval, multilingual/noisy descriptions | Efficient ANN retrieval; captures semantic variation | Semantic relatedness is **not identity**; false positives among related brands/subsidiaries/categories | Excellent candidate layer, weak standalone truth layer |
| Graph/collective ER | Bibliographic, fraud, organizational, social, supply-chain contexts | Relationship structure can disambiguate sparse attributes | Feedback loops and erroneous propagation can amplify initial mistakes | Powerful but must preserve provenance and constrain propagation. citeturn19search7 |
| LLM-assisted comparison | Ambiguous context, unstructured descriptions, organizational relationships | Can reason over heterogeneous evidence and interpret context | Hallucination, nondeterminism, privacy, cost, model drift | Useful adjudicator/extractor if constrained; poor autonomous source of truth |
| Human review | High-risk and ambiguous cases | Handles domain nuance and provides labels | Expensive and variable | Essential architectural component rather than an exceptional fallback |

The classical Fellegi-Sunter idea remains more architecturally important than its age suggests. Instead of collapsing every field into a generic fuzzy score, it treats agreements and disagreements as evidence with different information value. Splink exposes this logic explicitly through field comparison levels and match weights. citeturn20search9turn20search32 This is exactly the right intellectual direction for a new engine: **a rare tax-ID agreement and a common-name agreement must not be treated as interchangeable “90% similarities.”**

Neural approaches do not invalidate that principle. Ditto demonstrated that pretrained Transformers can perform very strong pairwise entity matching by serializing structured records into text and fine-tuning sequence classifiers, reporting substantial gains over then-existing methods across its benchmark suite. citeturn5search0turn5search4 The lesson is not “replace evidence with Transformers”; it is that a `Resolver` can legitimately be a learned model while the surrounding system retains candidate provenance, typed evidence, decision history, and reversible identity state.

Recent LLM work pushes farther. LLM-CER investigates resolving records at the cluster level rather than merely issuing pairwise calls, reporting higher F-measure in parts of its evaluation and fewer API calls while explicitly encountering issues such as cluster merging and hallucination. citeturn5search3turn5search15 DistillER studies using LLMs as teachers and distilling their judgments into cheaper models, showing one plausible way that expensive language-model reasoning can help bootstrap a domain matcher without making the online system permanently dependent on a frontier model. citeturn5academia35 These results support a **model-assisted, model-independent architecture**.

Your existing workflow—

`raw entities → normalization → constrained model reconciliation → embedding grouping → agent review → canonical entity`

—therefore has strong instincts, but should be decomposed more rigorously. Normalization should remain. Embeddings should become explicitly **retrieval/candidate evidence**. Constrained model reconciliation should become one resolver among several. Agent review should write a versioned review decision rather than directly mutate canonical records. The canonical entity should be produced from accepted identity memberships, while every raw observation and decision remains addressable.

## Current landscape

The strongest existing tools tend to optimize one portion of the problem rather than solve the entire persistent-identity lifecycle.

**Splink is the best architectural reference for probabilistic, SQL-native linkage.** It is explicitly a probabilistic record-linkage/entity-resolution library designed for deduplication and linkage where unique identifiers are unavailable. Its documentation exposes Fellegi-Sunter concepts, term-frequency adjustments, deterministic and probabilistic linkage, blocking, and graph-oriented interpretation of linked results. citeturn1search0turn1search28 The idea worth borrowing is not Splink's exact API but the transparency of **comparison levels → evidence weights → pair scores**, together with database-executed blocking and comparison. Splink is less naturally a persistent domain-object/ledger framework for arbitrary graph and agent workflows; the new library should therefore treat probabilistic linkage as a resolver backend rather than make Fellegi-Sunter its entire ontology.

**Dedupe is a strong reference for interactive learning.** Its official documentation describes machine-learning-based deduplication and ER over structured data, including names and addresses and linking records across datasets. citeturn3search24 The most reusable idea is that human labeling can train the matcher rather than merely correct the output. The new architecture should generalize this into a first-class `ReviewDecision → TrainingExample` path.

**Zingg contributes scalability and incremental concepts.** Its project exposes Spark-based entity resolution, configurable matching types, blocking strategies, deterministic rules, and incremental operation. citeturn3search9 What to borrow is the notion that entity resolution is a pipeline that must support both bulk and incremental modes under one conceptual model; what not to inherit is a mandatory distributed-compute footprint for local users.

**The Python Record Linkage Toolkit is an excellent decomposition reference.** It explicitly separates preprocessing, indexing, comparison, classification and evaluation, but its own repository characterizes it as intended primarily for research and small/medium files. citeturn3search23turn17search20 Its pipeline separation should survive into the new library, while persistence, evidence provenance, type plugins, review, incremental identity and graph integration should go substantially farther.

**Transformer systems such as Ditto show what a learned resolver can look like without becoming the system architecture.** Ditto converts record pairs into sequences and fine-tunes pretrained language models, with optional domain knowledge, summarization, and augmentation. citeturn5search0turn5search8 A modern engine should be able to plug in such a resolver while preserving the exact record pair, model identifier, serialization/version, returned score and ultimate policy decision.

**AWS Entity Resolution illustrates a production separation between workflow, matching technique and output identity.** AWS supports rule-based, ML-based and provider-service-based workflows and assigns matched groups `MatchID`s. Its rule workflows can expose which rule generated a match, and its ML mode emits group-level—and in incremental ML operation, record-level—confidence values. AWS also supports batch/incremental workflows and certain hashed-data workflows. citeturn15search9turn15search16turn15search23turn15search30 The reusable ideas are typed matcher strategies, generated identity groups, incremental processing and explicit match provenance. The opportunity for an open-source system is substantially deeper evidence inspection, pluggable algorithms, reversible identity history and deployment independence.

**Microsoft Customer Insights makes another useful pipeline distinction.** Its published workflow diagnostics list ingestion, preparation, mapping, matching, merging, profile storage and relationship processing as separate operations. citeturn16search2 This reinforces the proposed architectural rule that **matching is not merging** and that relationships are not equivalent to identity.

**Tamr is useful as an MDM-style comparator because its public materials explicitly combine ML clustering, human feedback, consolidation into a golden record and real-time matching.** Its documentation describes a clustering approach involving machine learning and linkage as well as curator feedback, while its domain configuration covers schema, cleaning/enrichment, clustering and golden-record consolidation rules. citeturn12search22turn12search27 The lesson is to make canonicalization/survivorship a separate layer from identity. Reltio, Informatica, Semarchy and Ataccama belong to the broader MDM comparator set; for an open-source ER substrate, their most relevant conceptual category is source-to-master governance, not a need to reproduce full MDM workflow, UI, stewardship and synchronization machinery.

**Senzing emphasizes a somewhat different pattern: persistent identity and relationships rather than a batch-only merge job.** Its current public description emphasizes real-time identity resolution, relationship awareness and explanations, although these claims should be treated as vendor descriptions rather than independent algorithmic evidence. citeturn22search0 Architecturally, this supports the idea of ER as a continuously maintained identity service rather than a one-shot dataframe transformation.

**Quantexa likewise frames ER as the conversion of disparate records into distinct entities and places significant weight on contextual/connected-data use cases.** Again, this is a vendor characterization rather than an open algorithm specification. citeturn22search31 The transferable idea is that graph context belongs in the evidence space; the engine should not, however, require the entire investigation/decision platform around it.

**Elasticsearch/OpenSearch-style search infrastructure belongs primarily in candidate generation.** Elasticsearch's text-analysis machinery exists to retrieve relevant full-text matches rather than only exact strings, which is exactly what candidate search needs. citeturn18search13 Fuzzy lexical indexes, n-grams, phonetic analyzers and vector ANN can cheaply narrow millions of possible entities to tens of plausible ones. They should not own canonical identity.

**Neo4j and graph databases are similarly better treated as graph substrates than as the sole resolution engine.** Neo4j's public ER material emphasizes using relationships and contextual information in addition to attributes. citeturn17search19turn22search12 Graph algorithms are highly useful for connected components, relational evidence and cluster inspection, but identity policy should live above the graph so a dubious similarity edge does not automatically become irreversible node collapse.

**Databricks, Snowflake and Google Cloud are most useful as execution environments or sources of similarity primitives when a dedicated ER layer is absent.** Google BigQuery, for example, exposes embedding similarity primitives, but similarity computation is not itself an identity-management architecture. citeturn3search22 Warehouse-native SQL is nevertheless attractive for blocking, feature computation and large pairwise joins. A portable engine should be able to compile portions of its candidate/evidence workload to SQL without tying the domain model to one warehouse.

The biggest architectural gap across this landscape is not another string matcher. It is an open, persistent layer combining:

`observations + stable entities + candidate retrieval + typed evidence + multiple resolvers + policy + review + reversible identity + ledger + graph interfaces`.

### The Palantir pattern

Palantir requires unusually careful treatment because its publicly visible architecture is broader than entity resolution itself and much of the ER implementation is not documented at an algorithmic level.

**Documented behavior:** Palantir describes its Ontology as an operational/semantic layer sitting over integrated digital assets such as datasets, tables and models, mapping them to real-world objects, properties and links. It combines semantic objects/properties/links with operational elements such as actions and functions. citeturn8view1 That is strong evidence for a logical separation between **source/integrated data** and **semantic real-world objects**, although it is not proof that Palantir stores ER observations in precisely the data model proposed here.

**Documented behavior:** Ontology objects have object-type identity and primary keys, and Palantir APIs expose object primary keys and object RIDs. citeturn6search9turn6search16 This is an important pattern: semantic objects need stable identifiers that are not merely display names.

**Documented behavior:** Palantir supports object types backed by multiple data sources, with mappings around object primary keys; its documentation distinguishes column-wise multi-datasource composition and other datasource patterns. citeturn9view0 This demonstrates that a single semantic object can be assembled from multiple physical sources once identity/keying is established. It should **not** be interpreted as proof that the multi-datasource mapping itself performs probabilistic entity resolution.

**Documented behavior:** Palantir separately models links between objects. Foreign-key-backed and object-backed links represent relationships among semantic objects. citeturn10view3 This supports the principle that ontology relationships are first-class and distinct from object identity.

**Documented behavior:** Palantir also maintains detailed edit history and an action log. The action log can capture who made a decision, when it occurred, the action version and affected object primary keys, while edit history captures object changes; action logs can be linked to edited objects. citeturn10view0turn10view1 Action reversion exists for supported actions, subject to important limitations such as subsequent edits and non-revertible side effects. citeturn10view2 These are highly relevant analogues for a resolution ledger even though they are not documentation of Palantir's internal ER ledger.

**Documented behavior:** Palantir publicly markets Foundry Entity Resolution as a capability to link records into a reliable deduplicated foundation. citeturn22search1 Public documentation available in this research does **not** specify enough internal detail to establish the exact probabilistic models, cluster algorithms, confidence representation, raw-record lifecycle, or generic merge/unmerge implementation.

**Reasonable architectural inference:** Foundry's separation of integrated source assets from Ontology objects, combined with stable object identities and provenance/history mechanisms, strongly suggests a design philosophy in which semantic entities are not merely destructive rewrites of source rows. citeturn8view1turn10view0turn10view1 This is an inference about architectural philosophy, not evidence of an undocumented internal schema.

**Speculation that should be avoided:** It would be unjustified to assert from public sources that Palantir internally implements the exact `Observation → ResolutionAssertion → Entity` model proposed here, that every raw ER observation is immutable forever, or that arbitrary ER merges can always be unmerged. The public sources examined do not establish those facts.

The Palantir-inspired pattern worth adapting is therefore much smaller than “open-source Palantir”:

\[
\text{source observations}
\rightarrow
\text{resolution/evidence layer}
\rightarrow
\text{stable semantic entities}
\rightarrow
\text{typed relationships and actions}
\]

with provenance and decision history connecting every layer.

That answers the most important Palantir question: **yes, raw observations should generally remain first-class and canonical entities should exist separately above them.** The reason is not imitation of Palantir; it follows directly from reversibility, provenance, incremental resolution, differing survivorship policies and graph safety. But “immutable forever” needs one qualification: immutability should be a *logical identity/audit rule*, while retention/privacy policies must be allowed to redact or delete sensitive payloads. The ledger can retain a tombstone, cryptographic reference or minimal provenance when policy permits rather than violating deletion requirements.

## Architectural principles

The fourteen hypotheses in the prompt are mostly correct, but several need tightening.

**Raw observations should never disappear — accept, with a privacy qualification.** An accepted match must not replace its source observation. Otherwise a later split becomes forensic reconstruction. Treat corrections as new observation versions or superseding source events rather than silently modifying the historical record. This mirrors the broader lineage principle visible in Palantir's separation of ontology objects from backing data and its explicit edit/action history. citeturn8view1turn10view0turn10view1 Physical retention remains policy-controlled.

**Canonical entities should have stable IDs independent of source names — strongly accept.** Palantir's object model likewise separates primary identity from display properties. citeturn6search9turn6search16 Names, addresses, supplier codes and domains change; the library's entity ID should not.

**Entity resolution should generate explicit evidence — strongly accept.** Splink demonstrates the value of decomposing a match into comparison levels and weights; AWS rule workflows similarly retain the rule responsible for a matched group. citeturn20search9turn15search16 A score without its inputs is inadequate for an audit-grade system.

**Candidate generation and final decisioning should be separate — make this fundamental.** The Record Linkage Toolkit already separates indexing from comparison/classification, while production systems employ blocking/search precisely because an all-pairs comparison is quadratic. citeturn3search23turn20search17 Candidate recall and decision precision are separate optimization problems and should have separate APIs and metrics.

**Embeddings are primarily candidate-generation tools, not truth engines — accept with one nuance.** An embedding similarity may legitimately become **one evidence feature** inside a calibrated domain-specific decision model. It should rarely be a sufficient identity rule on its own.

The dangerous distinction is:

\[
\text{semantic similarity} \not\Rightarrow \text{referential identity}.
\]

“Apple,” “Apple Records,” “Apple Bank” and “Apple Inc.” can occupy related semantic neighborhoods while denoting separate organizations. The same applies to parent/subsidiary entities, adjacent product models and two people with very similar biographies. Embeddings are especially valuable when candidate retrieval must bridge multilingual labels, lexical variation, descriptions or aliases that exact indexes miss. They are less trustworthy for short proper names whose main task is discrimination rather than semantic meaning.

**LLMs should adjudicate ambiguous evidence under constraints rather than issue unconstrained merges — strongly accept.** Recent LLM-ER research shows real promise but also explicitly encounters cluster-merging and hallucination problems, while distillation research suggests LLM knowledge can sometimes be transferred into smaller repeatable models. citeturn5search3turn5academia35 The right interface is therefore not:

```text
"Are these the same company? Explain."
```

but structurally closer to:

```text
Given:
  observation
  candidate entity snapshot
  evidence items E1..En
  permitted identity rules
  permitted relationship types
  contradiction rules

Return:
  decision enum
  referenced_evidence_ids
  contradictions
  proposed_relation_type | null
  structured_reason_codes
```

An LLM-generated paragraph can be saved as an auxiliary explanation, but the **decision must cite machine-readable evidence**.

**Human review is part of the architecture — strongly accept.** The decisive insight from active-learning systems such as Dedupe is that reviewer effort can simultaneously resolve difficult cases and generate training information. citeturn3search24 Review should be triggered not only by “confidence around 0.5” but also by risk: a proposed merge joining two large entities, a bridge between previously distinct graph communities, conflicting authoritative identifiers, or an identity decision with large downstream blast radius may deserve review even at a superficially high score.

**Resolution decisions should be versioned and auditable — strongly accept.** This should extend beyond `resolver_version`. Evidence extraction, normalization, blocking configuration, type schema, model parameters, prompt template and policy version can all change a result.

**Identity and relationship should not be conflated — make this a hard invariant.** Collective resolution benefits from relationship evidence, but relational similarity is not itself identity. citeturn19search7 `same_entity`, `subsidiary_of`, `division_of`, `brand_of`, `operated_by`, `located_at`, `formerly_known_as`, `successor_to` and `related_to` need distinct semantics.

**The system must support merging and unmerging — accept, but do not implement merge as destructive union.** An “unmerge” becomes easy only when underlying observations were never destroyed. Prefer membership changes and entity lineage events over physical data collapse.

**Previously resolved identity should form reusable resolution memory — accept conditionally.** Resolution memory should accelerate future candidate retrieval and obvious decisions, but every memory entry must carry provenance, scope, validity and revocation state. A learned alias should not become an eternal deterministic truth merely because one analyst approved it in one context.

**The core engine should be model-agnostic — strongly accept.** Classical probabilistic models remain strong; Transformers offer powerful pairwise matching; LLMs add contextual reasoning; rules dominate when authoritative identifiers exist. citeturn1search0turn5search0turn5search3 The architecture should allow all of them simultaneously.

**Entity-type-specific behavior should be extensible — strongly accept.** There is no meaningful universal similarity function over a person, physical facility and manufactured product. What is universal is the *protocol*: candidate retrieval, evidence production, constraints, decision, ledger and review.

**Local-first but enterprise-scalable — strongly accept.** Splink and Record Linkage Toolkit demonstrate the value of accessible Python/SQL workflows, while Spark-oriented Zingg and managed AWS ER demonstrate the need to scale farther. citeturn1search0turn17search20turn3search9turn15search9 The package should not make distributed infrastructure part of the core abstraction.

### Evidence should be data, not prose

The central domain object missing from many ER APIs is an explicit immutable `EvidenceItem`.

A useful evidence record is:

```python
EvidenceItem(
    evidence_id="ev_...",
    kind="normalized_name_similarity",
    subject_observation_id="obs_...",
    candidate_entity_id="org_1234",
    field="name",
    value=0.94,
    direction="supports_match",
    strength="moderate",
    inputs={
        "left": "international business machines",
        "right": "international business machines corporation",
    },
    method="token_jaccard",
    method_version="2.1.0",
    produced_by="lexical-evidence-provider",
    produced_at=...,
    source_refs=[...],
)
```

Other evidence might be `authoritative_identifier_equal`, `email_domain_equal`, `address_conflict`, `phonetic_name_match`, `graph_neighbor_overlap`, `embedding_cosine`, `llm_relation_extraction`, or `human_attestation`.

Two rules matter.

First, **positive and negative evidence must be equally expressible**. A shared name supports identity; conflicting legal registration IDs may strongly oppose it.

Second, **retrieval scores must not masquerade as identity probabilities**. Elasticsearch relevance, cosine similarity, graph proximity and an LLM's self-reported confidence are different quantities. The evidence model should retain the score's type and semantics.

Splink's explicit comparison-level weighting is a useful precedent for decomposed evidence rather than opaque whole-record similarity. citeturn20search9 AWS's distinction between rule identifiers and ML confidence similarly illustrates that different decision mechanisms expose different evidence semantics. citeturn15search16turn15search23

### Identity should be constrained reasoning, not binary similarity

A resolver should operate over:

\[
O,\ C,\ E,\ T,\ P,\ G
\rightarrow D
\]

where:

- \(O\) is the observation;
- \(C\) is a candidate entity snapshot;
- \(E\) is typed evidence;
- \(T\) is entity-type configuration;
- \(P\) is policy/constraints;
- \(G\) is optional graph context;
- \(D\) is a typed decision.

The internal decision vocabulary should include at least:

`MATCH`, `NO_MATCH`, `POSSIBLE_MATCH`, `REQUIRES_REVIEW`.

`NEW_ENTITY` is best treated as an **orchestration outcome** after no candidate is accepted rather than as the result of one observation-candidate pair. `RELATED_ENTITY` should likewise become a separate `RelationshipAssertion` rather than overloading an identity decision.

That leaves a cleaner model:

```text
IdentityDecision:
    MATCH | NO_MATCH | POSSIBLE_MATCH | REQUIRES_REVIEW

RelationshipDecision:
    relation_type + subject + object + evidence

ResolutionOutcome:
    ATTACH_TO_EXISTING
    CREATE_ENTITY
    DEFER
```

This small distinction prevents a surprisingly large class of architecture errors.

### Confidence should be typed

Do not expose one generic `confidence: float` and imply universal comparability.

Instead:

```python
Score(
    kind="calibrated_match_probability",
    value=0.997,
    calibration_id="org-v4-isotonic",
)

Score(
    kind="embedding_cosine",
    value=0.91,
    model="...",
)

Score(
    kind="fellegi_sunter_weight",
    value=13.4,
    model="...",
)

Score(
    kind="rule_certainty",
    value="authoritative_identifier_exact",
)

Score(
    kind="human_decision",
    value="approved",
)
```

AWS's own rule and ML modes illustrate why this distinction is natural: rule-based results carry matching-rule provenance, while ML matching produces confidence values. citeturn15search16turn15search23

Where probabilities are advertised as probabilities, calibration should be evaluated. Where models merely emit ranking scores, the system should say so. Conflicting evidence should remain visible rather than disappearing into an average.

## Proposed system architecture

The proposed `Observation → Entity → ResolutionAssertion` abstraction is fundamentally strong, but it needs additional objects to remain reversible and scalable.

The recommended domain model is:

```text
SourceRecord / Observation
        │
        ├── normalization artifacts
        │
        ├── Candidate ──────────────┐
        │                           │
        ├── EvidenceItem(s)         │
        │                           ▼
        └────────────────── ResolutionDecision
                                    │
                          accepted / reviewed
                                    │
                                    ▼
                           IdentityMembership
                                    │
                                    ▼
Entity ───── EntityVersion / EntitySnapshot
  │
  ├── AliasAssertion
  ├── ExternalIdentifierAssertion
  ├── RelationshipAssertion
  └── EntityLineageEvent

all material state changes
        │
        ▼
ResolutionLedger
```

**Observation.** A discrete claim from a source or document occurrence. It should include `observation_id`, `entity_type`, source locator, extraction timestamp, raw attributes, normalized attributes or references thereto, context, effective time if known, and source version/hash. Observations are logically immutable.

For documents, `"IBM Corp"` appearing in paragraph 14 of `document_183` is an observation even when the same document mentions IBM five more times. Whether mention-level observations are subsequently grouped into a document-local mention entity is a configurable preprocessing choice.

**Entity.** A stable system identity:

```python
Entity(
    entity_id="org_01J...",
    entity_type="organization",
    state="active",
)
```

It should be intentionally small. Avoid making it a giant mutable golden record.

**EntitySnapshot / EntityVersion.** The current materialized view of preferred/canonical properties derived from accepted observations, explicit edits and survivorship rules:

```python
EntitySnapshot(
    entity_id="org_01J...",
    version=17,
    canonical={
        "name": "International Business Machines Corporation",
        "country": "US",
    },
    derived_from=[...],
    policy_version="org-survivorship-v3",
)
```

Canonical names should therefore be mutable while entity IDs remain stable.

**Candidate.** A retrieval result, not a decision:

```python
Candidate(
    observation_id="obs_...",
    entity_id="org_...",
    generator="lexical_company_name",
    generator_version="1.4",
    rank=3,
    retrieval_score=7.22,
    retrieval_score_kind="bm25",
    retrieval_evidence={...},
)
```

Persisting candidates is optional; decisions and evidence are not.

**EvidenceItem.** An immutable computed or asserted fact relevant to matching. Its payload must include method/version and input references.

**ResolutionDecision.** The immutable outcome produced by a resolver/policy at a point in time:

```python
ResolutionDecision(
    decision_id="dec_...",
    observation_id="obs_...",
    candidate_entity_id="org_...",
    decision="MATCH",
    scores=[...],
    evidence_ids=[...],
    resolver="hybrid_policy",
    resolver_version="3.2",
    policy_version="organization-v5",
    created_at=...,
)
```

The prompt's `ResolutionAssertion` is therefore retained but narrowed: it records **a decision**, not mutable truth.

**IdentityMembership.** The current accepted mapping between an observation and an entity. This should be derived from ledger events or itself versioned:

```text
obs_991 → org_1234
valid_from = ...
valid_to = null
basis_decision_id = dec_...
```

This separation is important because a decision is historical; membership is current state.

**AliasAssertion.** Avoid storing `aliases=[...]` as an unexplained mutable array. An alias should have a source:

```text
"IBM" --alias_of--> org_1234
basis = observations [...]
scope = organization
locale = en
validity = [...]
```

Aliases can then be revoked without rewriting entity history.

**ExternalIdentifierAssertion.** Map external namespaces independently:

```text
(org_1234, namespace="LEI", value="...", source=...)
```

A canonical external source can thus be linked without making its identifier the engine's internal identity.

**RelationshipAssertion.** Represents `subsidiary_of`, `division_of`, `brand_of`, `formerly_known_as`, `located_at`, and arbitrary custom relationships. Relationship assertions carry evidence, provenance, confidence/decision semantics, and temporal validity.

**EntityLineageEvent.** Records entity-level operations such as `MERGED`, `SPLIT`, `RETIRED`, `REDIRECTED`, or `REACTIVATED`.

### Candidate generation

Candidate generation should be a hard architectural boundary.

```text
Observation
   │
   ├─ exact-key lookup
   ├─ alias memory lookup
   ├─ lexical / fuzzy search
   ├─ phonetic index
   ├─ blocking keys
   ├─ sparse TF-IDF
   ├─ embedding ANN
   ├─ graph-neighbor lookup
   └─ external canonical index
            │
            ▼
      candidate union
            │
      dedupe + ranking
            ▼
       top-K candidates
```

The indexing/comparison separation is well established in record-linkage tooling. citeturn3search23 In practical large-scale linkage, blocking exists because comparing every record with every other record becomes infeasible, and modern Fellegi-Sunter workflows similarly rely on blocking before scoring. citeturn20search17

A candidate generator protocol should therefore expose **recall-oriented retrieval**, not identity:

```python
class CandidateGenerator(Protocol):
    def generate(
        self,
        observation: Observation,
        *,
        entity_type: EntityType,
        limit: int,
        context: ResolutionContext,
    ) -> Iterable[Candidate]:
        ...
```

Multiple generators should run in parallel and union their results. This means `"International Business Machines"` could enter through exact normalized alias lookup, `"IBM Corp"` through lexical search, and a multilingual description through embeddings—all before one resolver judges identity.

Candidate-generation quality gets its own first-class metric: **candidate recall@K**. A perfect resolver cannot recover a true entity that candidate generation never surfaced.

### Resolution and policy

A resolver should ideally be side-effect-free:

```python
class Resolver(Protocol):
    def resolve(
        self,
        observation: Observation,
        candidate: EntitySnapshot,
        evidence: Sequence[EvidenceItem],
        context: ResolutionContext,
    ) -> ResolutionDecision:
        ...
```

Implementations can include:

```text
DeterministicResolver
FellegiSunterResolver
SklearnResolver
GradientBoostingResolver
TransformerResolver
LLMAdjudicator
HumanResolver
EnsembleResolver
```

A **policy layer** should sit above model scores. This is where domain constraints live:

```text
IF authoritative_registration_id conflicts:
    NO_MATCH

ELSE IF same_registration_id AND jurisdiction_equal:
    MATCH

ELSE IF calibrated_probability > 0.999
        AND no hard_conflicts
        AND merge_risk < threshold:
    MATCH

ELSE IF candidate_relation == subsidiary_of:
    NO_MATCH identity
    propose subsidiary_of relationship

ELSE:
    REQUIRES_REVIEW
```

This is a major improvement over letting each model invent its own state-changing threshold.

### Entity-type plugins

Generic infrastructure should own the protocol; plugins should own semantic details.

```python
class EntityTypePlugin(Protocol):
    name: str

    def normalize(self, observation): ...
    def blocking_keys(self, observation): ...
    def candidate_generators(self): ...
    def evidence_providers(self): ...
    def hard_constraints(self): ...
    def relation_types(self): ...
    def canonicalization_policy(self): ...
    def decision_policy(self): ...
```

Typical evidence differs materially:

| Entity type | Particularly useful evidence | Dangerous shortcuts |
|---|---|---|
| Person | full/parsed name, DOB, email, phones, address history, authoritative ID, household/relationship context | shared address, common surname, recycled phone |
| Organization/company | registration IDs, domain ownership, legal name, addresses, jurisdiction, hierarchy, officers | same brand, shared office, parent/subsidiary name |
| Supplier/customer | source-system account IDs, tax IDs, legal entity, remit-to addresses, ERP relationships | identical salesperson-entered name |
| Location/address | parsed address components, geocodes, postal identifiers, spatial proximity | nearby coordinates alone |
| Product | manufacturer, GTIN/SKU/MPN, specifications, model family, dimensions | semantic description similarity alone |
| Facility/asset | serial identifiers, operator, physical location, asset class | co-location |
| Document | checksums, source IDs, titles/authors, content fingerprint | topical similarity |
| Arbitrary custom type | schema-declared identity fields and constraints | generic global score |

This is where the proposed engine becomes genuinely general-purpose rather than “person dedupe with renamed classes.”

### Embeddings

The hypothesis that embeddings belong primarily in candidate generation is **correct**.

Embedding retrieval is especially attractive for:

- multilingual aliases;
- long product descriptions;
- company descriptions whose formal names vary substantially;
- text extracted noisily from documents;
- candidate recovery where token overlap is weak;
- review-cluster discovery.

It is dangerous when **relatedness and identity are easily confused**. An embedding model may correctly infer that a parent corporation, subsidiary and brand are strongly related while failing the system's actual identity criterion. The problem is conceptual, not merely threshold tuning.

Therefore:

```text
embedding similarity
      │
      ├── candidate retrieval: YES
      ├── review-group discovery: YES
      ├── weak evidence feature: YES
      └── sole automatic merge criterion: GENERALLY NO
```

A domain-specific embedding/contrastive model trained explicitly on same-entity examples can carry more decision weight, but its score should still flow through ordinary evidence and policy.

### LLMs

An LLM belongs in four places.

**Evidence extraction:** turn messy context into structured claims: legal name, corporate relationship, former name, address, date range.

**Ambiguous candidate adjudication:** reason over a small candidate set and explicit evidence.

**Relationship classification:** distinguish “same company” from “subsidiary,” “division,” “brand,” “successor” or “unrelated.”

**Review assistance:** summarize why a cluster looks coherent or identify anomalous cluster members.

Recent work supports both pairwise and cluster-level LLM ER while also exposing hallucination and cluster-merging risks. citeturn5search3turn5search15 Distillation results further suggest that an LLM may be valuable as an offline teacher rather than a permanent online dependency. citeturn5academia35

LLMs should **not** directly:

```text
write membership rows
merge canonical entities
invent external identifiers
override authoritative contradictions
silently update aliases
```

Every LLM execution used in a consequential decision should capture model/provider/version, prompt/template version, structured inputs or reproducible input references, output schema version, evidence IDs and cost/latency metadata.

### Knowledge-graph integration

The canonical knowledge graph should not be the only place where resolution happens. A safer graph pattern is:

```text
(Source Observation A) ──observes──► (Canonical Entity X)
(Source Observation B) ──observes──► (Canonical Entity X)

(Canonical Entity X) ──subsidiary_of──► (Canonical Entity Y)

(Observation C) ──possible_identity──► (Entity X)
                 score / evidence / provenance

Resolution ledger
       │
       └── supports accepted membership and relationship assertions
```

There should be three different semantics:

**Accepted identity membership.** Observation belongs to entity.

**Probabilistic identity hypothesis.** Observation or entity may match another entity.

**Ordinary domain relationship.** Two distinct entities are related.

Do **not** treat all pairwise matches above threshold as `same_as` edges and then take unrestricted connected components. Pairwise similarity is not mathematically guaranteed to be transitive, and one bad bridge can join two otherwise clean clusters. Collective ER research shows relational evidence can improve resolution, but also makes decisions mutually dependent. citeturn19search7

For the IBM example, graph data should permit:

```text
International Business Machines Corporation
    aliases: IBM, IBM Corp.
    external_ids: ...

IBM subsidiary A
    subsidiary_of → IBM

IBM brand B
    brand_of → IBM
```

rather than collapsing every node whose name contains or semantically resembles “IBM.”

Temporal semantics should also be explicit:

```text
formerly_known_as
valid_from / valid_to

subsidiary_of
valid_from / valid_to

observation membership
observed_at / effective_at
```

A historical corporate entity may be a predecessor rather than the same current legal entity.

### Incremental identity and resolution memory

On arrival of a new observation:

```text
ingest
 → normalize
 → check exact resolution memory
 → generate candidates
 → compute evidence
 → apply hard constraints
 → run resolver/policy
 → attach / create / review
 → append ledger event
 → update entity snapshot + indexes
 → optionally reevaluate affected local neighborhood
```

Do not globally recluster the universe every time one observation arrives.

Resolution memory should have tiers:

```text
Tier A: authoritative mappings
(source_namespace, source_id) → entity_id

Tier B: accepted deterministic aliases/keys
(normalized alias + scope) → candidate entity/entities

Tier C: learned candidate accelerators
blocking keys / embeddings / graph signatures

Tier D: cached model decisions
input fingerprint + model/policy version → prior decision
```

The memory entry must include the decision/evidence that created it. If an earlier match is superseded or an entity splits, dependent memory entries can be invalidated.

This avoids both extremes: repeatedly spending LLM tokens resolving `"Intl Business Machines"` and permanently poisoning identity because one historic judgment was wrong.

### The resolution ledger

The ledger should be a **core concept**, but the whole application need not become a doctrinaire event-sourced system.

A ledger entry might be:

```python
LedgerEvent(
    event_id="evt_...",
    event_type="IDENTITY_MEMBERSHIP_ACCEPTED",
    subject_ids=["obs_...", "org_..."],
    basis_decision_id="dec_...",
    evidence_ids=["ev_...", "ev_..."],
    actor=Actor(kind="resolver", id="hybrid-v5"),
    reviewer=None,
    policy_version="organization-v8",
    created_at=...,
    supersedes=None,
)
```

A reviewer reversal should append another event; it should not erase the first.

Current state—entity membership, aliases, canonical snapshots, redirects—can then be materialized for fast queries. This closely parallels the broader separation Palantir documents between object state/edit history and action logs that preserve decision context. citeturn10view0turn10view1

The ledger provides:

```text
auditability
reproducibility
review history
training labels
rollback inputs
model comparison
incremental reprocessing
regression testing
explanations
```

It also makes model migration tractable: rerun a new resolver against historical evidence without pretending previous decisions never existed.

### Merging and splitting

Never implement:

```sql
UPDATE observation
SET entity_id = survivor
WHERE entity_id = loser;

DELETE entity_loser;
```

as the semantic model of merging.

Instead:

```text
MERGE:
    record MergeEvent(A, B → A)
    move/activate memberships under policy
    retire B
    retain redirect B → A
    preserve every former membership and decision

SPLIT:
    record SplitEvent(A → A, C) or (A → B, C)
    assign affected memberships to new entities
    retire or retain prior entity according to policy
    invalidate dependent memory entries
    preserve historical snapshots and redirects
```

Entity IDs should **never be reused**. Whether one side of a split keeps the old ID is a domain policy question. For high-stakes systems, retiring the ambiguous old entity and minting new identities for both descendants gives the cleanest historical semantics; for ordinary MDM, retaining the ID for the dominant component may reduce downstream disruption.

### Storage

A minimal deployment should not require Elasticsearch, a vector database, Kafka and Neo4j.

Recommended modes:

```text
Local:
    Python
    DuckDB
    Parquet
    optional local embedding index

Production:
    PostgreSQL
    optional pg_trgm / full-text / vector extension
    object storage for large immutable artifacts
    optional OpenSearch for candidate retrieval
    optional external vector index
    optional graph DB sink/adapter
```

Parquet is an open column-oriented storage format designed for efficient storage and analytical retrieval and is an appropriate interchange/archive layer for bulk observations and generated feature sets. citeturn21search1turn21search37

DuckDB is particularly attractive conceptually for the local/batch profile because ER spends significant time on analytical scans, blocking joins, group statistics and feature materialization. Splink's SQL-oriented approach illustrates how much of probabilistic linkage can be profitably pushed into a relational execution engine. citeturn1search0 PostgreSQL should be the default production persistence target where concurrent service workloads, transactions, durable ledger writes and operational indexes matter.

SQLite can be supported through an adapter but need not be a separate first-class architecture if DuckDB already owns local persistent operation. Elasticsearch/OpenSearch, graph databases and vector stores should all remain **optional indexes/projections**, not sources of identity truth.

### Privacy and security

PII must be policy-visible at the schema layer:

```python
Field(
    name="tax_id",
    sensitivity="restricted",
    matching_policy="exact_or_tokenized",
    log_policy="never",
    model_exposure="forbidden",
)
```

Recommended capabilities include deterministic keyed tokenization for exact cross-system matching, pluggable privacy-preserving matchers, per-field retention policies, redaction of evidence payloads, external-model exposure controls and audit-safe hashes/fingerprints. AWS ER's rule workflows demonstrate one production precedent for comparing certain hashed inputs and for hashing outputs, while its ML mode has different cleartext requirements—another reason privacy must be expressed per resolver rather than as one global switch. citeturn15search16turn15search23

An external LLM adapter should default to **deny raw sensitive fields unless explicitly permitted**. A resolver that requires sending names, addresses or identifiers to a remote model must advertise that capability requirement so deployment policy can disable it.

## API proposal

The package should feel like a small composable Python library first and an ER platform second.

A reasonable namespace is:

```text
entityresolve/
    core/
        observation.py
        entity.py
        decisions.py
        relationships.py
        scores.py

    types/
        registry.py
        person.py
        organization.py
        product.py
        address.py

    normalize/
    candidates/
    evidence/
    resolvers/
    policy/
    clustering/
    memory/
    ledger/
    review/
    storage/
    graph/
    eval/
    integrations/
        llm/
        search/
        vector/
        mcp/
```

I would **not** create separately installed packages called `entity-core`, `entity-evidence`, etc. in the first release. Maintain one coherent package with optional dependency extras; split repositories only after real extension pressure emerges.

A user-facing engine could be:

```python
from entityresolve import ResolutionEngine
from entityresolve.types import Organization
from entityresolve.storage import DuckDBStore

engine = ResolutionEngine(
    store=DuckDBStore("identity.duckdb"),
    entity_types=[Organization.default()],
)

result = engine.resolve(
    raw_value="IBM Corp",
    entity_type="organization",
    source={"document_id": "document_183"},
    context={"sentence": "..."},
)

print(result.outcome)
print(result.entity_id)
print(result.decision)
print(result.evidence)
```

But the high-level convenience API must delegate to independently replaceable components.

A fuller core interface might be:

```python
@dataclass(frozen=True)
class ResolutionRequest:
    observation: Observation
    candidate_limit: int = 50
    auto_commit: bool = False


@dataclass(frozen=True)
class ResolutionResult:
    observation_id: str
    outcome: Literal[
        "ATTACH_TO_EXISTING",
        "CREATE_ENTITY",
        "REQUIRES_REVIEW",
        "UNRESOLVED",
    ]
    entity_id: str | None
    candidates: tuple[Candidate, ...]
    decisions: tuple[ResolutionDecision, ...]
    evidence: tuple[EvidenceItem, ...]
    committed: bool
```

The principal methods should be:

```python
engine.ingest(...)
engine.find_candidates(...)
engine.collect_evidence(...)
engine.resolve(...)
engine.resolve_batch(...)
engine.commit(...)
engine.explain(...)
engine.review(...)
engine.merge_entities(...)
engine.split_entity(...)
engine.get_entity(...)
engine.search_entities(...)
engine.history(...)
```

The distinction between `resolve()` and `commit()` is valuable. Resolution computes a proposal. Policy determines whether it may modify current identity state.

**Review API:**

```python
review = engine.review.submit(
    decision_id="dec_123",
    verdict="MATCH",
    reviewer="analyst_42",
    reason_codes=["verified_company_registry"],
    notes="Registry number confirmed",
)
```

That review should automatically be available as an immutable training example, but training code decides whether it is eligible.

**Merge API:**

```python
engine.merge_entities(
    left="org_100",
    right="org_200",
    reason="duplicate master entities",
    evidence_ids=[...],
    expected_left_version=17,
    expected_right_version=9,
    approval=approval_token,
)
```

Optimistic version parameters prevent an agent or analyst from merging stale snapshots.

**Split API:**

```python
engine.split_entity(
    entity_id="org_100",
    partitions={
        "new_entity_a": ["obs_1", "obs_2"],
        "new_entity_b": ["obs_3", "obs_4"],
    },
    reason="legal entities were conflated",
    expected_version=21,
    approval=approval_token,
)
```

**Explanation API:**

```python
explanation = engine.explain(
    decision_id="dec_123",
    include=[
        "evidence",
        "rules",
        "scores",
        "model_versions",
        "history",
    ],
)
```

The answer should be reconstructable without asking an LLM to invent a post-hoc explanation.

### MCP surface

MCP is now an open standard for connecting AI applications to external systems, tools and data sources. citeturn21search0 That makes it an excellent **thin protocol boundary** around the library, but it should not become the internal architecture.

The proposed tools are close to right, with one crucial modification: separate *proposing* identity changes from *committing* them.

Recommended read-oriented tools:

```text
find_candidates
get_entity
search_entities
explain_resolution
get_resolution_history
```

Proposal tools:

```text
resolve_entity
resolve_batch
propose_merge
propose_split
propose_relationship
```

Controlled mutation tools:

```text
review_resolution
approve_resolution
commit_merge
commit_split
```

`resolve_entity` should default to read/proposal semantics:

```json
{
  "observation": {
    "raw_value": "IBM Corp",
    "entity_type": "organization",
    "source": {"document_id": "document_183"}
  },
  "mode": "propose"
}
```

The response should be structured:

```json
{
  "outcome": "ATTACH_TO_EXISTING",
  "candidate_entity_id": "org_1234",
  "decision": "MATCH",
  "scores": [
    {
      "kind": "calibrated_match_probability",
      "value": 0.998
    }
  ],
  "evidence": [
    {
      "kind": "normalized_name_match",
      "direction": "supports_match"
    }
  ],
  "requires_approval": false,
  "proposal_id": "proposal_..."
}
```

An agent should not receive a generic `execute_sql`, `set_entity_id`, or `write_same_as_edge` capability. The semantic tool boundary is itself a safety control.

High-impact merge/split operations should require:

```text
authenticated actor
authorization scope
expected entity version
idempotency key
reason code
proposal ID
approval token or configured auto-approval policy
```

MCP's purpose is standardized external tool interaction, not authorization by itself; therefore tool exposure and state-change policy belong to the ER service. citeturn21search0

### Agentic use cases

Entity resolution becomes unusually important for persistent agents because agents repeatedly cross system boundaries.

A research agent may encounter a company under a formal filing name, website brand, article shorthand and CRM account name. A procurement agent sees supplier masters, invoices, purchase orders and sanctions records. A legal agent sees legal entities, trade names, former names and subsidiaries. A RAG system sees thousands of textual mentions. Without a stable identity layer, downstream retrieval repeatedly re-solves the same ambiguity and risks aggregating facts from different entities.

The ER engine therefore becomes an **identity cache plus evidence service**:

```text
fragmented enterprise data
        │
        ▼
stable entity IDs
        │
        ├── RAG / search
        ├── knowledge graph
        ├── agent memory
        ├── analytics
        ├── investigations
        ├── supplier/customer 360
        └── workflow automation
```

The emerging direction in vendor systems is already toward real-time identity services rather than occasional batch dedupe; Senzing, AWS ER and cloud customer-profile products all expose persistent or incremental matching concepts in different forms. citeturn22search0turn15search16turn15search23 The architectural recommendation goes one step farther: an agent should consume resolved identity as shared infrastructure instead of embedding ad-hoc matching prompts inside every agent workflow.

## Evaluation strategy

Evaluation must distinguish **candidate retrieval**, **pair decisions**, **clusters**, **calibration**, **operational cost**, and **damage from mistakes**.

The most important metric is not generic F1. It is often **false-merge risk**.

Suppose:

```text
true entities:
A = 1,000 observations
B = 2,000 observations
```

One erroneous bridge that merges A and B can contaminate 3,000 observations even though the underlying classifier made only one bad pairwise decision. Pairwise accuracy may look excellent while entity integrity is catastrophically poor.

First-class metrics should therefore include:

| Layer | Required metrics |
|---|---|
| Candidate generation | candidate recall@K, mean candidate set size, retrieval latency |
| Pair decisions | precision, recall, F1, false-positive rate, false-negative rate |
| Automatic merge | **auto-merge precision**, false-merge rate, number of contaminated entities |
| Clustering | B-cubed precision/recall/F1, cluster purity, cluster recall, exact-cluster accuracy where feasible |
| Split quality | false split rate, fragmentation per true entity |
| Calibration | reliability curves, Brier/log loss for actual probabilities, calibration by entity type |
| Review | review rate, analyst agreement, time per case, reversals |
| Incremental behavior | identity churn, merge reversals, split frequency, candidate-index freshness |
| Efficiency | observations/sec, pairs/sec, p50/p95 latency, memory/storage |
| LLM | calls, tokens, cost, latency, schema failures, decision disagreement by model version |
| Graph safety | bridge-edge error rate, largest erroneous cluster, number of propagated errors |

The evaluation harness should make false-merge cost configurable:

\[
L =
c_{FM} N_{false\ merges}
+
c_{FS} N_{false\ splits}
+
c_R N_{reviews}
+
c_C \text{compute cost}
\]

where \(c_{FM}\) can be far larger than \(c_{FS}\) in knowledge-graph, compliance or investigation settings.

That leads to asymmetric thresholds:

```text
very high certainty → auto-match
moderate certainty  → review
weak evidence       → no-match / new entity
```

rather than simply thresholding everything at 0.5.

### Benchmark suite

No single existing benchmark adequately tests the proposed architecture.

The classic entity-matching suites used in neural work such as Ditto contain datasets covering bibliographic and product matching and are useful for controlled pairwise comparisons. citeturn5search0 These commonly used families should be included where licensing permits:

```text
DBLP–ACM
DBLP–Scholar
Amazon–Google products
Abt–Buy products
Walmart–Amazon products
```

They are valuable because many published entity-matching systems report results on overlapping benchmark families, making regression comparisons possible. Ditto's benchmark results are a useful modern neural baseline. citeturn5search0

But they are insufficient for this project because standard pair datasets usually do not stress:

```text
persistent entity IDs
incremental observations
merge reversal
entity splits
corporate hierarchies
relationship-vs-identity confusion
temporal changes
human review
resolution memory
graph propagation
LLM drift
```

A first-party **ER adversarial benchmark** should therefore become part of the open-source project.

It should contain synthetic and semi-synthetic cases such as:

```text
IBM
IBM Corp.
International Business Machines Corporation
IBM Research
IBM Consulting
a wholly owned IBM subsidiary
a historical predecessor
an unrelated company using IBM-like initials
```

and:

```text
Apple
Apple Inc.
Apple Bank
Apple Records
Apple Hospitality REIT
Apple Store #123
```

Person suites should include common names, married names, transliteration, swapped names, shared households, reused phones and sparse records. Product suites should include model-family neighbors where semantic similarity is intentionally high but SKU identity is different.

The synthetic generator should permit controlled corruption:

```text
character edits
OCR substitutions
token deletion/reordering
abbreviation
transliteration
address moves
identifier corruption
shared attributes
source-specific bias
temporal changes
relationship injection
```

The gold data should represent **entities and relationships**, not merely positive/negative pairs.

### Failure taxonomy and defenses

| Failure | Typical cause | Defensive pattern |
|---|---|---|
| Common names | low-discriminative name evidence | frequency-aware weighting; require independent evidence |
| Acronyms | many expansions share acronym | candidate retrieval only until contextual/identifier evidence |
| Transliteration | multiple valid romanizations | locale-aware normalization + multilingual candidate generation |
| Married/former names | temporal name change | temporally scoped aliases |
| Parent/subsidiary collapse | semantic/lexical similarity | organization hierarchy relation types + legal-ID constraints |
| Brand/legal-entity collapse | branding language | `brand_of` vs identity |
| Corporate rename | same legal identity changes label | temporal aliases and authoritative identifiers |
| Merger/acquisition | organizational continuity ambiguity | explicit `merged_into`, `successor_to`; do not assume identity |
| Franchise | shared brand/address patterns | distinguish operator/legal entity from brand |
| Shared address | household/office/coworking | address evidence receives limited weight |
| Shared domain | group companies/employee domains | domain evidence is supportive, not sufficient |
| Recycled phone/email | temporal reuse | validity intervals |
| OCR corruption | extraction errors | character models, document context, source quality |
| Sparse records | insufficient positive evidence | abstain/review rather than aggressive merge |
| Conflicting authoritative IDs | source errors or distinct entities | hard-negative constraint unless explicitly overridden |
| Embedding false positive | semantic relatedness | embeddings retrieve candidates; policy decides identity |
| LLM over-merge | plausible reasoning without hard grounding | schema-constrained output + evidence references + no direct write |
| Cluster chain error | A≈B and B≈C does not imply A=C | cluster consistency tests and bridge-edge review |
| Graph feedback loop | inferred relationships reinforce themselves | provenance tiers; don't reuse inferred evidence as independent evidence |
| Historical entity ambiguity | legal succession/renaming | temporal identity and typed successor relationships |

### Human review and active learning

The review queue should prioritize **expected risk reduction**, not merely raw uncertainty.

A queue item should contain:

```text
observation
top candidates
positive evidence
contradictory evidence
cluster impact
entity size
relationship context
resolver outputs
prior decisions
recommended review question
```

Review modes should include:

```text
pair review
observation-to-entity review
cluster review
merge review
split review
relationship review
```

Cluster review is especially important because pairwise review can miss a weak bridge that creates an incoherent cluster.

Dedupe's use of ML for ER supports the broader pattern of interactive labeled information improving matching. citeturn3search24 The engine should therefore expose reviewed examples in an explicit training store rather than automatically retraining in-place; automatic retraining could silently alter production behavior.

### Test methodology

Every release should execute four regimes.

**Static benchmark tests:** same frozen train/test datasets, enabling algorithm regression.

**Adversarial property tests:** generated ambiguity scenarios, especially identity-versus-relationship traps.

**Incremental replay tests:** feed records in chronological order and check that final entities, decision history and reversals are correct.

**Shadow-model tests:** run candidate resolvers without committing results and compare them against the production resolver and subsequent human decisions.

The ledger makes the fourth regime unusually powerful: a new model can be evaluated retrospectively against thousands of historical decision contexts without rewriting identity state.

## Build roadmap

The system should resist becoming an “enterprise identity platform” before it has a beautiful resolution kernel.

**MVP — build the semantic contract.**

Necessary:

```text
Observation
Entity
EntitySnapshot
Candidate
EvidenceItem
ResolutionDecision
IdentityMembership
RelationshipAssertion
LedgerEvent
```

Implement organization and person plugins first. Add deterministic normalization, exact/blocking candidate generators, edit/token similarity, optional TF-IDF retrieval, a rule resolver and a simple weighted/probabilistic resolver.

Use DuckDB for local persistence, with a clean storage protocol so PostgreSQL can follow.

Implement:

```text
resolve
find_candidates
explain
review
merge
split
history
```

from the beginning. Merge/split cannot be postponed without risking a destructive data model.

Include evaluation instrumentation from day one:

```text
candidate recall
auto-match precision
false merges
pair F1
review rate
latency
```

The MVP **does not need** a graph database, vector database, LLM dependency, Spark backend, distributed service, UI or universal ontology.

**V1 — make it production-worthy.**

Add PostgreSQL persistence, transaction-safe ledger commits, entity/version optimistic concurrency, resolution memory, incremental indexing, batch APIs and background-job integration hooks.

Add Fellegi-Sunter-style resolver support; Splink is a useful reference for explainable probabilistic comparison levels and scalable linkage. citeturn1search0turn20search9

Add pluggable search candidate generators:

```text
SQL lexical
OpenSearch/Elasticsearch
vector ANN
external canonical registry
```

Add organization, person, address and product plugins, typed relationship taxonomies, cluster consistency validation, calibration utilities and review queues.

Add the thin MCP server after the Python semantics stabilize. MCP provides a standardized way for AI applications to reach tools/data, so a semantic ER tool surface is a natural integration layer rather than a reason to redesign the core. citeturn21search0

**V2 — add model-assisted resolution.**

Add:

```text
transformer pair resolver
embedding candidate generator
LLM evidence extractor
LLM constrained adjudicator
model registry
prompt/version registry
shadow evaluation
active-learning sampler
```

Ditto provides a strong benchmark/reference point for transformer pair matching, while current LLM research provides useful evidence for constrained LLM-assisted and cluster-level ER. citeturn5search0turn5search3

LLM functionality should be an optional package extra. Nothing in the core should import an LLM SDK.

Add graph adapters in V2:

```text
export canonical entities
export observation provenance
import relationship context
generate graph evidence
write accepted relationships
```

but keep graph storage optional.

**Longer term — build identity infrastructure, not merely matching algorithms.**

The strongest opportunities over the next three to five years are likely to be:

```text
multimodal evidence from documents/images
domain-trained local adjudication models
continuous entity-state maintenance
learned candidate routing
collective graph reasoning
privacy-preserving cross-organization matching
automated model/rule discovery
agent-native identity queries
cross-modal source provenance
temporal organizational identity
```

The direction suggested by Transformer ER, cluster-oriented LLM ER, persistent commercial identity systems and MCP-style agent tooling is that entity resolution is moving from a preprocessing operation toward a reusable **identity infrastructure primitive**. citeturn5search0turn5search3turn22search0turn21search0

The analogy to search is useful:

```text
Search:
raw documents → indexes → retrieval → stable references

Modern ER:
raw observations → identity indexes → evidence → stable entities
```

and likewise to retrieval infrastructure:

```text
vector databases answer:
"What looks related?"

entity resolution answers:
"What is actually the same thing, according to these rules and this evidence?"
```

Agents need both. Confusing them is dangerous.

The portions of your existing supplier consolidation workflow that should carry forward are therefore quite clear:

```text
KEEP:
raw data retention
normalization
constrained model reasoning
embedding-assisted discovery
fast review
canonical entities

CHANGE:
"model reconciliation"
    → one resolver in a resolver ensemble

"embedding cluster"
    → candidate/review structure, not identity truth

"agent review"
    → structured review decision + ledger event

"canonical entity"
    → stable entity ID + derived snapshot

ADD:
typed evidence
hard constraints
resolution memory
relationship assertions
merge/split history
evaluation and calibration
```

That preserves what worked empirically while eliminating the aspects that would become liabilities at knowledge-graph or enterprise scale.

## Key architectural decisions

**Decision: Keep observations logically immutable and separate from canonical entities.**

**Rationale:** Source records and document occurrences are evidence about entities, not the entities themselves. Keeping them separate makes splits, reprocessing, audit, temporal reasoning and new model evaluation possible. Palantir's documented separation between integrated data assets and Ontology objects is a useful analogous production pattern. citeturn8view1

**Tradeoff:** Storage use is higher, and retention/privacy controls must be able to redact or tombstone payloads rather than interpreting “immutable” as “legally undeletable.”

**Decision: Give every canonical entity a system-generated stable identifier independent of names and source keys.**

**Rationale:** Names and external IDs can change, conflict or be replaced. Stable semantic object identity is also a core concept in Palantir's object model. citeturn6search9turn6search16

**Tradeoff:** Downstream systems need redirect/lineage semantics when two previously issued entity IDs are merged.

**Decision: Make candidate generation and identity decisioning separate public abstractions.**

**Rationale:** Retrieval optimizes recall and cost; decisioning optimizes precision and calibrated identity. Existing record-linkage toolkits already separate indexing from comparison/classification. citeturn3search23

**Tradeoff:** More interfaces and instrumentation than a single `match()` API.

**Decision: Treat evidence as immutable first-class data and decisions as separate immutable records.**

**Rationale:** The same evidence can later be scored by a new Fellegi-Sunter model, gradient booster, LLM or human policy without pretending the evidence itself changed. Splink's decomposed comparison levels demonstrate the value of inspecting the contributing pieces of a probabilistic match. citeturn20search9

**Tradeoff:** Evidence records can become voluminous, so storage policies and compact representations will be needed at very high scale.

**Decision: Do not define one universal confidence number.**

**Rationale:** BM25 relevance, cosine similarity, Fellegi-Sunter weights, calibrated probabilities, deterministic rules and human judgments have incompatible semantics. AWS itself exposes different explanatory constructs for rule-based versus ML matching. citeturn15search16turn15search23

**Tradeoff:** Client applications must understand typed scores rather than sorting blindly on `confidence`.

**Decision: Make identity, aliases, external mappings and relationships different assertion types.**

**Rationale:** `same_entity`, `alias_of`, `subsidiary_of`, `brand_of`, `successor_to` and `located_at` answer fundamentally different questions. Graph ER research shows relationships are useful evidence, but relational similarity does not erase those distinctions. citeturn19search7

**Tradeoff:** Ontology design becomes more explicit and requires domain configuration.

**Decision: Do not physically collapse source observations during entity merges.**

**Rationale:** A merge should alter accepted membership/current semantic state while leaving historical evidence intact. This makes unmerge/split an ordinary state transition instead of data archaeology.

**Tradeoff:** Consumers querying raw tables may see apparent duplicates unless they use canonical-entity projections.

**Decision: Maintain an append-only resolution ledger plus materialized current state.**

**Rationale:** This gives the project the audit and replay advantages of event sourcing without requiring every read to reconstruct the world from events. Palantir's action-log/edit-history separation is a useful analogous pattern for preserving decision context alongside current object state. citeturn10view0turn10view1

**Tradeoff:** Ledger schemas and materialized projections must evolve carefully.

**Decision: Optimize automatic matching for false-merge avoidance, not maximum global F1.**

**Rationale:** A false merge can contaminate an entire entity cluster and propagate incorrect knowledge into graphs, analytics and agents. Ambiguous cases should abstain into review rather than be forced into a binary answer.

**Tradeoff:** Recall decreases and human-review or unresolved rates increase.

**Decision: Use embeddings primarily for candidate generation and review discovery.**

**Rationale:** Dense representations excel at retrieving semantically related material but semantic relatedness is not equivalent to identity. They may also contribute a typed feature to a calibrated resolver.

**Tradeoff:** Some domains with excellent identity-trained embeddings will not exploit the model as aggressively as a pure nearest-neighbor system could.

**Decision: Constrain LLMs to evidence extraction, adjudication and explanation; never grant them direct identity-state mutation.**

**Rationale:** Current research demonstrates promising LLM ER performance but also exposes hallucination and cluster-merging risks. citeturn5search3turn5search15 A structured resolver boundary preserves the ability to replace, audit or distill the model. citeturn5academia35

**Tradeoff:** The LLM receives less freedom than an end-to-end agent and requires more engineering around schemas and evidence.

**Decision: Treat human review as a normal resolver and a source of supervised feedback.**

**Rationale:** Interactive ML systems such as Dedupe demonstrate the importance of user-provided labels in practical ER. citeturn3search24 Human decisions should feed a reusable training store and rule-discovery process.

**Tradeoff:** Reviewer consistency, permissions and disagreement must themselves be measured.

**Decision: Make resolution memory revocable and provenance-aware.**

**Rationale:** Accepted aliases and mappings should prevent repeated expensive reasoning, but no learned mapping should become irreversible merely because it was previously accepted.

**Tradeoff:** Cache invalidation becomes a genuine identity-management concern, especially after splits.

**Decision: Use entity-type plugins for semantic behavior while keeping the core protocol domain-neutral.**

**Rationale:** People, legal entities, facilities and products require different evidence and constraints, while candidate/evidence/decision/ledger mechanics remain common.

**Tradeoff:** Plugin authors must design explicit schemas and policies rather than relying on one generic matcher.

**Decision: Keep graph databases, vector stores and search engines as optional indexes/projections rather than identity authorities.**

**Rationale:** Search engines are excellent candidate generators, vectors provide semantic retrieval and graph stores provide relational context, but none alone supplies the policy, provenance and reversibility required for identity. Elasticsearch's own text-analysis model is fundamentally retrieval-oriented, while Neo4j's graph framing emphasizes contextual relationships. citeturn18search13turn17search19

**Tradeoff:** There is an additional core store whose state must be synchronized to external indexes.

**Decision: Make DuckDB the preferred local/batch profile and PostgreSQL the preferred production relational profile, with adapters rather than infrastructure mandates.**

**Rationale:** ER benefits heavily from relational scans, blocking joins and feature aggregation, as SQL-oriented systems such as Splink demonstrate. citeturn1search0 Parquet provides an efficient open bulk-storage/interchange format for observations and feature artifacts. citeturn21search1

**Tradeoff:** Extremely large distributed workloads may eventually need specialized Spark/warehouse executors.

**Decision: Expose MCP as a semantic safety boundary over the Python library, not as a second implementation.**

**Rationale:** MCP standardizes AI access to external tools and data. citeturn21search0 The server should therefore expose `resolve`, `find_candidates`, `explain`, `propose_merge`, `review` and controlled commit operations while the Python package remains the source of behavior.

**Tradeoff:** Some low-level library capabilities will intentionally not be remotely exposed.

**Decision: Model entity resolution as persistent constrained reasoning rather than a one-time pair classifier.**

**Rationale:** The system ultimately needs to answer not merely whether two strings look alike but whether a new observation should attach to an existing identity, create a new identity, remain unresolved, trigger review, or establish a non-identity relationship—while preserving enough evidence to change its mind later. Classical probabilistic linkage, collective graph ER, transformer matching, emerging LLM ER and production identity systems each solve useful portions of this problem; none is sufficient alone. citeturn20search1turn19search7turn5search0turn5search3turn15search30

**Tradeoff:** This is a larger conceptual contract than a deduplication library. The payoff is that the same substrate can support batch linkage, knowledge graphs, enterprise master identity, document intelligence, retrieval, RAG and agent workflows without sacrificing deterministic evidence, provenance, uncertainty or reversibility.

The resulting architecture can be summarized in one pipeline:

```text
                         ┌──────────────────────┐
                         │    Source systems    │
                         │ documents / graphs   │
                         └──────────┬───────────┘
                                    │
                                    ▼
                           immutable Observation
                                    │
                     normalize / type / contextualize
                                    │
                                    ▼
                         Candidate Generation
                  exact │ lexical │ vector │ graph
                                    │
                                    ▼
                             Evidence Set
                positive │ negative │ contextual
                                    │
                                    ▼
                       Constraints + Resolution
             rules │ probabilistic │ ML │ LLM │ human
                                    │
                                    ▼
                            Decision / Review
                                    │
                   ┌────────────────┴───────────────┐
                   │                                │
                   ▼                                ▼
          Identity Membership              Relationship Assertion
                   │                                │
                   └───────────────┬────────────────┘
                                   ▼
                            Stable Entity ID
                                   │
                     canonicalized EntitySnapshot
                                   │
               ┌───────────────────┼────────────────────┐
               ▼                   ▼                    ▼
         Knowledge graph      Search / RAG         AI agents
               │                   │                    │
               └───────────────────┴────────────────────┘

                 Every consequential transition
                              │
                              ▼
                    append-only Resolution Ledger
                              │
             evidence / versions / reviewer / provenance
```

That is the architecture I would choose from first principles in 2026: **not a monolithic matcher, not a graph-merging algorithm, and not an LLM wrapper, but a small identity kernel whose durable abstractions are observations, evidence, decisions, stable entities, relationships, memory and reversible history.**