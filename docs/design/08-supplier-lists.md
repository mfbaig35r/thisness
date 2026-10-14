# Design 08: supplier lists

Status: accepted 2026-10-14, with the recommendations in section 10.

Builds on: design 05 (the name-only plan, §6; the rewrite-and-hash baseline, §11.5), design 07 (real organizations) and the organization profile as of commit fbe761c.

## 1. Why

A vendor master, an accounts-payable extract or a spend cube is a list of supplier names and little else. Thisness resolves such lists with the name-only plan, which on the names-only benchmarks matches the best rewrite-and-hash pipeline on precision and recall and adds what that pipeline lacks: a review queue for what it cannot settle and the votes behind every link (design 05 §11.5). Identity is not what is missing.

What is missing is everything around it that a supplier list needs:

- **Clean names.** Real extracts carry payment-processor prefixes ("PAYPAL *ACME", "SQ *JOES CAFE"), store and account numbers ("ACME CORP #4411", "Acme - 812773"), and notes ("DO NOT USE", "DUPLICATE OF 10233"). The organization normalizer strips a legal form only from the end of a name, so "Harjas Foods PLC - 812773" keeps both the number and the "PLC" in its key.
- **A readable name per entity.** An entity's label is its most frequent spelling, so a company most often written in a registry's capitals is labelled "HALEON UK FINANCE LIMITED".
- **The mapping.** What a spend analyst takes away is a table: every source record, the entity it belongs to, and that entity's name. Thisness has the data and no export.

## 2. What was measured first

- **Name-only resolution works without cleaning, but rules cost little.** With account numbers on a quarter of the names, Fellegi-Sunter on names loses 2 to 4 points of recall (synthetic: .571 to .528; UK: .367 to .344); the name-only plan already strips account numbers in its collapse key. The benchmarks carry no payment prefixes or notes, so their cost is unmeasured.
- **Blocking is not the bottleneck.** With names only, the candidate generators propose 96.8% of true pairs on the synthetic benchmark and 96.4% on the UK one (98.0% and 96.5% with full records). Names-only recall is low (.53 and .34 for Fellegi-Sunter) because a name is weak evidence, not because pairs go unproposed. An embedding candidate generator could raise the ceiling by at most 3.5 points here.
- **A model's string equality does not over-merge** (design 05 §11.5), so a model asked to clean names does not threaten precision by itself, provided it only cleans and the identity decision stays with the plan.

## 3. Name cleaning by rules

A new version of the organization name normalizer (`organization_name` v3) cleans before it tokenizes. Each rule is a dictionary or pattern in the normalizer's configuration, so changing one is a new component version and a feature rebuild, as now. The raw name is never changed; cleaning only shapes the features.

| Rule | Examples | Removed |
|---|---|---|
| Payment-processor prefixes | "PAYPAL *ACME", "SQ *JOES CAFE", "TST* BLUE DOOR", "SP * SHOPNAME" | the prefix through its asterisk |
| Trailing reference numbers | "Acme - 812773", "ACME CORP #4411", "Acme Corp 812773", "Acme (Vendor 10233)" | a number at the end after a separator ("-", "#", ":") or a reference word, a bare trailing number of five or more digits, or a trailing parenthesis with a reference word |
| Store and location suffixes | "WALGREENS STORE 1022", "SHELL OIL 57442 BLDG 3" | "store", "unit", "bldg", "loc" and a number |
| Notes | "DO NOT USE", "INACTIVE", "DUPLICATE OF 10233", "(OLD)", "USE VENDOR 4411" | the note |

What the rules must not remove: numbers that are part of a name ("3M", "7-Eleven", "Studio 54"), numbered vehicles ("Omega Holdings II", "Landmark Mortgage Securities No. 3"), which the `number_differs` level relies on (design 07 §13.2), and years ("Mayfield 1966 Investments"). Trailing numbers are stripped only after a separator or a reference word, or when they have five or more digits; Roman numerals and "No. n" are never stripped.

Two things the rules find are kept rather than dropped:

- **Notes are observation metadata.** "DO NOT USE" and "INACTIVE" say something about the vendor record, not the vendor. They become a `record_status` feature, shown on the record in the Workbench and in the export, never compared.
- **Trade names are a second name.** "Acme Holdings LLC dba Acme Tools" and "T/A" forms yield two names for one record. The record's name stays as written; the trade name becomes an alias feature that candidate generation and the name comparison read as an additional value (decision 3).

## 4. Name cleaning by a model, only if the rules leave enough behind

What rules miss (mangled processor descriptors, free-text notes, names run together with addresses) a model can clean. It is built only if the measurement in §8 shows a residue worth the cost and the machinery:

- One call per batch of distinct raw names not already clean by the rules, cached by the raw string, so a name is cleaned once per store and the answer is replayed thereafter.
- The answer is stored as an **annotation**: a value derived from an observation by a recorded component or model invocation, never an edit to the observation. Normalizers read a name's annotation when one exists. Annotations are new (decision 4): they need a table, a ledger-free rebuild rule (dropping an annotation component rebuilds features without it) and a place in the dossier.
- The prompt only cleans; it is never asked to make two names equal. Consolidation stays with the name-only plan, where it is voted and reviewable.

## 5. Display names

Two steps, the first free:

1. **Prefer a readable spelling.** The organization canonicalizer picks, among the spellings of the entity's most supported name key, the one in mixed case when one exists, else the most frequent. "HALEON UK FINANCE LIMITED" (3 records) and "Haleon Uk Finance Limited" (1) share a key; the label becomes the second. Support counts are unchanged: the dossier still shows how many records carry each value.
2. **A model's suggestion, recorded and reviewable.** For an entity whose best spelling is all capitals, abbreviated or truncated, the review assistant can suggest a display name from its variants ("Haleon UK Finance Limited"). An accepted suggestion is a ledger event (`ENTITY_LABELED`) citing the suggestion, so it is reversible and the snapshot shows it; the canonicalizer never overrides an accepted label. Suggestions are asked for in bulk, like review recommendations, and cost one short call per entity.

## 6. The mapping export

```bash
thisness export mapping --type organization > mapping.csv      # or --format parquet
```

`engine.mapping(entity_type, as_of_seq=None)` returns the same table:

| Column | |
|---|---|
| `source_type`, `source_id`, `locator` | the record as it was ingested |
| `name` | the record's name as written |
| `observation_id` | |
| `entity_id`, `entity_name` | its entity, and the entity's display name |
| `status` | `resolved`, `in_review` (a member of an open review), `provisional` |
| `record_status` | the note the cleaning rules found, if any (§3) |
| `since_seq`, `decision_id` | when the membership began, and the decision behind it |

`as_of_seq` exports the mapping as it stood at a point in the ledger, so a spend report can be reproduced after later merges. The Workbench gets a download link on each entity list.

## 7. Embedding candidates, deferred

§2 shows the generators already propose nearly every true pair on these benchmarks, so an embedding generator is not part of this milestone. It stays the answer to a measured gap: if real supplier lists show pairs that share no blocking key (a renamed company with its old and new names in one list is not one; nothing in two unrelated names says they are the same), an `EmbeddingNeighbors` generator would embed cleaned names through the provider layer (OpenAI-compatible `/embeddings`, so a local server works too), cache vectors by text, and propose nearest neighbours above a similarity, to be scored like any other candidate.

## 8. Benchmark

The names-only benchmarks gain a **messy** variant (`--names-only --messy`): on a share of records, a payment prefix, a store or location suffix, a reference number in one of the forms of §3, a note, or a trade name. The same generator settings apply to the synthetic and UK benchmarks. Measured before and after each step:

1. Fellegi-Sunter on names and the name-only plan, on clean and messy names, before any rule (the cost of mess).
2. The same after the rules (§3).
3. If the residue warrants it, model cleaning (§4).
4. Display names: the share of entities whose label is all capitals, before and after §5.1.

A regression test fixes the rules' behaviour on a list of names that must and must not change, including every example in §3.

## 9. Order

1. **The mapping export** (§6). Small, useful on its own, and the table every later step is checked against.
2. **The messy benchmark and the first measurement** (§8.1).
3. **Cleaning rules and the readable-spelling canonicalizer** (§3, §5.1), measured (§8.2, §8.4).
4. **Display-name suggestions** (§5.2).
5. **Model cleaning**, only if §8.2 leaves a residue worth it (§4).

## 10. Decisions

| # | Question | Recommendation |
|---|---|---|
| 1 | What to take from rewrite-and-hash pipelines | Cleaning and display names; not their identity rule (design 05 §11.5) |
| 2 | Where cleaning lives | In the organization name normalizer, versioned; the raw name never changes |
| 3 | Trade names ("dba", "t/a") | An alias feature read by candidates and the name comparison; the record's name stays as written |
| 4 | Model cleaning | Deferred until the rules are measured; if built, as annotations (a new record type), never edits to observations |
| 5 | Display names | Readable spelling by rule now; model suggestions accepted as a reversible ledger event |
| 6 | Export | `thisness export mapping` and `engine.mapping()`, with `as_of_seq` |
| 7 | Embedding candidates | Deferred: blocking proposes 96 to 98% of true pairs on names alone |
| 8 | Prompts | Written for Thisness; no prompt or dictionary is copied from other pipelines |

## 11. Risks

- **Rules that strip part of a name.** A number, a word like "Store" or a note-like phrase can be the name itself ("Store 54 Records", "The Inactive Club"). The must-not-change list in §8 is the guard, and every rule is a dictionary entry that can be removed in a new version.
- **Locale.** Payment descriptors and note conventions differ by country and by ERP. The first dictionaries are US and UK forms; others are additions, not code.
- **Annotations, if built, are a new kind of state.** They must rebuild features deterministically from recorded answers and must not let a model's cleaning become a merge by the back door. The prompt cleans one name at a time, and §2's finding about string equality is checked again on the messy benchmark before they ship.

## 12. Not in scope

Supplier classification (industry, commodity), web enrichment, and parent-subsidiary hierarchies: relationships already carry "subsidiary of", and Level 2 data is design 07's.

## 13. Implementation notes

### 13.1 Step 1: the mapping export

- **As built.** `engine.mapping(entity_type, as_of_seq=None)` returns a pyarrow table with the columns of §6, from one query over the ledger: records ingested by the point, memberships valid at it, each entity's latest snapshot by then, entities created provisional and not yet promoted, and reviews open at it. `thisness export mapping [--type] [--format csv|parquet] [--output] [--as-of SEQ]` writes it (CSV to standard output without `--output`), and the Workbench's entity lists have a Mapping download. `record_status` is empty until the cleaning rules (step 3) fill it.
- **Status.** `in_review` when the record or its entity is a member of an open review; then `unresolved` (no entity), `provisional`, `resolved`. On the 46,565 UK records of design 07 §13.4, 69% of records are `in_review`: 2,629 boundary reviews are open there, and a boundary review holds every record of the entities it asks about. That is what the status says (those records' entity may still change), but an analyst may read it as doubt about the records themselves; a `review_id` column, so a report can group by the open question, is a small addition if it proves needed.
- **Cost.** 40 ms for the 46,565 records, 70 ms as of an earlier point.
- **Tests** (`test_mapping.py`): one row per record with its current entity and that entity's name; status agreeing with open reviews and entity states; a point before resolution (every record unresolved) and before ingest (no rows); a merge changing the mapping from its seq on and not before; the command in CSV and Parquet; the Workbench download, its 404 and its link.
