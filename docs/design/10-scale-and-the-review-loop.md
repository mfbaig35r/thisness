# Design 10: scale, and the review loop

Status: accepted 2026-10-18, with the decisions of section 7 as proposed. Steps 1 to 3 done (§10.1 to §10.3); step 3 closed at 217,337 records and 10.7 GB, with decisions 7 and 8 deferred.

Builds on: design 03 (consensus, §9 to §11), design 04 (reviews, §2.4; labels from reviews, §2.7), design 05 (the review assistant, §5), design 07 §13.4 (scale on real organizations) and design 09 §11.4 and §11.5 (the assistant and scale on real people), as of 0.3.1 (commit 0309faf).

## 1. Why

Two things stop Thisness from being used on a real list.

- **It does not scale.** 46,565 records take 7.4 minutes and 15 GB of memory. A vendor master of a few hundred thousand rows is out of reach on a laptop, and Splink resolves millions.
- **Reviews do not scale either.** A run leaves one review for every 18 organization records and one for every 7 people. At 46,565 organization records that is 2,629 reviews. Nobody answers 2,629 reviews, so the recall that answers would buy stays on the table: people's recall is .715 with no contested pair answered and .948 with every one answered correctly (design 09 §11.5).

Part A makes a run cheap. Part B makes reviews cheap and makes each answer count for more than one review.

## 2. What was measured first

UK benchmark, identifiers hidden, seed 20261009, no model, this laptop (24 GB, 12 cores). Time without a profiler; peak memory from memray, which counts what Python and DuckDB allocate. macOS compresses memory, so resident size (`/usr/bin/time -l`) understates it: 7.3 GB resident against 15.2 GB allocated at 46,565 records, and two runs at 24,318 records read 6.3 and 7.8 GB. (memray in turn overstates by about 1 GB that DuckDB reserves and barely touches; from step 2 on, memory is the process's peak footprint, §10.2.)

| Records | Candidate pairs | Pair judgments | Evidence rows | Reviews | Time | Peak memory |
|---|---|---|---|---|---|---|
| 12,605 | 119,976 | 102,843 | 268,117 | 806 | 37 s | 3.8 GB |
| 24,318 | 305,558 | 229,045 | 595,821 | 1,451 | 109 s | not profiled |
| 46,565 | 790,011 | 503,902 | 1,311,655 | 2,629 | 441 s | 15.2 GB |

A file-backed store peaks at the same memory as an in-memory one (12,605 records: 4.37 GB resident either way; the file is 204 MB), so the store's own data is not the problem.

### 2.1 Where the time goes

Profiled at 46,565 records (528 s under the profiler), 69% of the time is four defects, none of them scoring:

| Cost | Time | What |
|---|---|---|
| `Thisness._plan` | 295 s (56%) | For each cluster, a generator scans every folded pair to find the cluster's strong edges: clusters times pairs. |
| `FSModel.ref` | about 40 s (8%) | The judgment loop reads `model.ref.ref` three times per kept pair. Each read rebuilds the model's config and its canonical JSON digest: 1,511,715 digests for one model. |
| `ValidatedComponents` boundaries | 19 s (4%) | For each boundary group, a scan of every contested pair (`consensus.py`, the `pairs = sorted(...)` line): groups times pairs. |
| `DuckDBStore._register` | 12 s (2%) | A component is registered once per entity snapshot (214,286 calls), and each call's datetime parameter makes DuckDB's Python binding try a failed import. |

The real work is scoring (`score_pairs`, 37 s), evidence (`evidence_items`, 43 s) and model validation of the objects they build (about 25 s). Without the four defects the profiled run would take about 160 s of its 528, so an unprofiled 46,565-record run should take a little over two minutes; step 1 measures it.

### 2.2 Where the memory goes

At the 46,565-record peak (15.2 GB), during `record_decisions`:

| Held | Memory | What |
|---|---|---|
| The write path | about 6.9 GB | 1,311,655 `EvidenceItem` objects (about 3.4 GB), 503,902 judgments and their row dicts, their JSON, and the Arrow table built from them (about 3.5 GB), all alive at once. |
| DuckDB | about 3.2 GB | Native allocations: buffers taken during ingest and the inserts. |
| Consensus | about 1.7 GB | Scored rows as Python dicts (`scored.to_pylist()`), folded pairs, edges. |
| `FSModel.ref` leftovers | about 1.1 GB | The canonical JSON of §2.1, still referenced at the peak. Removing it did not lower the peak (§10.1). |

Memory is per pair: about 32 KB per candidate pair at 12,605 records and 19 KB at 46,565. Candidate pairs grow faster than records (each doubling of records multiplied pairs by about 2.6), though blocks are capped (`max_block` 100 and 50), so growth should slow towards linear. If it did not, 250,000 records would make about 9 million pairs and 1,000,000 about 67 million.

### 2.3 What reviews cost, and what the assistant already knows

Reviews per 1,000 records: organizations 68 at 3,507 records, 64 at 12,605, 60 at 24,318 and 56 at 46,565; people 141 at 28,692 (design 09 §11.5).

The review assistant (gpt-6-luna) has been scored against the truth twice:

- **Organizations, 3,507 records** (design 07 §13.3, $0.11): "same" 29 of 29 right, "related" 28 of 28, "different" 75 of 82, a regrouping 22 of 26, 9 unsure. Every wrong answer left a match apart; none merged different companies.
- **People, 10,000** (design 09 §11.4, $0.25): "same" 363 of 377 (96%), a regrouping 83 of 142 (58%), "different" 4 of 7, 94 unsure. Accepting every suggestion would merge different people in 26 reviews.

The organization result predates bounded boundary reviews (design 07 §13.4): it scored 177 reviews where the same run now opens 237. It has to be measured again (§6, step 4).

Answered reviews are already ground truth about pairs (`thisness.labels`, design 04 §2.7), but nothing reads them: "M2 exports them; it does not retrain." The thresholds that decide which pairs become reviews (`FellegiSunterEdgePolicy`: T_high from the posterior, T_low 0) never see an answer.

## 3. Part A: a cheaper run

### 3.1 The four defects

Each is fixed without changing any output:

- `_plan` buckets strong pairs by cluster in one pass (`cluster_of` already exists) instead of scanning all pairs per cluster.
- `FSModel.ref` is computed once per model; the model is frozen, so the ref can be cached on first read.
- Boundary groups take their pairs from one pass over the contested pairs, keyed by group.
- A commit registers each distinct component once, not once per snapshot.

The check: for UK 400, UK 1,600 and EDGAR 1,000, the ledger, memberships, decisions and the mapping export are identical to 0.3.1's, ignoring ids and times.

### 3.2 The write path in chunks

Evidence and judgments are needed only to be written; consensus needs only the edges. So:

1. Scoring yields an Arrow table, and consensus reads its columns (`left_id`, `right_id`, weight, class, relationship signal) without turning rows into dicts.
2. After consensus, the kept pairs are written in chunks (50,000 pairs each, configurable): for each chunk, build its evidence and judgments, write them, drop them. Judgments still cite their evidence ids; a chunk holds both sides of every pair it contains.
3. Decisions, operations and outcomes, one per cluster, stay as they are: they are per cluster, not per pair.

Peak memory then holds the scored table, the edges and one chunk. The store's tables and what they record do not change.

### 3.3 DuckDB's share

DuckDB's 3.2 GB is partly its buffer pool, which by default may grow to 80% of RAM. Step 2 measures what a `memory_limit` setting (exposed as `Thisness(..., memory_limit="4GB")` and `--memory-limit`) does to time and peak; a file-backed store spills, an in-memory one cannot. (Measured in §10.2: it changes neither, so there is no such setting.)

### 3.4 Targets, and a gate before the last one

- **After step 2:** 46,565 records in under 3 minutes and under 4 GB peak.
- **Step 3:** 250,000 records under 8 GB and 30 minutes. That needs under about 900 bytes per pair at the peak if pairs reach 9 million, which a columnar scored table can meet (two ids, a weight and the organization profile's five comparison levels, dictionary-encoded: under 100 bytes) and Python objects per pair cannot.
- **1,000,000 records is a stretch goal, gated on step 3's pair counts.** If pairs grow towards 67 million, scoring alone is about 50 minutes on one core (today 37 s scores 790,011 pairs twice, once per pass of the two-pass common-value adjustment: about 23 µs per pair per pass), and the answer is scoring chunks in parallel processes, which is a decision for then (decision 2).

The UK benchmark is staged from all of Companies House, so it can sample 250,000 records (about 35,000 companies); step 3 first checks the generator holds up at that size.

## 4. Part B: the review loop

### 4.1 A headline number: reviews per 1,000 records

`thisness eval` reports reviews per 1,000 records next to precision and recall, and the README's table gains the column. A change that raises recall by opening twice the reviews is then visible as such.

### 4.2 Accepting suggestions in bulk

A person can accept every open suggestion of chosen kinds at once:

```bash
thisness recommend --limit 5000 --budget 3            # suggestions for every open review
thisness reviews accept --dry-run                     # what would be accepted, by kind
thisness reviews accept                               # the profile's default kinds
thisness reviews accept --resolution same,related     # or name them
```

`Thisness.accept_recommendations(entity_type, resolutions=None, *, actor, reason, dry_run=False)` and a Workbench button over the review queue do the same.

- **Which kinds** a profile accepts by default is part of the profile (`accept_suggestions`), measured per domain like `model_role`: organizations "same", "related" and "different" (a wrong "different" leaves a match apart and never merges); people and taxa none, until measured otherwise. People's wrong "same" answers merged namesakes in 26 reviews.
- **Each accepted review is an ordinary answer:** one `ReviewDecision` per review, its `recommendation_id` set, the person who ran the command as its actor and "accepted the model's suggestion in bulk" as its reason. It is reversible like any other answer.
- **Stale suggestions are skipped.** A suggestion made before the review's members last changed is not accepted.
- **Agreement statistics keep them apart.** `assistant_agreement` counts bulk acceptances separately, so rubber-stamped answers do not inflate the measured agreement between people and the model.

On the 3,507-record organization run, accepting "same", "related" and "different" would have closed 139 of 177 reviews (79%) and merged no different companies, at the cost of 7 matches left apart. The 38 left for a person were 26 regroupings, 9 unsure and 3 without a valid answer. Step 4 measures it again on the current code at 3,507 and 46,565 records (about $2 with gpt-6-luna).

### 4.3 Answers that change the next run, measured first

A review answer today settles its own review and adds constraints for the pairs it names. It says nothing about the next thousand pairs with the same evidence. Two uses of the labels could:

- **Calibrated thresholds** (recommended first): from labelled pairs and their weights, set T_high to the lowest weight above which labelled non-matches are rare, and T_low to the highest weight below which labelled matches are rare (a bound such as Wilson's, so twenty labels do not set a threshold alone). The thresholds become a component with its own ref, citing the review decisions it was computed from.
- **Semi-supervised EM:** labelled pairs enter EM with fixed match probabilities. It changes every weight, which is harder to reason about and to reverse; it comes second, if at all.

With new thresholds, **open reviews are triaged again**: their contested pairs are reclassified, and a review whose pairs all fall outside the band is closed by a system decision citing the calibration. Without that, a better threshold only helps records that arrive later, and a one-off supplier list has none.

This is an experiment before it is a feature. `thisness eval --answer N` has the oracle answer the N most impactful reviews, as a person would, then calibrates, triages again and reports reviews left, precision and recall. The measure is **leverage**: reviews closed per review answered. If answering 200 reviews does not close at least 200 more (leverage at least 1) on UK 46,565 and EDGAR 10,000, labels stay an export and the step stops there.

## 5. Benchmarks and what `eval` gains

- Peak memory per run: the process's peak footprint (macOS) or high-water resident size (Linux), since resident size on macOS counts clean file pages and memray counts DuckDB's untouched reservations (§10.2).
- Reviews per 1,000 records.
- `--accept` (with `--assistant`): apply the suggestions the profile would accept, then report precision, recall and reviews left.
- `--answer N`: the oracle answers N reviews, then calibration and triage (§4.3).
- UK sizes up to 250,000 records.

## 6. Order

1. **The four defects** (§3.1), with the identical-output check. Re-measure the table of §2.
2. **The chunked write path and a columnar scored table** (§3.2), and the `memory_limit` measurement (§3.3). Target: 46,565 records under 3 minutes and 4 GB.
3. **Scale to 250,000 records** (§3.4): the generator at that size, candidate pairs alone first, then a full run. Decide on 1,000,000 from the pair counts.
4. **Reviews per 1,000 records and bulk acceptance** (§4.1, §4.2), with the assistant measured again on organizations at 3,507 and 46,565 records.
5. **The calibration experiment** (§4.3). Build calibration and triage only if leverage clears 1.

Part B does not wait for part A beyond step 1: bulk acceptance at 46,565 records needs runs fast enough to measure.

## 7. Decisions

1. **Scale target.** Proposed: 250,000 records under 8 GB and 30 minutes is the goal; 1,000,000 is gated on measured pair growth (§3.4).
2. **Parallel scoring.** Proposed: not in this design unless step 3 shows one core is the limit at 250,000 records.
3. **Evidence storage stays one row per comparison per pair.** Writing it in chunks keeps the schema; moving evidence into SQL (unpivoting the scored table inside DuckDB) is the fallback only if chunking misses the memory target.
4. **Who accepts in bulk.** Proposed: a person, named as the actor of every accepted review, with the model's recommendation linked. Not an unattended model decision: that is `model_role="decide"`, which stays per profile as now.
5. **Default kinds to accept.** Proposed: organizations "same", "related", "different"; people and taxa none.
6. **Labels.** Proposed: calibrated thresholds and triage, behind the leverage experiment; semi-supervised EM not now.
7. **A benchmark above 217,337 records** (added at step 3). The UK benchmark cannot reach 250,000 without companies that have no LEI. Deferred 2026-10-22: 217,337 is enough to close the scale step.
8. **Consensus on integer indexes, and skipping teardown** (added at step 3), to meet 8 GB at 217,337 records. Deferred 2026-10-22 until a real list needs it; the review loop (steps 4 and 5) comes first.

## 8. Risks

- **Identical output is harder than it sounds.** Ids are time-ordered and some code orders by decision id (`labels` takes the latest judgment by id). Chunking must not change the order judgments are created in.
- **Bulk acceptance invites rubber-stamping.** The dry run shows counts by kind, defaults are per domain and measured, and agreement statistics separate bulk answers. A domain with a measured false-merge rate gets no default.
- **The oracle is not a person.** A simulated reviewer answers every review correctly and in impact order; leverage measured with it is an upper bound.
- **Store size grows with evidence.** 1.3 million evidence rows at 46,565 records; at 250,000 records perhaps 15 million. Step 3 reports file size.
- **Benchmark records are generated** from real companies and people. Scale and review numbers on a real list will differ; the first outside user's data is the check.

## 9. Not in scope

- Porting comparisons to SQL. They are Python functions with domain logic (name parsing, legal forms, numbered vehicles), and chunked Python scoring may be enough.
- Embedding candidates (design 08 §7 still holds: blocking proposes 96 to 98% of true pairs).
- A Postgres store or a service. An MCP server over the local store is a separate design.
- Changing the people profile's `model_role`.

## 10. Implementation notes

### 10.1 Step 1: the four defects

The four fixes of §3.1, as proposed:

- `_plan` maps each node to its clusters once and buckets the strong pairs (and a model's strong answers) by cluster in one pass.
- `FSModel.ref` is a `cached_property`: the model is frozen.
- `ValidatedComponents` assigns each contested pair to its boundary group in one pass; the groups are disjoint (union-find), and a pair whose ends are in different groups, or in none, is counted unasked exactly as before.
- A commit registers each canonicalizer a dispatcher picks once, not once per snapshot.

**Output is identical.** `python -m thisness.benchmark store-digest A B` (new, `benchmark.digest`) hashes every table of two stores, rows sorted and values compared as text, and lists the tables that differ. Evaluation runs are deterministic (seeded ids, a fixed clock: two runs of the old code gave identical digests, and `tests/test_digest.py` checks it). Five runs saved with `--save` before and after, every table identical, ids included:

| Run | Records | Before | After |
|---|---|---|---|
| UK, `--size 400` | 3,507 | 13.1 s | 7.3 s |
| UK, `--size 1600` | 12,605 | 45.1 s | 20.6 s |
| EDGAR people, `--size 1000 --seeds 11` | 2,893 | 8.6 s | 5.8 s |
| Taxa, `--reference` | 4,851 | 14.6 s | 9.5 s |
| Synthetic organizations, 805 | 1,725 | 7.1 s | 4.6 s |

Wall time of the whole command, start-up and benchmark generation included.

**At scale** (fitting and resolving only, as `eval` reports it; same precision, recall, false merges and reviews as in §2):

| Records | Before | After |
|---|---|---|
| 24,318 | 109 s | 37 s |
| 46,565 | 441 s | 88 s |

**Memory did not move:** 15.17 GB peak at 46,565 records, against 15.21. The model ref's 1.1 GB is gone, but the peak now falls a moment later in the same write, when DuckDB's insert buffer (0.9 GB) and more of the run's state are alive. The write path sets the peak, which is step 2.

### 10.2 Step 2: the write path in chunks

- **Consensus reads columns, not dicts.** `scoring.ScoredRows` wraps the scored Arrow table: `rows[i]["weight"]` reads one column, which becomes a Python list the first time it is read. The 790,011 dicts of one row each are gone, and columns nobody reads (most comparison levels) stay in Arrow.
- **Evidence and judgments are written a chunk at a time** (`Thisness(write_chunk=50_000)`). Ids are created in the order they always were, every evidence item before any judgment, so the output is unchanged; what each judgment cites is held in flat lists indexed by kept pair (kept pairs are sorted, so a model's answer finds its pair's evidence by bisection). A judgment chunk is written as soon as it is full.
- **The check:** the five runs of §10.1 are identical to step 1's, table by table, and `tests/test_digest.py` checks that a chunk of seven pairs and one chunk for everything give identical stores.

**How memory is measured from here.** memray counts about 1.07 GB that DuckDB reserves with `aligned_alloc` during ingest at any size; ingesting 3,507 records raised the resident size by 34 MB. Resident size on macOS includes clean file-backed pages (6.6 GB at 46,565 records, mostly the staged UK data the generator reads). The measure is the peak memory footprint `/usr/bin/time -l` reports for the Python process itself: the process's own memory, compressed pages included. Measured that way, with 0.3.1 and step 1 from clean worktrees:

| Records | 0.3.1 | Step 1 | Step 2 |
|---|---|---|---|
| 12,605 | 2.66 GB | | 1.53 GB |
| 46,565 | 14.38 GB | 14.41 GB | 3.24 GB |

46,565 records now take 81 s to fit and resolve (88 s after step 1, 441 s in 0.3.1) and 3.2 GB: the targets of §3.4 (under 3 minutes and 4 GB) are met. Precision, recall, false merges and reviews are unchanged.

**DuckDB's `memory_limit` changes nothing here,** so there is no setting for it (§3.3): at 46,565 records the peak footprint is 3.24 GB with no limit, 3.21 GB at 2 GB and 3.18 GB at 1 GB, and the time 81 s in every case. A smaller write chunk (10,000) changed neither time (80 s) nor peak.

**What is left, for step 3.** Of the remaining peak, the run's per-pair Python state is the part that grows: the columns read as lists (ids, weights, classes), the folded pairs, the judgments' citations (1.3 million evidence id strings) and the candidate pairs. At 250,000 records and perhaps 9 million pairs these grow about tenfold, so step 3 starts by measuring candidate pairs at that size before deciding which of them must become arrays.

### 10.3 Step 3: towards 250,000 records

**The benchmark tops out at 217,337 records.** Targets are drawn from GLEIF entities joined to Companies House, and there are 62,569 of them; `--size 62000` uses nearly all of them. Sizes and candidate pairs (generation, ingest and candidate generation only):

| `--size` | Records | Candidate pairs |
|---|---|---|
| 6,400 | 46,565 | 790,011 |
| 12,800 | 86,136 | 2,012,390 |
| 35,000 | 188,190 | 4,861,683 |
| 62,000 | 217,337 | 5,484,798 |

Pair growth slows towards linear, as the block caps promise: about 26 pairs per record at 86,136 and 25 at 217,337. 250,000 records would need targets without an LEI, which changes the benchmark (decision 7).

**What was fixed**, each checked identical on the five runs of §10.1:

- The first pass of a two-pass common-value adjustment kept its folded pairs alive through the second pass (`_, _, _, _, first = ...` binds `_` to them). Now only its clusters are kept.
- The candidate lists lived until the run returned; they are released once the scored pair list exists.
- The scored pairs are two id lists sharing one string per observation, and the scored rows reuse them as their id columns: before, each structure held its own copy of every id, one per pair.
- Float columns read from the scored table are `array("d")`, eight bytes a value instead of a float object.
- Evidence and judgments are written 10,000 pairs at a time (was 50,000), and the last chunk no longer outlives the loop.

Two tries made no difference and were dropped: scoring into Arrow in chunks, and DuckDB's `memory_limit` with a file-backed store (4.69 GB against 4.64 GB at 86,136 records).

**Results**, file-backed store (`--save`), as a user's store is:

| Records | Pairs | Time | Peak footprint |
|---|---|---|---|
| 86,136, before step 3 | 2,012,390 | 188 s | 6.28 GB |
| 86,136 | 2,012,390 | 200 s | 3.77 GB |
| 217,337, part-way | 5,484,798 | 515 s | 12.7 GB |
| 217,337 | 5,484,798 | 532 s | 10.7 GB |

At 217,337 records: precision .9953, recall .9374, 10,094 reviews. Time is well inside the target (30 minutes); memory is not (8 GB). An in-memory store adds the database itself: 8.93 GB against 6.28 GB at 86,136 records before these fixes.

**How memory is spent now.** tracemalloc puts Python's own peak at 2.93 GB of the 3.85 GB footprint at 86,136 records, so what remains is Python's per-pair state: the folded node pairs (a dict entry, a key tuple and an index list per pair), the class of every pair as a tuple, the evidence and decision ids (one string each, 1.8 million at 46,565 records), and consensus's own maps. Each is small per pair; together they are about 1.9 KB a pair. Reaching 8 GB at 217,337 records needs about a third less, which means consensus working on integer node indexes and arrays rather than dicts of string pairs: a change to `edges.aggregate`, `ValidatedComponents` and `_plan` together, not another trim (decision 8).

**Measuring needs care.** The footprint varies from run to run: two runs of the same code read 3.78 and 3.76 GB, but a run straight after a batch of other runs read 5.17 GB. Every number above is the lower of at least two runs or was checked against a repeat.

**Exit under memory pressure is slow.** A 217,337-record run finished resolving at 532 s and took 77 more minutes to exit while the test suite ran beside it and the machine swapped: Python freeing about 10 GB of objects that had been paged out. Alone, the same run exited at once. A run near the machine's memory pays this; the CLI could skip the interpreter's teardown once its output is written (decision 8).

