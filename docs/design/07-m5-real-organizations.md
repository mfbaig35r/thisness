# Design 07: M5, real organizations

Status: accepted 2026-10-09, with the recommendations in section 11.

## 1. Why

Every organization and person result so far was measured on generators written for Thisness. The taxa milestone, on real data, is where the surprises came from: guidance that was wrong, authorities that disagree, and a model policy that only real answers exposed. The question M5 answers is whether the settings M4 recommends hold on organizations nobody designed: the model decides for organizations at a cost of a few false merges, and it never folds a parent into its subsidiary often enough to matter.

## 2. Sources

| Source | What | Licence |
|---|---|---|
| GLEIF Golden Copy, Level 1 | Every LEI: legal name, other and transliterated names, legal and headquarters addresses, legal form (ELF code), status, registration authority and the entity's number there | CC0 |
| GLEIF Golden Copy, Level 2 | Direct and ultimate accounting parents between LEIs | CC0 |
| Companies House basic company data | Every live UK company: name, previous names, registered office, company type and status, SIC codes, incorporation date; a monthly bulk snapshot | Open Government Licence v3.0 |

Optional third source, unlabelled: SEC EDGAR issuers (`company_tickers.json`), to see the engine on a source with no shared registry. It is not part of the measured benchmark.

All three are fetched on demand and cached under `~/.cache/thisness/`; the Golden Copy is a zip of about half a gigabyte, streamed and staged in DuckDB, as entity-anthroponym already does. Small fixtures of each (a few hundred records) ship with Thisness for tests: GLEIF's under CC0, Companies House's with the attribution the OGL requires.

## 3. Ground truth

**One organization.** A Companies House record and a GLEIF record name one organization when the LEI's registration authority is Companies House (RA000585 England and Wales, RA000586 Northern Ireland, RA000587 Scotland) and its registration authority entity id is the company number, after normalizing zero padding and the SC, NI, OC and similar prefixes.

**Related, not the same.** Two LEIs in a Level 2 parent relationship are different organizations, related. Where both are UK companies with Companies House records, the benchmark knows four records, two organizations and one relationship: the case where luna folded 10% of distinct-company pairs on the synthetic benchmark.

**Truth has defects, measured rather than assumed.** A GLEIF record can cite the wrong company number; two LEIs can name one company (GLEIF marks some duplicates); an LEI can lapse while the company lives on, or outlive a company that has been dissolved, which the live-only snapshot omits. Step 1 counts each kind, and audits a sample of the join by reading both records, before any resolution result is reported.

## 4. What the resolver sees

**Identifiers hidden by default.** The company number on both sides would decide every pair, and the question is how well names, addresses and the rest do without it. Three modes: hidden; partial (a share of records carry the number, as real feeds do); and visible, as a check that identifiers still decide.

**Records** are each source's own fields, unchanged except for attribute names: legal name and other names, addresses, legal form, status, and from Companies House previous names, company type, SIC codes and incorporation date. What the Organization profile cannot yet use is expected to show up in step 2, and is named in section 7 rather than designed in advance.

## 5. The benchmark

Companies House holds millions of live companies, so the benchmark is a sample built to be hard rather than a random draw:

- UK LEIs with their Companies House records (the matches);
- Companies House companies that share name words or a postcode with those, and have no LEI (the distractors a real search meets);
- LEIs with no live Companies House record (the right answer is no match);
- Level 2 parent and subsidiary pairs, both sides present (related, not the same);
- kinds measured from the data, as for taxa: renamed (a previous name on one side), group company, same name at another address, dissolved.

Sizes of about 2,000, 10,000 and 50,000 records, the largest to see what blocking and the review queue do at a scale the synthetic benchmark never reached.

## 6. Evaluations

1. The Organization profile as it is, identifiers hidden: precision, recall, false-merge clusters, by kind.
2. The profile with the changes step 2 finds necessary.
3. gpt-6-luna in decide mode with yielding, and the review assistant; the fold-together rate measured on the Level 2 pairs, which is the number that decides whether `model_role="decide"` stays the organization default.
4. Identifier modes: partial and visible.
5. Scale: the 50,000-record benchmark's runtime, memory and review queue, and a real store opened in the Workbench.

## 7. Changes expected, not yet designed

- **Several names per organization.** GLEIF's other names and Companies House's previous names are aliases; the Organization profile compares one name per record.
- **Legal forms across vocabularies.** GLEIF uses ISO 20275 ELF codes ("H0PO" for a private limited company), Companies House its own company types; both should normalize to the profile's legal form.
- **UK addresses.** Postcodes are strong and well structured; the location comparison may want a postcode level.
- **Scale.** Blocking and the two-pass term-frequency estimate have run on a few thousand records, not fifty thousand.

## 8. People stay synthetic

Companies House also publishes officers and persons with significant control. Thisness does not use them: real personal data is out of scope for a benchmark, and the Person profile keeps its synthetic one.

## 9. Relation to entity-anthroponym

entity-anthroponym already loads the Golden Copy, matches EDGAR issuers to LEIs by exact normalized name and country, and reached about 40% coverage; its next milestone was fuzzy resolution with a review queue. M5 is that milestone's question measured with ground truth. The code is written fresh in Thisness (Apache-2.0, no dependency on a private repository); the lessons carry over: stream the zip, stage in DuckDB, insert in bulk rather than row by row.

## 10. Order

1. **Data and truth.** Fetch and stage the three sources; build the truth join; count each truth defect; audit a sample by reading both records; ship the fixtures.
2. **Benchmark and first measurement.** The sample of section 5, the profile as it is (evaluation 1), then the profile changes the results call for (evaluation 2).
3. **Models.** Evaluations 3 and 4.
4. **Scale and the Workbench** on a real store (evaluation 5).

## 11. Decisions

| # | Question | Recommendation |
|---|---|---|
| 1 | Which real data | The UK: GLEIF and Companies House, joined by company number. Other open registries (France's SIRENE, Norway's Brønnøysund) later, once the UK result stands |
| 2 | Identifiers | Hidden by default; partial and visible as further modes |
| 3 | Ground truth | GLEIF's registration authority number, with its defects counted and a sample audited before any result is reported |
| 4 | Related organizations | Level 2 parents, measured as "related, not the same" |
| 5 | Data handling | Fetched and cached; small fixtures of each source ship with attribution |
| 6 | Personal data | None: officers and persons with significant control are not used |
| 7 | Code reuse | Lessons from entity-anthroponym, not its code |

## 12. Risks

- **The join may be thinner than hoped.** How many UK LEIs carry a usable company number is unknown until step 1 counts it; if it is small, the benchmark's matches are few and the distractors dominate.
- **Data wrangling comes first.** Expect most of step 1 to be loading and checking data, with no resolution number until step 2.
- **GLEIF records are curated.** Their names and addresses are cleaner than a typical supplier file's, so the benchmark is real but not messy in the way an ERP extract is. Companies House, filed by companies themselves, is the messier side.

## 13. Implementation notes

### 13.1 Step 1: the data and its truth

- **Loading** (`benchmark/uk.py`; `python -m thisness.benchmark uk-stage`, `uk-truth`, `uk-audit`, `uk-fixture`). The three files (GLEIF Level 1 and 2 of 2026-10-09, Companies House of 2026-10-01; about a gigabyte zipped, eight unzipped) are downloaded once to `~/.cache/thisness/uk/raw`, and staging reuses them unless asked to refresh, since GLEIF republishes three times a day. Staging projects what the benchmark needs into one DuckDB file (337 MB): `gleif`, the 239,581 LEIs registered with Companies House or with a UK address; `gleif_rr`, the relationships touching them; `ch`, all 5,704,711 live companies. It takes under ten seconds once unzipped. The company-number rule (zero padding, letter prefixes such as SC, NI, OC) is a DuckDB macro, with the same rule in Python and a test that they agree: a Python function inside DuckDB needs numpy, which Thisness does not depend on.
- **The truth join, counted.** 119,113 LEIs are registered with Companies House; 102,034 join to a live company, 62,569 of them issued and active. Of the rest, 16,659 are LEIs of companies no longer live (inactive in GLEIF, absent from the live snapshot: the right answer for them is no match), 384 are active yet unjoined (a wrong or stale number, or a company dissolved since), and 6 have no usable number. 175 company numbers carry more than one LEI. 4,572 direct and 3,871 ultimate parent links have both sides joined, the "related, not the same" truth.
- **The audit.** On 400 joined pairs, issued and active, the names agree on 399 (one is a rename recorded on the other side) and the postcodes on 379 (17 differ, 4 missing). GLEIF validates UK records against Companies House, so the truth is sound.
- **What that means for the benchmark.** The same soundness makes GLEIF against Companies House easy: the two name an organization identically, and Companies House does not register two live companies with the same name, so an exact name is nearly an identifier. The difficulty in UK data lies elsewhere: 40% of the joined companies share their first two name words with another live company (9,379 with six or more): one group's companies (Sports Direct Holdings, Sports Direct International, Sports Direct Limited, UK Sports Direct Limited) and unrelated namesakes (Smart Steps Nurseries, Smart Steps Solutions). A record that says only "Sports Direct" is the hard case, and neither source contains such records. Step 2 therefore needs records as they appear in a supplier or customer file, generated from the true company the way the taxon benchmark generates names from WFO's: the organizations, their namesakes and their parents real, the way a record writes them generated.
- **The fixture** (`benchmark/data/uk`, 54 KB, Parquet so lists survive): 102 LEIs, 87 of them joined, 15 of companies no longer live, 15 direct parent links, and 373 live companies including every near-namesake of the joined ones; the GLEIF and Companies House attribution is in its README.
- **Tests** (`test_uk.py`): the company-number rule in Python and SQL; the truth report and the audit on the fixture; near-namesakes present and distinct; the attribution; the cache needing all three files.

### 13.2 Step 2: the benchmark, and what real organizations changed in the profile

- **The benchmark** (`uk.generate`, `uk.standard`, `uk.from_fixture`). About n companies with an LEI joined to Companies House are sampled (half from near-namesake groups, an eighth as parent and subsidiary pairs), then every live near-namesake of theirs (at most six to a name), then LEIs of companies no longer live. Each organization has its Companies House record, its GLEIF record when it has an LEI, and up to three supplier-file records (ERP, accounts payable, CRM): the current, a previous or a shortened name, legal forms and words abbreviated, a typo, case, and a full, postcode-only or city-only address. Truth is the company number. Identifiers are hidden by default; `partial` shows them on 30% of records, `visible` on all. 400 sampled companies make 1,157 organizations and about 3,500 records; 2,000 make 5,284 and 15,729.
- **Records nobody could place are not generated.** The first measurement (precision .984, 32 false-merge clusters) was mostly the generator's doing: "Lex Autolease, 25 Gresham Street" written for Lex Autolease (VC) Limited, beside Lex Autolease Limited at the same address; two companies shortened to one brand at one address. A generated name that normalizes to another sampled company's own name, or a shortening another company shares, is generated again. Shortenings that match no company exactly ("Sports Direct") remain: those are the honest hard case. Real name collisions remain too (PAMIA LIMITED and the assurance company PAMIA).
- **Real addresses are shared.** 62% of live UK companies share their registered address and 23% are at one with more than a hundred companies (71-75 Shelton Street, a formation agent, holds 87,347). The Organization profile, tuned on synthetic data where addresses are nearly unique, gave a shared postcode 8 to 9 bits, so a group's companies at their head office merged. The profile now adjusts the postcode for how common it is, as it does the name, which needed several common-value adjustments per profile (the engine change withdrawn in section 10.4 of design 06, reinstated here); the postcode's frequency is counted over records, since a first resolution pass would already have merged a group's companies and hidden how shared their address is. Companies House's country names were added too ("Northern Ireland" did not normalize; nor did the offshore jurisdictions common in the register).
- **Three name levels for group companies** (name comparison version 4), each found in the false merges:
  - `number_differs`: both names numbered, differently ("Omega Galactic Holdings II" and "III", a fund's vehicles 5 and 6), evidence against. Before, "ii" and "iii" scored as a typo, the strongest level.
  - `token_subset`: one name is the other with words added ("Castle Hearing", "Castle Hearing Group", "Castle Hearing (Dumfries)"), capped at 0 bits and a relationship signal. Capped at 2 it still tipped such pairs over the threshold at 5,000 companies, where a shared postcode weighs more.
  - `qualifier_differs`: each name has a word the other lacks, neither a typo nor an abbreviation of the other ("Bridgepoint Advisers UK" and "... Europe", "(GBP)" and "(EURO)"), capped at 0 and a relationship signal. It absorbs most of what `brand_only` caught ("Libid Holdings" and "Libid Solutions"), which stays a relationship signal under its new name.
- **Results** (identifiers hidden unless stated; synthetic numbers from design 03 for comparison):

  | | P | R | false-merge clusters | reviews |
  |---|---|---|---|---|
  | UK, 400 sampled, profile as it was | .984 | .872 | 32 | 261 |
  | UK, 400 sampled, now | .999 | .912 | 2 | 178 |
  | UK, 400 sampled, identifiers on 30% of records | 1.000 | .931 | 0 | 243 |
  | UK, 400 sampled, identifiers visible | 1.000 | 1.000 | 0 | 3 |
  | UK, 2,000 sampled, before the 0 cap | .943 | .930 | 264 | 568 |
  | UK, 2,000 sampled, now | .994 | .936 | 36 | 634 |
  | Synthetic, 805 organizations (M2: .9994 / .888 / 1 / 136) | .9994 | .894 | 1 | 112 |
  | Synthetic, 4,000 (M2: .9995 / .941 / 4) | .9995 | .938 | 4 | 409 |

  The synthetic benchmark lost little and gained at 805 organizations; its "heavy" suppliers, whose noisy records drop a word, now read as a group's other company (their recall floor in the regression test is lowered from .55 to .50, with the reason).
- **The remaining false merges at 2,000 are mostly the truth's.** 24 of the 36 clusters join companies whose names are identical: an ordinary company and an assurance-company (AC), overseas-entity (OE) or overseas-company (FC) registration of the same business. Companies House keeps them on separate registers under separate numbers; the benchmark calls them two organizations, and no resolver could separate them. A refinement of the truth (registrations of one business as one organization) is noted for step 3.
- **Tests** (`test_uk.py`): the benchmark's determinism, sources and kinds; identifiers by mode; no generated name reading as another company's (real collisions allowed); a regression floor on the fixture (P 1.000, R .674, no false merges). `test_comparisons.py` covers the three new levels.

### 13.3 Step 3: the truth refined, and the models on real organizations

- **One legal entity on two registers.** Of the 36 false-merge clusters left at 2,000 sampled companies, 24 joined identically named companies. Not all are one organization: an overseas entity (OE) beside a UK company of the same name usually is two (Cluny Estates Limited in Leicestershire and in Jersey; 1,944 such pairs in the register, at different postcodes). One legal entity is the same foreign company as an overseas company (FC) and an overseas entity (OE) (CNH Industrial N.V., one postcode), an insurer on the assurance-company register (AC) beside its company number, or a UK company's old Northern Irish or Scottish overseas registration (NF, SF). Those, identically named, are now one organization in the truth (kind `registered_twice`); an OE beside a UK company stays two, and merging them is the engine's error.
- **Countries in the generated records.** GLEIF records were written with country GB even for a Jersey or Guernsey entity, which taught EM that a different country means little (-0.4 bits). They now carry GLEIF's legal-address country, supplier records the company's own: different country is -1.6 bits. At 400 sampled, P .9992, one false-merge cluster; at 2,000, P .9963, 24.
- **`thisness eval --source uk`** runs the UK benchmark (`--identifiers hidden | partial | visible`), so the model evaluations go through the same command line as the synthetic ones.
- **Transient failures are retried.** On the first real run, 65 of 625 calls failed on a dropped connection or a read timeout at 16 in flight, and the client gave up on each at once, losing its eight pairs. It now retries an error the provider marks transient three times, after 1, 2 and 4 seconds (`LLMPolicy.retries_on_transient`, `backoff_seconds`); a permanent error is not retried. The re-run lost none.
- **Results** (400 sampled companies, 3,507 records, identifiers hidden):

  | | P | R | false-merge clusters | reviews | cost |
  |---|---|---|---|---|---|
  | No model | .9992 | .912 | 1 | 177 | |
  | gpt-6-luna, decide, yielding | .9965 | .938 | 5 | 182 | $0.62 |
  | Oracle, decide | 1.000 | .948 | 0 | 133 | |

  The contested pairs are not the synthetic benchmark's: of the 5,000 heaviest (of 8,087), the oracle called 91% different, 6% same and 3% related. Real organizations' hard pairs are look-alikes, not variants. Luna answered "related" for 3,728 pairs the truth calls unrelated; many are a group's siblings with no Level 2 link (GLEIF records parents only between LEIs), so the truth's "related" is incomplete and the accuracy figure (.21) understates luna. "Related" sets no edge. Its wrong "same" (80 pairs, 1.75%) cost four false-merge clusters.
- **The review assistant on real organizations.** Luna suggested answers for the 177 open reviews of the same benchmark resolved with no model ($0.11; 174 answered, 2 invalid groupings, 1 skipped). Of the 165 decided suggestions, 154 would be right if accepted (93%): "same" 29 of 29, "different" 75 of 82, "related" 28 of 28, a regrouping 22 of 26. Accepting every suggestion would merge different companies in no review and leave a match apart in eleven. As for people (design 06 §10.10), the model judging a whole review is far more careful than judging one pair.
- **What this says about `model_role`.** For organizations, decide mode buys recall (.912 to .938 of a possible .948) for a little precision (one false-merge cluster to five in 1,155 organizations); the review assistant gives most of the same answers with no unattended merge. The profile keeps `decide` for organizations, and the measured trade is the reason to choose `suggest` where a wrong merge is costly.

### 13.4 Step 4: scale, and the Workbench on a real store

- **Precision fell with size, and the postcode adjustment was why.** At 6,400 sampled companies (46,565 records) precision dropped to .946 with 812 false-merge clusters, from .996 at 2,000. Of 2,436 wrong strong pairs, 2,294 were a group's siblings (`token_subset` or `qualifier_differs`, 0 bits) at one postcode (9.7 bits): about ten bits, under T_high, until the postcode's common-value adjustment added more. Its baseline is the average postcode, which formation agents holding hundreds of records drag up, so an ordinary head office shared by three or four siblings looked rarer than average and earned a bonus. Common-value adjustments may now only discount (`TermFrequency.boost=False`), and the postcode's does: a shared rare postcode is already worth its full weight. At 46,565 records: P .9965, R .934, 63 false-merge clusters; at 15,729, 16 (was 24); at 3,507 and on the synthetic benchmark, unchanged.
- **Reviews are bounded.** Real look-alike companies chain through contested pairs: one boundary review held 419 entities and 38% of all records under review, and neither a reviewer nor the model could answer it. A boundary review now asks about at most twelve clusters (`ValidatedComponents.max_boundary`; consensus ref version 3, the unbounded policy keeps 2). Larger chains are cut into reviews built from the strongest contested pairs first (the engine passes each pair's weight); a pair that would join two full reviews is not asked, which leaves the two apart, and the run reports how many (`boundary_pairs_unasked`). At 3,507 records the largest review fell from 964 records to 32 (237 reviews, 799 pairs unasked); at 46,565, 2,629 reviews and 34,252 pairs unasked, mostly cross-links within large families of namesakes. Recall did not change.
- **Possible relationships at scale.** At 46,565 records the page held 66,978 candidates and took 32 minutes: the candidates computed in 1.3 s, a name per entity cost 24 s, rendering the rest. It now shows fifty neighborhoods a page, largest first, at most fifty pairs each with the rest counted, and names come from one query (`Thisness.names`, `store.snapshot_names`): 3.2 s. Neighborhoods had the reviews' problem, one of them 14,255 entities (80% of the store); they are now built from the best-supported pairs first, at most thirty entities each (`relationships.MAX_NEIGHBORHOOD`), and every candidate is still listed. The first neighborhood is now one family (Gresham House, Gresham Insurance, the numbered Gresham Receivables vehicles).
- **The rest of the Workbench at 46,565 records:** entity list 1.7 s, a search 1.6 s, the review queue 2.4 s, an entity page and its tabs about 1.6 s, a review 1.7 s, global search 0.2 s.
- **Resolution cost at 46,565 records:** 7.5 minutes of CPU (ingest 3 s, fitting and resolving 413 s) and 7.8 GB peak memory. Memory is the next constraint: scoring holds every candidate pair's row in Python. Not addressed here.
- **An intermittent stall, open.** Twice, a run of three test files stalled at full CPU in the same Workbench test (`test_groups_from_the_board_are_saved`); it passes alone and passed in three later runs of the same files, under different hash seeds. No stack was captured. Recorded here so the next occurrence is caught with `faulthandler` registered on a signal (`kill -USR1`), since pytest's fault-handler plugin cancels a dump timer.
- **Tests:** the penalty-only adjustment and its round trip; a long chain of contested pairs cut into reviews of at most five clusters with the strongest pair kept and every pair accounted for; bounded neighborhoods keeping every candidate; the relationships page a page at a time; batched names.
