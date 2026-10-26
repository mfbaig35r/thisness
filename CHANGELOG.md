# Changelog

## 0.3.5 (2026-10-26)

- **A sharper comparison in the README** with Splink, Dedupe, Zingg and Senzing: Splink's four backends and what it keeps between runs, Dedupe's labels up front against fitting without them, Zingg's licence, and Senzing's scale. No code changes.

## 0.3.4 (2026-10-25)

- **No warning on every read with DuckDB 2.0.** Queries are read into Arrow through a relation (`con.sql`) instead of an `execute` result, which DuckDB 2.0 warned copies the rows once more. Output is identical: three benchmark stores (UK at 3,507 and 12,605 records, EDGAR people at 2,893) hash the same before and after. Tested on DuckDB 1.3.0, 1.5.6 and 2.0.0.
- **Accepting suggestions in bulk, and scale to 217,337 records** (design 10 steps 3 to 5): `thisness reviews accept`, reviews per 1,000 records in `thisness eval`, and lower memory per candidate pair. See the README.
- **Known limits** quote the current scale: 217,337 records in 9 minutes at a 10.7 GB peak.

## 0.3.3 (2026-10-22)

- **DuckDB 2.0 works, and the cap is gone.** The store reads single rows to the end (DuckDB 2.0 refused to register a table while a result read with `fetchone()` was open), and the UK benchmark's SQL uses the `lambda x: ...` syntax, the only one DuckDB 2.0 accepts by default. Tested on DuckDB 1.3.0, 1.5.6 and 2.0.0.
- **CI tests both ends of the dependency ranges.** A job resolves the newest versions the ranges allow and another the oldest, ignoring the lock, on every push and weekly, so a dependency release that breaks a fresh install shows up without waiting for one.

## 0.3.2 (2026-10-22)

- **DuckDB is capped below 2.** DuckDB 2.0.0 refuses to register a table while a query read with `fetchone()` is still open, and the store reads the ledger head that way just before every write: a fresh `pip install thisness==0.3.1` pulled DuckDB 2.0.0 and failed on the first ingest. 0.3.2 installs DuckDB 1.x; support for 2.0 comes in a later release.
- **Faster, smaller runs** (design 10 steps 1 and 2). Four scaling defects are gone (finding each cluster's strong edges, the model's config digest, boundary grouping, registering the canonicalizer), and evidence and judgments are written in chunks of `write_chunk` pairs (default 50,000). 46,565 records: 81 s and a 3.24 GB peak footprint, from 441 s and 14.38 GB. Output is identical, checked with `python -m thisness.benchmark store-digest A B`.

## 0.3.1 (2026-10-18)

- **The default model is gpt-6-luna**, the model behind every published measurement. `--model` and `--group-model` now follow the provider: gpt-6-luna for `openai`, `claude-sonnet-5` and `claude-haiku-4-5` for `anthropic`. Before, `--provider openai` without `--model` sent a Claude model name to OpenAI. `recommend` and `labels` default to `--provider openai`. `Adjudicator`, `ReviewAssistant`, `LabelAssistant` and `NameOnlyPlan` default to gpt-6-luna. The Anthropic provider is unchanged and not measured.
- **The resolve summary counts entities.** `thisness resolve` and `thisness demo` report the entities the store holds ("628 observations resolved; 426 entities in the store; 84 reviews opened") instead of a per-record outcome breakdown that read as if nothing merged.
- **`thisness --version`.**
- **The EDGAR people fixture is out of the package.** It names real people, so it lives in the source repository's `tests/data/edgar`; `edgar.fixture_db` says where it is when it is missing.
- **Docs.** The README compares Thisness with Splink, Dedupe, Zingg and Senzing. The research survey's broken citation markers are removed.

## 0.3.0 (2026-10-16)

Real people (design 09).

- **Name order.** A person record's `name_order` attribute (`given_first`, the default, or `family_first`) says how its source writes a name without a comma; registries such as SEC EDGAR write "SMITH JOHN A JR". A family-first name of three words or more also gets the reading with a compound family name ("Pineyro Sublett Carla" as Carla Pineyro Sublett), which proposes candidates and counts as a `fuzzy` name match. A family-first name is also a blocking key with its order swapped, since registries are not always consistent. Person name normalizer and comparison version 2; the person starter model is rebuilt. Given-first names are unaffected.
- **Person name comparison** (version 2) also:
  - asks a Soundex match to be close by Jaro-Winkler (.8), since Soundex alone pairs "Buchanan" with "Beckmann";
  - makes a near match with a different middle initial or suffix a `middle_conflict`, as an exact one is;
  - gives a suffix on one side only its own level, `suffix_one_side`, capped at 4 bits: a dropped "Jr" and a father and son at one company look the same, so such pairs go to review.

  The `initial` level is capped at 8 bits. On 10,000 real people, false-merge clusters fall from 51 to 29 (precision .9967 to .9985, recall .726 to .715).
- **Given name and employer** is a new person blocking key (`given_employer`): where there is no date of birth, it finds family names that changed or were mistyped. Colleagues share employers, so EM does not learn from its pairs.
- **Benchmarks.** `python -m thisness.benchmark edgar-stage --store PATH` stages SEC Section 16 insiders from an edgar-persons-pool store; `edgar-truth` and `edgar-audit` measure the CIK as truth; `thisness eval --type person --source edgar` runs the benchmark: real people and namesakes, each with their EDGAR records and up to three records from other systems written as those write people (nicknames, initials, dropped suffixes, typos, former names, employers without their legal form). A fixture of about 150 insiders ships for tests. `thisness eval --assistant` has the review assistant answer every review a run leaves open and scores its suggestions against the truth (`benchmark.evaluate.recommendation_accuracy`). On 1,000 real people gpt-6-luna deciding pairs raised false-merge clusters from 2 to 18, while its review suggestions were right 86% of the time; people keep `model_role="suggest"`.
- **Upgrading.** A store built with 0.2.0 keeps working: opening it recomputes person features with the new normalizer, and a person model fitted for the old comparisons is replaced on the next resolve.

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
