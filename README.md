# Thisness

**Identity resolution for imperfect observations.**

For centuries, Hesperus (the evening star) and Phosphorus (the morning star) were two things. Then observation showed they were the same planet. Two observations, from different times and contexts, of one underlying thing, and the identity established by evidence rather than by definition.

Thisness does that for data. It takes observations from many sources ("IBM", "IBM Corp.", "International Business Machines Corporation", an invoice line, a contract party) and decides what they refer to, keeping the evidence, the decision, and the history behind every conclusion, so any of them can be explained, reviewed, or reversed.

> Status: alpha. Three domains resolve end to end: organizations, people, and taxa (scientific plant names). Each has a review queue, dossiers, a local Workbench, and an optional language model that adjudicates contested pairs or suggests review answers. The API will change. The design is in [`docs/design/`](docs/design/).

```bash
pip install 'thisness[ui]'
```

```python
import csv
from thisness import Thisness
from thisness.profiles.organization import organization

engine = Thisness("identity.duckdb", profiles=[organization()])

# Columns such as name, website, email, registration_ids, country, city, postal_code.
with open("vendors.csv", newline="") as f:
    engine.ingest(
        [
            engine.observe(
                "organization", row, {"type": "csv", "id": "vendors.csv", "locator": {"row": i}}
            )
            for i, row in enumerate(csv.DictReader(f))
        ]
    )

result = engine.resolve_many("organization")  # fits a model on first use, then resolves
print(result.stats)  # pairs scored, clusters, reviews opened

# Later records resolve against what already exists.
outcome = engine.resolve(
    "organization",
    {"name": "IBM Corp.", "website": "https://www.ibm.com"},
    {"type": "crm", "id": "account-17"},
)
print(outcome.outcome, outcome.entity_id)  # ATTACH_TO_EXISTING / CREATE_ENTITY / REQUIRES_REVIEW
engine.explain(outcome.decision_id)  # the decision, its evidence, the pairs behind it
```

A store needs a few hundred records of a type to fit its own model. Below that, Thisness uses a shipped starter model, and replaces it with a fitted one on the first run with enough data.

Every decision is stored with its evidence; merges and splits are reversible ledger events (`engine.merge_entities`, `engine.split_entity`, `engine.history`).

## Domains

A domain is a resolution profile: its attributes, normalizers, comparisons, constraints and the guidance a model is given. Three ship.

- **Organizations** (`thisness.profiles.organization`): names with legal forms, abbreviations, acronyms and group-company qualifiers; websites, emails, registration IDs, addresses. A shared brand or a group's sibling companies are relationship candidates, never merges.
- **People** (`thisness.profiles.person`): names with nicknames, initials and spelling variants; dates of birth, emails, phones, addresses. Same name at the same address (a household, a parent and child) stays a relationship until evidence says otherwise.
- **Taxa** (`thisness.profiles.taxon`): scientific names parsed into genus, epithets, rank and authorship. Synonyms join their accepted taxon when an authority such as World Flora Online or the Catalogue of Life is loaded, and every label says which authority supplied it.

## From the command line

```bash
thisness ingest vendors.csv --resolve   # into identity.duckdb
thisness ui                              # the Workbench: entities, reviews, history, context
thisness --store demo.duckdb demo && thisness ui demo.duckdb    # try it on synthetic data
thisness --store people.duckdb demo --type person                # synthetic people
thisness --store taxa.duckdb demo --type taxon --reference       # real plant names, labelled by WFO
thisness export mapping > mapping.csv   # every record, its entity and the entity's name
```

Supplier lists and spend extracts work as they are: payment-processor prefixes ("SQ *", "PAYPAL *"), store and reference numbers and ERP notes ("DO NOT USE", "ZZ ") are cleaned before names are compared, and a note becomes the record's status in the mapping. A model can suggest readable names for entities whose records are all in capitals (`thisness labels suggest`), which you accept or clear (design 08).

The Workbench is a local web app over the same API: browse entities with the evidence for every value, answer the review queue by dragging records between entities, relate or merge entities that share a brand, page through possible relationships, and see exactly what a language model was given and what it answered.

## With a language model

A model is optional, and the engine never needs one. Where it helps, it is given the contested pairs or the open reviews, with the evidence for each, and its answers are recorded like any other decision.

```bash
pip install 'thisness[ui,openai]'   # or [ui,anthropic]; keys from OPENAI_API_KEY / ANTHROPIC_API_KEY

thisness resolve --provider openai --model gpt-6-luna --budget 2   # contested pairs to the model
thisness recommend --provider openai --model gpt-6-luna --limit 50 # suggested answers for reviews
thisness ui --provider openai --model gpt-6-luna                   # ask from the Workbench
```

What the model does is set per profile (`model_role`): for organizations and taxa it decides contested pairs, and its "same" yields to its own "different" or to a cannot-link constraint; for people it only suggests review answers, since measured on people it was both too cautious and too credulous. `--model-role decide|suggest|shadow|off` overrides that for a run. `--record` keeps every exchange in a cassette and `--replay` answers from one, free and offline. Any OpenAI-compatible server works with `--base-url` (and `--local` when it is yours).

Set `--budget`: there is no default limit.

## How well it works

`thisness eval` resolves a benchmark with known truth and reports precision, recall and false-merge clusters. Measured so far (design 07 §13; design 06 §10):

| Benchmark | Records | P | R | False-merge clusters |
|---|---|---|---|---|
| Real UK companies (GLEIF + Companies House), identifiers hidden | 3,507 | .9992 | .912 | 1 |
| The same, gpt-6-luna deciding contested pairs ($0.62) | 3,507 | .9965 | .938 | 5 |
| Real UK companies, at scale | 46,565 | .9965 | .934 | 63 |
| Synthetic organizations, 4,000 | | .9995 | .938 | 4 |

```bash
thisness eval --size 805 4000                     # synthetic organizations (or --type person)
thisness eval --type taxon --reference            # WFO plant names, with WFO's own rows
python -m thisness.benchmark uk-stage             # download GLEIF and Companies House (~1 GB, once)
thisness eval --source uk --size 400              # real UK companies, identifiers hidden
thisness eval --source uk --adjudicator openai --model gpt-6-luna --mode decide --budget 2
```

The UK benchmark's hard cases are real: 62% of live UK companies share a registered address, and a record that says only "Sports Direct" sits beside four real Sports Direct companies. The record variations (shortened names, typos, partial addresses) are generated from the true company, so the truth is known.

## Known limits

- **Memory grows with candidate pairs.** 46,565 records took 7.5 minutes of CPU and 7.8 GB peak memory; scoring holds every candidate pair in Python. Tested to about fifty thousand records.
- **Candidates come from blocking keys.** There is no embedding-based candidate generator yet, so two records that share no blocking key (a renamed company with nothing else in common, a heterotypic synonym with no authority loaded) are never compared directly.
- **Model costs are real.** About $0.62 for 400 UK companies with gpt-6-luna; the default model is `claude-sonnet-5`. Use `--budget`, and `--replay` to re-run without paying again.
- **The network benchmarks download data.** WFO families beyond the shipped three, the Catalogue of Life, and the UK sources are fetched on demand and cached under `~/.cache/thisness`. Everything else, including the Workbench, runs offline.

## What it is

- **Observations are not entities.** A source record is an observation of something. Entities are stable identities above them, with IDs that never change when names do.
- **Similarity is not identity.** Candidate retrieval, evidence, decisions and identity state are separate steps with separate records. Scores are typed; there is no universal `confidence` float.
- **Identity is not relationship.** "Subsidiary of" and "brand of" are relationships between distinct entities, never merges.
- **Everything is reversible.** Merges and splits are ledger events over memberships. Nothing is destructively collapsed, and the ledger alone can rebuild current state.
- **Models propose; policy decides.** Rules, Fellegi-Sunter, LLMs and people all contribute decisions. Only a commit, citing those decisions, changes identity state.

## Documents

- [Vision](docs/vision.md)
- [Product requirements](docs/prd.md) and [Appendix A: LLM-assisted resolution](docs/prd-appendix-a-llm-resolution.md)
- [Research: the entity-resolution landscape](docs/research/entity-resolution-landscape.md)
- Design:
  - [01 resolution runs and consensus](docs/design/01-resolution-runs.md)
  - [02 core model and storage](docs/design/02-core-model-and-storage.md)
  - [03 resolution pipeline](docs/design/03-resolution-pipeline.md)
  - [04 review, dossier, Workbench, people](docs/design/04-m2-review-dossier-workbench-person.md)
  - [05 the language-model layer](docs/design/05-m3-llm-layer.md)
  - [06 taxonomy, the third domain](docs/design/06-m4-taxonomy.md)
  - [07 real organizations](docs/design/07-m5-real-organizations.md)
  - [08 supplier lists](docs/design/08-supplier-lists.md)
- [Changelog](CHANGELOG.md)

## Development

```bash
uv sync --dev   # includes the ui extra
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Releases publish to PyPI from a version tag: bump `version` in `pyproject.toml`, add a `CHANGELOG.md` entry, then `git tag vX.Y.Z && git push origin vX.Y.Z`.

## Data and license

Thisness is Apache-2.0. See [LICENSE](LICENSE).

Bundled and fetched data keep their own terms, credited where they ship: World Flora Online (CC0 1.0) in `benchmark/data`; GLEIF Golden Copy (CC0 1.0) and Companies House data (Open Government Licence v3.0) in `benchmark/data/uk`; the Catalogue of Life (CC BY 4.0), fetched on demand with attribution; and the Workbench's fonts, icons and scripts in `ui/static/THIRD_PARTY.md`.
