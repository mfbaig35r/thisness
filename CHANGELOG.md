# Changelog

## 0.1.0 (2026-10-10)

The first release.

- **Core** (design 01 to 03). Observations, stable entities, and a ledger of reversible merges and splits in DuckDB. Candidate generation by blocking keys, typed comparisons, Fellegi-Sunter weights fitted by EM with term-frequency adjustments, and validated-components consensus that commits only what the evidence supports and opens a review for the rest. Every decision is stored with its evidence and can be explained.
- **Review and the Workbench** (design 04). Pair constraints, a review queue ranked by impact, dossiers, relationship candidates (a shared brand, a household), and a local web Workbench to browse entities, answer reviews by dragging records, relate or merge entities, and page through possible relationships.
- **Three domains.** Organizations, people, and taxa (scientific plant names, with World Flora Online and the Catalogue of Life as authorities; design 06). Each ships a starter model for stores too small to fit their own.
- **The language-model layer** (design 05). Anthropic and OpenAI-compatible providers, budgets, retries on transient errors, record and replay. An adjudicator for contested pairs (shadow or decide; a model's "same" yields to its own "different" and to cannot-link constraints), a review assistant that suggests answers, name-only resolution, and a per-profile `model_role`.
- **Benchmarks.** Synthetic organizations and people, WFO plant names, and real UK companies from GLEIF and Companies House with company numbers as truth (design 07). `thisness eval` reports precision, recall, false-merge clusters, and a model's accuracy against the truth.
- **Scale.** Tested to 46,565 real records: bounded boundary reviews and relationship neighborhoods, a paginated relationships page, penalty-only common-value adjustments for shared addresses.
