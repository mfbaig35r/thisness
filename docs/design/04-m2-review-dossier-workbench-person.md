# Design 04: M2 (Review, Dossier, Workbench, Person)

Status: accepted 2026-10-07 (decisions 1 and 3 to 6 as recommended; decision 2, reference designs, to be settled before the Workbench step).
Builds on: designs 01 to 03 and M1 as built (commit 00cb278).

---

## 1. Scope

M1 resolves organizations end to end, but a person can only see the result through Python, and everything it sends to review stays open forever. M2 closes that loop:

1. **Review queue.** Read a review with its context, decide it, and have the decision change identity state, persist as a constraint, and release its holds.
2. **Dossier.** Everything Thisness believes about one entity, with provenance and contradictions, and a compact text rendering for an LLM (the vision's "context compiler").
3. **Workbench.** `thisness ui identity.duckdb`: a local app over the same public API (invariant 13).
4. **Person profile.** The PRD's second MVP profile, with its own benchmark and starter model.

Not in M2: the LLM layer (M3), PostgreSQL, MCP, the third test domain.

## 2. Review queue

### 2.1 What M1 leaves behind

On the design 03 dataset a bulk run opens about 80 to 100 reviews: boundary reviews (contested neighborhoods, mostly common-name collisions and sibling companies), cluster reviews (conflicts, held weak points) and merge proposals (two active entities). Review members are held: strong edges to them become contested, so later runs do not merge them automatically. Observations in a review got their own provisional entities. Nothing yet closes a review.

### 2.2 A review case

`engine.review_case(review_id)` assembles, from stored records only:

- the review item, the decision that opened it, and its flags (conflicts, bridges, articulations, suggested cut, contested pairs);
- every member node: observations with their attributes and source refs, entities with their current snapshot and size;
- the pair judgments between members, each with its evidence levels and weights;
- the question to ask, by kind: "Are these one entity, or several?" (cluster), "Same entity, or related?" (boundary), "Merge these two entities?" (merge).

### 2.3 Ordering

`engine.reviews(kind=None, order="impact")` sorts open reviews by impact: observations affected, then the size of the largest entity touched, then age. The research report's point holds: a merge that would join two large entities matters more than a near-tie between two singletons.

### 2.4 Resolving a review

`engine.resolve_review(review_id, verdict, *, groups=None, relation=None, reason, actor)` records a `ReviewDecision` and commits one change set:

| Kind | Verdict | Operations |
|---|---|---|
| cluster | `groups`: a partition of the members (or `same`, `different`, or the suggested cut) | per group: merge its provisional entities into one (into the group's active entity, if any) and promote it; must-link within groups; cannot-link across groups |
| boundary | `same` | merge the entities (a human may merge two active entities) |
| boundary | `different` | cannot-link between the clusters |
| boundary | `related` + `relation` | `Relate` (for example `subsidiary_of`) and cannot-link |
| merge | `approve` / `reject` | merge / cannot-link |
| any | `dismiss` | close without changes (holds released) |

Closing a review releases its holds automatically, since holds are derived from open reviews.

### 2.5 Verdicts become constraints

A reviewer who says two clusters are different should never be asked again. M2 adds **pair constraints**, resolution-memory tier B from the PRD:

```text
PairConstraint
  constraint_id, kind (must_link | cannot_link)
  left_id, right_id          observation ids (stable; entities move, observations do not)
  basis_decision_id          the ReviewDecision
  valid_from_seq, valid_to_seq
```

Stored in a new table (migration 0002), written only through `commit` (`AddConstraint`, `RevokeConstraint`, ledger events `CONSTRAINT_ADDED` / `CONSTRAINT_REVOKED`), replayed by the invariant suite. A run maps constraint observations to their current nodes and overrides edges: cannot-link beats any score; must-link becomes strong. Constraints are anchored on observations, so a later split or merge carries them correctly. Revoking one is an ordinary operator action.

### 2.6 Relationship candidates

Pairs with a relationship signal (`brand_only`) between different final entities are relationship candidates. On the design 03 dataset there are about 300 per bulk run, half of them siblings. Opening a review for each would bury the identity queue.

Recommendation: a derived, queryable list (`engine.relationship_candidates(entity_id=None)`), grouped by brand neighborhood, shown in the Workbench next to the entity, not in the review queue. Accepting one is `engine.relate(...)`.

### 2.7 Labels

Every `ReviewDecision` is a labeled example. `engine.labels()` exports (pair, verdict, features, model version) for calibration later (PRD V1). M2 stores and exports; it does not retrain.

## 3. Dossier

`engine.dossier(entity_id)` returns, from stored records:

- the entity, its snapshot, lineage (entities merged into it, splits, redirects) and history summary;
- **attribute support**: for each attribute, every distinct normalized value, how many observations carry it, and which sources;
- **contradictions**: attributes whose single-valued attribute has more than one value among members (two countries, two registration ids in one scheme), with the observations on each side. Derived, not stored. This is the vision's "4 unresolved contradictions";
- members with source refs (paged), relationships, open reviews touching it, relationship candidates.

`dossier.to_context(max_chars=...)` renders it as compact Markdown for an LLM: canonical attributes with support counts, contradictions first, relationships, then sample observations with their source refs, truncated by priority, never mid-record. Deterministic for a given ledger position, and every statement carries a pointer back to an observation or decision. This is the context-compiler claim made testable.

## 4. Workbench

### 4.1 Constraints

- **In-process.** DuckDB allows one writer, so the UI is served by the process that owns the store (design 02 §9).
- **Public API only.** The UI calls `Thisness` methods, never SQL (invariant 13). Anything the UI needs that the API lacks is added to the API first.
- **Optional install.** `pip install thisness[ui]`; the core stays light.
- **Local by default.** Binds to localhost; no auth in M2.

### 4.2 Stack (decision 1)

| Option | For | Against |
|---|---|---|
| **FastAPI + Jinja templates + HTMX** | Python only; ships inside the wheel with no build step; fast to iterate; matches the dbt-docs feel the PRD asks for | Less interactive; graph views need a small JS library (vis-network or Cytoscape, loaded as a file) |
| FastAPI JSON API + prebuilt React SPA | Richest interactions | A Node toolchain and a build artifact in a Python package; the API and UI drift apart |
| marimo or Streamlit app | Fastest first screen | Hard to make feel like a product; less control over review actions |

### 4.3 Views

1. **Entities**: search, then an entity page (the dossier): snapshot, attribute support, contradictions, members, relationships, history, a small graph of member observations and their strong edges.
2. **Decision**: what `explain` returns, laid out: verdict, scores, evidence levels with weights, the pairs behind a consensus decision.
3. **Review queue**: ordered list; a review page with the case (§2.2), the suggested cut, and the verdict actions of §2.4.
4. **Profile and model**: attributes, normalizers, generators, comparisons, the active model's level weights (with caps marked), its source (starter or fitted), and the thresholds in use.
5. **Runs**: recent runs with their stats and outcomes.

### 4.4 Process

Reference designs before any screen (decision 2: Twenty, §4.5). Every screen is rendered and screenshotted against real benchmark data and checked before it is shown.

### 4.5 Reference design: Twenty

The Workbench takes its look and layout from [Twenty](https://github.com/twentyhq/twenty), the open-source CRM. A CRM is the right model: it is a tool for looking at records about real-world things, their history and their connections, which is what an identity store is.

**What we take, and where it lands:**

| Twenty | Workbench |
|---|---|
| Left sidebar listing the workspace's objects | Organizations, People (one entry per registered profile), Review queue with an open-count badge, Runs, Model |
| Record index: a dense table with icon-labelled column headers, a view bar of filter and sort chips | Entity index (name, state chip, members, sources, open reviews, last change); review queue (kind, question, observations affected, age) |
| Record show page: a narrow column of fields on the left, tabs on the right | Entity page (the dossier): snapshot attributes on the left, each with its support ("4 of 5 sources") and a contradiction marker; tabs for Members, Relationships, History, Graph |
| Timeline tab | History: ledger events for the entity as a vertical timeline, each opening its decision |
| Side panel (opens a record without leaving the list) | Observations, decisions and `explain` output open in the side panel, so a reviewer never loses the case they are on |
| Kanban board | The `groups` answer: one column per proposed entity, nodes dragged between columns, "new group" as an empty column |
| Command menu (Cmd+K) | Jump to any entity, observation, review or decision by name or ID |
| Tags in muted colors | Entity state (active, provisional, retired), verdicts, edge kinds |

**What we leave:** inline cell editing and record creation. Identity state changes only through decisions (design 02), so the Workbench reads and decides; it never edits a field.

**Tokens and assets.** Twenty's `twenty-ui` package is MIT licensed (the rest of Twenty is AGPL-3.0), and it ships its design tokens as generated CSS custom properties (`theme-light.css`, `theme-dark.css`): Inter, a 4px spacing scale, font sizes from 0.625 to 1.85rem, radii of 2, 4, 8 and 16px, a neutral gray scale, a 500px side panel. We vendor the subset we use, with the MIT notice, under `thisness/ui/static/`. Inter (OFL) and the icon subset (Tabler, MIT, which Twenty also uses) are bundled in the wheel, so the Workbench works offline. We reference the AGPL application's layouts by eye only and copy no code from it, and we use neither Twenty's name nor its logo in the product.

**Stack.** Decision 1 stands. Most of what makes Twenty feel quick is instant navigation and the side panel, both of which HTMX handles with partial swaps. The two pieces that need script are the command menu and drag between columns; drag uses SortableJS (MIT, one file). The graph tab still uses one graph library.

## 5. CLI

`thisness ingest FILE.csv --type organization`, `thisness resolve`, `thisness ui`, `thisness eval` (the benchmark), `thisness history ID`, `thisness check` (the invariant suite). Thin wrappers over the engine.

## 6. Person profile

### 6.1 Attributes

`name` (full), optional `given_name` / `family_name`, `email` (multi), `phone` (multi), `date_of_birth`, `employer`, `address_line`, `city`, `country`, `postal_code`, `identifiers` (`SCHEME:value`, multi).

### 6.2 Normalizers

- **Names**: honorifics and suffixes stripped (Dr, Mr, Ms, Jr, III); given, middle and family parsed (both "Last, First" and "First Last"); initials kept as initials; a nickname map (Bob to Robert, Liz to Elizabeth) and transliteration groups (Mohammad, Muhammad, Mohammed) to a canonical form; a phonetic key.
- **Email**: lowercase; for providers that ignore them, dots and `+tags` removed in the local part.
- **Phone**: digits only, with a default country code from the observation's country; no external dependency.
- **Date of birth**: ISO date, or year and month when that is all there is.
- **Employer**: the organization name key, so employers compare like organizations.

### 6.3 Comparisons

| Comparison | Levels |
|---|---|
| name | exact, given+family exact, nickname or transliteration match, initial-compatible ("M. Khan" vs "Mohammad Khan"), phonetic, family only, else |
| email | exact, else |
| phone | exact, else |
| date_of_birth | exact, year+month, year, different |
| employer | exact, else |
| location | as for organizations (v2) |

### 6.4 Constraints, caps and relationship signals

- Cannot-link: full dates of birth that differ; identifiers in the same scheme that differ.
- Caps (the two-class problem again): **shared phone and shared address** (households, recycled numbers) and **shared employer** (colleagues) are capped; **family-only name match with shared address** is a relationship signal (household), not identity.

### 6.5 Benchmark and starter

A person generator with the person traps from the research report: common names (many John Smiths), married-name changes, nicknames, transliterations, shared households, reused phone numbers, twins (same family name, address and date of birth, different given names), sparse records. A starter model trained on it, as for organizations.

## 7. Order

1. Pair constraints (migration 0002) and the review queue API.
2. Dossier, `to_context`, relationship candidates, labels.
3. CLI and the Workbench.
4. Person profile, benchmark and starter.

Each step ships with tests, benchmark numbers where relevant, and implementation notes, as in M1.

## 8. Decisions (accepted 2026-10-07)

| # | Question | Decision |
|---|---|---|
| 1 | Workbench stack | FastAPI + Jinja + HTMX, with one small graph library served as a static file |
| 2 | Reference designs for the Workbench | Twenty, the open-source CRM (§4.5): its MIT-licensed design tokens, its record table, show page, side panel, kanban and command menu |
| 3 | Relationship candidates | A derived list beside each entity, not review items |
| 4 | Reviewer verdicts persist as revocable pair constraints anchored on observations | Accept |
| 5 | Person name dictionaries | Small curated built-in lists (nicknames, transliteration groups), extensible per profile; no external dataset, so no licensing question |
| 6 | Order | As in §7: review and constraints first, Person last |

## 9. Implementation notes

### 9.1 Step 1: pair constraints and the review queue

- **Storage.** Migration 0002 adds `pair_constraints` (indexed on both endpoints). Constraints are added and revoked only by `AddConstraint` and `RevokeConstraint` in a change set, which emit `CONSTRAINT_ADDED` and `CONSTRAINT_REVOKED`; the invariant suite replays them. Version-one stores upgrade in place.
- **A cannot-link is checked at commit, not by convention.** After versions are finalized, commit maps the final members of every touched entity (and the endpoints of new cannot-links) to their entities and refuses the change set if any open cannot-link would end up inside one entity: `cannot-link con_… would leave obs_a and obs_b together in org_…; revoke it first`. This covers attach, merge, create, split targets, and adding a cannot-link between observations that already share an entity. The invariant suite checks the same rule over the materialized tables. A must-link is advisory: it shapes resolution runs but blocks nothing.
- **Runs see constraints as edges.** Before consensus, open constraints between the entities in a run's graph become edges: cannot-link overrides whatever the scorer said, must-link becomes strong. Holdout is skipped when a conflict is between two entities, since holding a settled entity out would hide the question rather than answer it; the neighborhood goes to review whole.
- **Answering a review** (`Thisness.resolve_review`) records one `ReviewDecision` and commits one change set: splits, then merges and promotions per group, then must-links chaining each group's anchors, cannot-links between the groups' first anchors, a `Relate` for `related`, then `CloseReview`, which releases the holds. An entity node anchors on its first member. `different` and `reject` group by current entity, so they never ask to separate observations that already share one. `groups` may divide an entity: the entity stays with the group holding its own node, or else most of its members, and the rest are split out into new entities that join their groups' merges. Survivors prefer active, then existing, then larger entities.
- **The queue** is ordered by the observations an answer can change, then the largest entity involved, then age. On the 300-organization benchmark it holds 87 boundary and 3 cluster reviews; the largest boundary covers 31 observations in 3 entities with 453 pair judgments behind it.
- **Tests:** `test_constraints.py` (store level: recording, revocation, every way of joining two constrained observations, anchoring across merges) and `test_reviews.py` (engine level over the benchmark: ordering, case contents, each resolution, refusals), plus a consensus test for entity conflicts. 383 tests, 94% coverage.

### 9.2 Step 2: dossier, context, relationship candidates, labels

- **Dossier** (`Thisness.dossier`, `dossier.py`): built from stored records at one ledger position. Attribute values group by the profile's normalized feature where the profile names one (`value_keys`: Organization groups `name` by `name.key`, `country`, `city` and `postal_code` by their features), so "US", "USA" and "United States" are one value with three observations behind it; other attributes group by a case- and space-folded key. Each value carries the observations behind it and a count per source type. A retired id is followed to the entity it was merged into (`lineage.redirected_from`); one that resolves to several entities after a partial split is refused with the list. Lineage reads the ledger: entities merged in, split targets, and the split a target came from.
- **Contradictions are declared by the profile** (`contradictions`, a tuple of `FeatureConflict`): the features members should agree on. Organization declares `reg_id` (compared within a scheme, so an LEI and a DUNS number never contradict) and `country`. Name variants are not contradictions; every organization has them. On the 300-organization benchmark no resolved entity has a contradiction, because the resolver keeps conflicting identifiers and countries apart; they appear after an operator merge or a false merge, which is what they are for.
- **`to_context(max_chars)`** renders Markdown in a fixed priority order: header, contradictions, attributes with support counts, relationships and relationship candidates, open reviews, then the current page of observations with their source refs. Records are added in that order until the next one does not fit; nothing of lower priority appears while something of higher priority is missing, and a final line counts what was left out. Every statement cites observations (three, then "+N"), a decision or evidence. Rendering applies each attribute's `external_model_policy`: `redact` and `hash` transform the value, `local_only` and `forbidden` drop the attribute and name it as withheld. The dossier object itself is unfiltered.
- **Relationship candidates** (`relationships.py`) are derived from stored evidence at a profile's relationship levels. To make that possible, runs now also record the judgments and evidence of pairs carrying a relationship signal even when the pair is negative (about 300 extra judgments per bulk run on the design 03 dataset; no effect on resolution). Pairs map to current entities; pairs inside one entity or already related drop out; the rest group into neighborhoods by union-find, largest first. On the 300-organization benchmark: 53 candidates in 14 neighborhoods, the largest the 14 "Premier Services" companies. Finding: the list mixes real siblings ("Harjas Foods", "Harjas Freight", "Harjas Holdings") with missed duplicates ("Yortan Svcs" five times), since a name match with no other evidence is exactly what both look like. The Workbench should offer merge as well as relate on a candidate.
- **Labels** (`labels.py`): a `ReviewDecision` now records the partition its answer means for every resolution but `dismiss` (it previously did only for `groups`), so labels never have to reconstruct it. Entity nodes expand to their members as of the ledger position just before the answer was committed, so later merges and splits do not change what an answer meant. One label per observation pair: every pair a resolver scored among the observations involved (with its latest judgment's scores and model ref) and every pair of observations the review named, each with both sides' features.
- **Tests:** `test_dossier.py` (support, contradictions, lineage, relationships, rendering priority, budget and policy) and `test_relationships_labels.py` (over the benchmark). 394 tests, 94% coverage.

### 9.3 Step 3: CLI and Workbench

- **CLI** (`thisness`, `cli.py`): `ingest FILE.csv [--resolve]` (columns the profile declares become attributes, multi-valued ones split on `;` or `|`, the rest are kept as observation context; re-ingesting a file stores nothing new), `resolve`, `ui [STORE]`, `demo` (a store built from the synthetic benchmark, with records spread over four source systems, so the Workbench can be tried without data), `history ID`, `dossier ID --chars N`, `reviews`, `check` (the invariant suite; exit status 1 on any violation) and `eval`. The store is `--store`, else `$THISNESS_STORE`, else `identity.duckdb`.
- **Workbench** (`thisness[ui]`; `thisness/ui/`): FastAPI, Jinja templates and htmx, served in-process. It calls only `Thisness` methods; a test fails if `ui/app.py` mentions the store. Step 3 added the read methods it needed to the engine: `entities` (a page of live entities with snapshot name, observation and source counts and open reviews, largest first), `overview`, `runs`, `observation`, `entity_of` and `search` (by name, or any id), backed by three listing queries in the store.
- **Screens, after Twenty (§4.5):** a sidebar of entity types, the review queue with its count, possible relationships, runs and each type's model; the entity table with state filters and a name filter, rows opening the side panel; the entity page with fields (each with its support) and contradictions on the left and tabs for values (every distinct value with a support bar), observations, relationships, history (one timeline item per commit) and context (the `to_context` rendering at 1,000 to 8,000 characters, with copy); the review board, one column per current entity plus a "new entity" column, cards dragged between columns, the pairs the resolver scored listed underneath (those across today's columns first), and the answers of §2.4 with a required reason; possible relationships by neighborhood, each pair opening a side-by-side comparison with "relate" and "same entity: merge"; the model page (levels with m, u and weight, caps and relationship signals marked, thresholds); runs; a Cmd+K command menu. Light and dark follow the system.
- **Assets** are bundled and work offline: tokens extracted from `twenty-ui`'s generated CSS (MIT), Inter (OFL), 39 Tabler icons (MIT), htmx (0BSD), SortableJS (MIT), with the notices in `ui/static/THIRD_PARTY.md`. The whole wheel, with them, is 331 KB.
- **Process:** every screen was rendered against the 300-organization demo store, screenshotted with Playwright at 1440×900 in light and dark, and revised before being shown. Changes from that pass: values group under the snapshot's spelling, so the field and its support agree; relationship candidates moved from an inline form per pair to rows that open a comparison panel; long review columns render compact cards; column counts read "1 of 2" when an entity has members outside the review.
- **Not done here:** the graph tab of §4.3 (deferred; it needs a layout and adds a library for one view), editing anything but identity decisions, and authentication (the server binds to localhost).
- **Tests:** `test_ui.py` (every page and panel over the benchmark, answering reviews and candidates through the forms, refusals, the public-API rule, bundled assets) and `test_cli.py`.

### 9.4 Step 4: Person profile, benchmark and starter

- **What shipped against §6.** Normalizers (`normalize/person.py`): names parsed in both orders, honorifics, generational suffixes and credentials stripped, family-name particles kept ("van Beethoven"), middle names kept as initials, 64 curated nickname and transliteration groups (a name may sit in two: "Sam"), Soundex; email with Gmail dots and known providers' `+tags` removed; phone to E.164 digits with the record's country, trunk 0 dropped, `00` and `+` read as international, extensions dropped; dates of birth with US month-first and elsewhere day-first unless a field is over 12, plus year-month and year; employer through the organization name key; a street address key; identifiers as `SCHEME:value`. Comparisons (`comparisons/person.py`) as §6.3, with three changes: the name comparison has nine levels (exact, given and family, alias, initial, fuzzy, middle conflict, given only, family only, else), since a father and son differ only in a middle initial or suffix and a married woman only in her family name; date of birth has a "transposed" level for a day and month swapped by hand, which does not cannot-link; and address is its own comparison next to location.
- **Constraints:** differing full dates of birth (not a transposition), differing identifiers in one scheme, differing generational suffixes ("Jr." against "Sr."), and the same family name and birth date with given names that cannot be the same person's (twins). The twins constraint allows a nickname, an initial, a typo, and a given name that is the other record's middle initial ("James R. Berg" and "Robert Berg").
- **Relationship patterns.** "Family-only name with shared address" needs two comparisons, so profiles gained `relationship_patterns` (conjunctions of levels) beside `relationship_levels`. Person declares `household` (family only and address exact) and `same_birth_date` (family only and birth date exact). Candidates and the Workbench treat a pattern's name as the signal.
- **Three engine changes the benchmark forced:**
  1. *EM sessions from household-shared blocks.* Blocks on phone and address hold households, and EM learned "same household" as the match class: "family name only" came out at +4.7 bits and the match rate at 0.93. Profiles can now keep generators out of EM (`em_excluded_generators`); they still propose candidates. Person excludes `phone` and `address`.
  2. *Contradicting levels the m floor made positive.* Random pairs almost never land on "middle conflict" or "family only", so a floored m gave them positive weight. Person caps them at -4 and -6 bits, beside the caps §6.4 asked for (phone +4, address +3, employer +2).
  3. *Term frequency for people counts records.* The two-pass entity count (design 03 §21) runs a first pass before the floor, which for people merges same-name strangers and makes their name look rare: every adjustment came out at -0.2. `TermFrequency.count` can now be `observation`; Person uses it (nobody has dozens of records), and false merges at 4,000 people fell from 48 to 25. Organizations keep entity counting.
- **Benchmark** (`benchmark/people.py`, `python -m thisness.benchmark run --type person`): the traps of §6.5 plus fathers and sons, recycled numbers, colleagues, people who move or change jobs, and sparse records. Two generator fixes changed the numbers and matter for reading them: postal codes come from 25 per city (unique random codes made a shared postal code worth +9 bits), and one personal mailbox belongs to one person after normalization (the first version gave two John Taylors `johntaylor@gmail.com`).

  | People | Records | B3 precision | B3 recall | False-merge clusters |
  |---|---|---|---|---|
  | 1,000 | 2,347 | 0.9983 | 0.728 | 4 (3 father and son, 1 common name) |
  | 4,000 | 9,502 | 0.9963 | 0.789 | 25 (12 father and son) |

  Organizations are unchanged (805: 0.9994 / 0.888 / 1; 4,000: 0.9995 / 0.941 / 4).
- **What the numbers say.** Fathers and sons are the residual: a "Michael Smith Sr." record and a "Michael Smith" record with no birth date at one address are not distinguishable from the record, and the constraint only fires when both suffixes are present. Married-name recall is the weakest (0.63): the maiden and married records share an email, phone or birth date only part of the time, and candidate generation connects under half of them. Precision is below Organization's because people share more with strangers than companies do (names, households, employers).
- **Starter** (`profiles/data/person_starter.json`), trained on 4,000 synthetic people with the starter seed; a store with fewer than 200 person records uses it.
- **Elsewhere:** the CLI's `demo` and `eval` take `--type person`; the Workbench reads "People", shows each type's own fields on review cards, offers each profile's `relations` (spouse of, parent of, household member of and so on for people), and groups a value's spellings by running it through the profile's normalizer in its record's context, so three spellings of one phone number are one value.
- **Tests:** `test_person.py` (normalizers, every name and date level, the constraints, patterns, a starter-resolved store with its dossier) and Person cases in `test_benchmark.py`, `test_cli.py` and `test_ui.py`.
