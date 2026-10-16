# Changelog

## Unreleased

Real people (design 09).

- **Name order.** A person record's `name_order` attribute (`given_first`, the default, or `family_first`) says how its source writes a name without a comma; registries such as SEC EDGAR write "SMITH JOHN A JR". A family-first name of three words or more also gets the reading with a compound family name ("Pineyro Sublett Carla" as Carla Pineyro Sublett), which proposes candidates and counts as a `fuzzy` name match. A family-first name is also a blocking key with its order swapped, since registries are not always consistent. Person name normalizer and comparison version 2; the person starter model is rebuilt. Given-first names are unaffected.
- **Given name and employer** is a new person blocking key (`given_employer`): where there is no date of birth, it finds family names that changed or were mistyped. Colleagues share employers, so EM does not learn from its pairs.
- **Benchmarks.** `python -m thisness.benchmark edgar-stage --store PATH` stages SEC Section 16 insiders from an edgar-persons-pool store; `edgar-truth` and `edgar-audit` measure the CIK as truth; `thisness eval --type person --source edgar` runs the benchmark: real people and namesakes, each with their EDGAR records and up to three records from other systems written as those write people (nicknames, initials, dropped suffixes, typos, former names, employers without their legal form). A fixture of about 150 insiders ships for tests. `thisness eval --assistant` has the review assistant answer every review a run leaves open and scores its suggestions against the truth (`benchmark.evaluate.recommendation_accuracy`). On 1,000 real people gpt-6-luna deciding pairs raised false-merge clusters from 2 to 18, while its review suggestions were right 86% of the time; people keep `model_role="suggest"`.

## 0.2.0 (2026-10-15)

Supplier lists (design 08).

- **The mapping export.** `engine.mapping(entity_type, as_of_seq=None)` and `thisness export mapping` give every record, its entity, the entity's name and a status (`resolved`, `in_review`, `provisional`, `unresolved`), as CSV or Parquet, as of any point in the ledger; the Workbench's entity lists download it.
- **Supplier-name cleaning.** The organization name normalizer (version 3) removes payment-processor prefixes, store and location suffixes, reference numbers and ERP notes before comparing names; a note becomes the record's status. Numbers inside a name ("Intercede 2296 Ltd", "Omega Holdings II") are kept. A trade name ("dba", "t/a") is an alias that proposes candidates and is a relationship signal, not a match. A card descriptor cut at its length limit is never taken for a whole name. On messy names the name-only plan's recall rises from .865 to .964 (synthetic) and .803 to .93 (UK).
- **Readable names.** Organization labels prefer a mixed-case spelling. A model can suggest display names for entities written in capitals (`thisness labels suggest`); a suggestion must be a spelling of the entity's own names, and accepting it is a reversible commit (`ENTITY_LABELED`), kept through later merges and splits.
- **Benchmarks.** `thisness eval --names-only` takes the UK benchmark; `--messy` adds what real extracts carry; `--plan rewrite` runs a rewrite-and-hash baseline of the kind supplier-consolidation pipelines use (design 05 §11.5).
- **Changed.** Name comparison version 5 (levels `trade_name` and `truncated_prefix`); the organization starter model is rebuilt. The name-only plan collapses on the cleaned name and no longer drops every number of three or more digits, so numbered companies reach the model apart. A store built with 0.1.0 keeps working: opening it recomputes its organization features with the new normalizer, and a model fitted for the old comparisons is replaced on the next resolve, by refitting or by the starter.

## 0.1.0 (2026-10-10)

The first release.

- **Core** (design 01 to 03). Observations, stable entities, and a ledger of reversible merges and splits in DuckDB. Candidate generation by blocking keys, typed comparisons, Fellegi-Sunter weights fitted by EM with term-frequency adjustments, and validated-components consensus that commits only what the evidence supports and opens a review for the rest. Every decision is stored with its evidence and can be explained.
- **Review and the Workbench** (design 04). Pair constraints, a review queue ranked by impact, dossiers, relationship candidates (a shared brand, a household), and a local web Workbench to browse entities, answer reviews by dragging records, relate or merge entities, and page through possible relationships.
- **Three domains.** Organizations, people, and taxa (scientific plant names, with World Flora Online and the Catalogue of Life as authorities; design 06). Each ships a starter model for stores too small to fit their own.
- **The language-model layer** (design 05). Anthropic and OpenAI-compatible providers, budgets, retries on transient errors, record and replay. An adjudicator for contested pairs (shadow or decide; a model's "same" yields to its own "different" and to cannot-link constraints), a review assistant that suggests answers, name-only resolution, and a per-profile `model_role`.
- **Benchmarks.** Synthetic organizations and people, WFO plant names, and real UK companies from GLEIF and Companies House with company numbers as truth (design 07). `thisness eval` reports precision, recall, false-merge clusters, and a model's accuracy against the truth.
- **Scale.** Tested to 46,565 real records: bounded boundary reviews and relationship neighborhoods, a paginated relationships page, penalty-only common-value adjustments for shared addresses.
