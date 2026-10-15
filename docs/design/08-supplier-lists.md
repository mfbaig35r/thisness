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
- **Trade names are a second name, but weak.** "Acme Holdings LLC dba Acme Tools" and "T/A" forms yield two names for one record. The record's name stays as written; the trade name becomes an alias feature that candidate generation reads, and the name comparison scores a match on it at a capped level (0 bits) and treats as a relationship signal: a trade name is often a group's brand, shared by sister companies (§13.2, decision 3).

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
| 3 | Trade names ("dba", "t/a") | An alias feature read by candidates; matched only at a capped level (0 bits) and as a relationship signal, since a trade name is often a group's brand. The record's name stays as written (revised 2026-10-14, §13.2) |
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

### 13.2 Step 2: the messy benchmark, and what mess costs

- **The variant** (`messy()`, `thisness eval --names-only --messy`): on 35% of names-only records, one of a payment-processor descriptor ("SQ *", "TST* ", "PAYPAL *" and the name in capitals, half of them cut at 22 characters as card statements cut them), an ERP note ("ZZ ", "XX-", "*INACTIVE* ", "DO NOT USE", "(OLD)", "DUPLICATE OF n", "USE VENDOR n"), a store or location suffix, a reference number ("(VENDOR n)", "REF n", "A/C n"), or a trade name ("dba", "T/A" and the name's first one or two words). It applies to the synthetic and UK benchmarks alike, on top of the account numbers names-only already appends.
- **Results** (gpt-6-luna; seeds 20261007 and 20261009; every model run recorded under `~/thisness-demo/messy`):

  | Benchmark | Method | Clean P | Clean R | Messy P | Messy R | False merges, clean to messy | Reviews, clean to messy |
  |---|---|---|---|---|---|---|---|
  | Synthetic, 1,776 records | Fellegi-Sunter on names | 1.000 | .528 | 1.000 | .507 | 0 to 0 | 219 to 137 |
  | | name-only plan | .985 | .968 | .987 | .865 | 6 to 5 | 20 to 21 |
  | | rewrite, passes by name | .983 | .976 | .982 | .967 | 8 to 8 | |
  | UK, 3,507 records | Fellegi-Sunter on names | .9996 | .344 | 1.000 | .335 | 1 to 0 | 466 to 340 |
  | | name-only plan | .995 | .944 | .996 | .803 | 7 to 7 | 21 to 47 |
  | | rewrite, passes by name | .996 | .949 | .986 | .923 | 6 to 28 | |

- **Mess costs the name-only plan recall, not precision.** 10 points on the synthetic benchmark and 14 on the UK one, at the same precision. The collapse key no longer folds a name's variants together once one of them carries a descriptor or a note (UK nodes 1,574 to 2,524), so more of them reach the model as separate items, and some are never grouped. The UK run also made more than twice the calls ($0.15 to $0.32). This is what the rules of §3 are for: a cleaned key collapses them again before any model sees them.
- **A model that cleans while it consolidates keeps recall and loses precision.** The rewrite baseline's false merges on the UK names went from 6 to 28. Read one by one, they are the cases §3 and §11 anticipated: a truncated descriptor ("TST* BRIDGEHAVEN EUROP") taken for a prefix of another company's name; numbered companies cleaned into one ("Intercede 2296 Ltd", "Intercede 2302 Ltd", "Intercede 2343 Limited"); a qualifier dropped ("BRIP 6 (Poole BC) LLP" and "BRIP 6 (Poole) LLP"); and trade names that are a group's brand ("BRIP 6 (POOLE) LLP dba BRIP 6"). It is the evidence for decision 4's rule that a model only cleans, and for the must-not-change list of §8.
- **Decision 3 revised** (accepted 2026-10-14). A trade name as an alias the name comparison reads as a second name would merge sister companies that trade under one brand, as the last case shows. Proposed: the trade name is an alias feature that candidate generation reads, and the name comparison scores at a capped level (0 bits, like `token_subset`) and treats as a relationship signal, so two records sharing only a trade name are related until other evidence says they are the same.
- **Tests** (`test_messy.py`): determinism and unchanged truth; the share of changed names and every kind of mess, truncated descriptors included; the UK variant; `--messy` requiring `--names-only`.

### 13.3 Step 3: cleaning rules and the readable label

- **As built.** `clean_name()` in the organization normalizer (version 3) removes, before tokenizing: payment-processor prefixes; ERP notes before or after the name, which become the record's status (`do_not_use`, `inactive`, `blocked`, `old`, `duplicate`, `use_other`; a feature, shown in the mapping export, never compared); store and location suffixes; reference numbers after a separator or a reference word, bare trailing numbers of five or more digits, and any number after a legal form ("Amazon, Inc. 4411"); and trade names after "dba", "t/a", "trading as", which become an alias key. Suffixes stack and are removed until none is left. A name that is only a note keeps itself. Every pattern is in the normalizer's configuration. `test_cleaning.py` holds the lists of names that must change and must not ("3M", "7-Eleven", "Studio 54", "Omega Holdings II", "No. 3", "Mayfield 1966 Investments", "Intercede 2296 Ltd", "Old Mutual", "The Inactive Club", "Unit 4 Ltd", "Building 3").
- **Trade names** (decision 3 as revised): candidate generators pair an alias with the name it equals and records sharing an alias; the name comparison (version 5) has a `trade_name` level, neutral, capped at 0 bits, a relationship signal. It and `truncated_prefix` are placed before `else` so the term-frequency levels keep their indices.
- **A card descriptor cut short is not a name.** The first version dropped the possibly cut last word of a descriptor 22 characters long. Measured, that made two companies' descriptors identical ("SP * ASSOCIATED BRITIS", "TST* ASSOCIATED BRITIS" both "Associated") and one company's descriptor another's name ("SQ *LOW PROFILE FASHIO" read "Low Profile", a different company's): six false-merge clusters for Fellegi-Sunter and twenty for the name-only plan on messy UK names, most collapsed before any model saw them. Now the cut word is kept and the name marked (`name.truncated`); the name comparison gives a cut name that could begin the other only `truncated_prefix` (neutral, capped at 0), even when two cut names read alike; and the name-only plan never collapses one, so each reaches the model on its own.
- **The name-only plan** collapses on the cleaned key, without its former rule of dropping every number of three or more digits, so numbered companies ("Intercede 2296 Ltd", "Intercede 2302 Ltd") reach the model apart; and its windows follow the cleaned key, so "SQ *HOWDEN JOINERY" sits beside "Howden Joinery".
- **Results** (gpt-6-luna; recordings under `~/thisness-demo/cleaned2`; before = step 2, §13.2):

  | Benchmark | Method | Before P | Before R | After P | After R | False merges, before to after |
  |---|---|---|---|---|---|---|
  | Synthetic, clean names | Fellegi-Sunter | 1.000 | .528 | 1.000 | .571 | 0 to 0 |
  | | name-only plan | .985 | .968 | .990 | .956 | 6 to 5 |
  | Synthetic, messy names | Fellegi-Sunter | 1.000 | .507 | 1.000 | .548 | 0 to 0 |
  | | name-only plan | .987 | .865 | .986 | .964 | 5 to 6 |
  | UK, clean names | Fellegi-Sunter | .9996 | .344 | .9995 | .367 | 1 to 1 |
  | | name-only plan | .995 | .944 | .995 | .948 | 7 to 6 |
  | UK, messy names | Fellegi-Sunter | 1.000 | .335 | .9995 | .364 | 0 to 1 |
  | | name-only plan | .996 | .803 | .993 | .930 | 7 to 13 |
  | Synthetic, full records | Fellegi-Sunter | .9994 | .923 | .9994 | .923 | 1 to 1 |
  | UK, full records | Fellegi-Sunter | .9992 | .9119 | .9992 | .9116 | 1 to 1 |

  On messy names the name-only plan recovers most of what mess cost (synthetic to the clean level; UK 12.7 of 14 points), at about half the UK cost ($0.32 to $0.23). The UK's 13 false-merge clusters, read one by one, are mostly the model joining a cut descriptor to a whole name it fits ("PAYPAL *ZODIAC MARITIM" and Zodiac Maritime Limited; "TST* ROYAL FREE HOSPIT" and Royal Free Hospital) where the benchmark gave the descriptor to a namesake: nothing in the record can tell them apart. Before the rules those descriptors sorted among the S's and P's, out of their names' windows, so they were neither merged wrongly nor found. Guidance telling the model that a cut descriptor may name another company is a possible next step. The synthetic clean run's recall fell a point (.968 to .956) with one fewer false merge, from the new window order.
- **The readable label.** The organization canonicalizer (`ReadableSpelling`) labels an entity with a spelling of its most supported cleaned key that needed no cleaning, then one in mixed case (most of its words with a lower-case letter, so "AXA XL UNDERWRITING AGENCIES Ltd." does not count), then the most frequent. On 548 UK entities, labels in capitals fell from 77% to 56%; 41% have no spelling but capitals, which is what step 4's suggestions are for. Case it cannot know stays wrong ("Axa Xl"), for the same reason.
- **The shipped organization starter** was rebuilt for name comparison version 5.
- **Tests** (`test_cleaning.py`): every name that must change, and every one that must not; statuses; trade names and descriptors; features from the cleaned name; the trade-name level, its cap and relationship signal, and the term-frequency levels' places; a cut descriptor never exact or collapsed; numbered companies apart in the collapse key; the readable label; the record status in the mapping.
