# Design 09: real people, from SEC EDGAR

Status: accepted 2026-10-15, with the recommendations in section 9.

Builds on: design 04 (the Person profile), design 06 §10.8 to §10.10 (models on people), design 07 (real organizations, whose method this follows) and the edgar-persons-pool store as of its commit 2452789.

## 1. Why

Every person result so far is on the synthetic generator. Design 07 found what synthetic organizations hid (shared addresses, numbered vehicles, group siblings) only once real data was measured; people have had no such test. Design 06 §10.8 to §10.10 also left the model's role for people at `suggest` because a model was both too cautious and too credulous on synthetic people; whether that holds on real people is unknown.

SEC EDGAR has what a people benchmark needs: real individuals with an identifier. Every Section 16 insider (director, officer, holder of more than 10%) files under a CIK, and their filings name their employer, title and business address. The edgar-persons-pool project has already built a store of them: 1,342,527 filings from 2020 to 2026 and 107,860 individuals classified with confidence 0.85 or more who have filed.

**This reverses design 07 §8**, which kept real personal data out of benchmarks. The ground for the change: Section 16 insiders are named by law in a public role, the data is public, and the benchmark keeps only what resolution needs (§7). Section 7 is where the data is limited; decision 1 asks for the reversal to be accepted explicitly.

## 2. What EDGAR is, measured

From the edgar-persons-pool store (individuals, confidence 0.85 or more, with Section 16 filings):

- **Names are conformed, not as written.** EDGAR writes a filer's name one way for its CIK: of 107,860 filers, 106,990 (99.2%) have a single spelling across all their filings, 856 two, 13 three or more. A benchmark of EDGAR records alone would test almost nothing about spelling.
- **Namesakes are real and many.** 2,854 names (as token sets, suffixes ignored) are shared by 6,579 filers: "James Miller", "Michael Smith", "Robert Smith", four or more each. 3,440 filers carry a generational suffix (Jr, Sr, II, III). This is the hard case synthetic people imitate and EDGAR supplies.
- **Names change.** 2,034 filers have former names in EDGAR, often marriage ("Ruhlman Maegan Adams" to "Cross Maegan Adams Ruhlman", "Nye Jean McClung" to "Halloran Jean").
- **Employers and roles per filing.** 83,827 filers report at one issuer, 24,033 at two or more. Titles are free text ("Chief Financial Officer", "See Remarks", "President and CEO").
- **Addresses are business addresses**, often the company's own: a reporting owner at Alphabet gives "ALPHABET INC., 1600 AMPHITHEATRE PARKWAY, MOUNTAIN VIEW, CA".
- **No date of birth, email or phone.** The Person profile's strongest evidence is absent.
- **The truth has defects.** CIK 0001706713 carries both "Morice Rosemary" and "Counts Travis": a CIK that holds two people. Some people hold two CIKs: 308 names among filers appear under more than one CIK, and how many are one person is unmeasured (§5).

## 3. Name order

EDGAR writes names family first with no comma: "SMITH JOHN A", "Vojvodich Radakovich Lynn M". The Person profile's parser reads a name without a comma as given first, so it reads "SMITH JOHN A" as given name "smith", family name "a". Every EDGAR name is misparsed today.

The fix is a record attribute, `name_order` (`given_first`, the default, or `family_first`), set by whoever ingests the source: a source's convention is a fact about the source, not something to guess per name. With `family_first`, the first token is the family name, the second the given name, the rest middle names or initials. Compound family names ("Vojvodich Radakovich Lynn", "Owen Adams Catherine") cannot be split from the name alone; the parser also emits the split with a two-token family name as an alternative key for blocking, so either reading finds its candidates, and the comparison scores the better of the two (decision 3). A new version of the name normalizer and comparison; the starter is rebuilt.

## 4. The benchmark

Like the UK one (design 07 §5): real people and their real namesakes; records generated the way other systems write them, from the true person.

- **Sample.** About n filers (default 1,000): half from namesake groups (with every namesake), an eighth with former names, an eighth with generational suffixes, the rest at random. Kinds: `plain`, `namesake`, `renamed`, `suffix`, `multi_issuer`.
- **Records per person:**
  - **EDGAR**: the conformed name (`name_order: family_first`), the issuer as employer, the title, and city and state from the filing.
  - **Up to three from other systems** (a CRM, a board list, a screening list), given first: the name with a nickname (from the profile's name groups), a dropped or initialled middle name, a former name for renamed people, a typo, case; the employer as written by others (legal form dropped, abbreviated); city only, state only, or none.
- **Truth** is the CIK. People whose CIK holds two people (§2) are left out; people holding two CIKs are one person when the audit (§5) says so.
- **Identifiers.** The CIK is hidden from records by default; `--identifiers partial` shows it on EDGAR records only, as a real feed would.

## 5. Truth audit, before any result

As design 07 did before its first measurement:

- **A CIK with two people.** Count CIKs whose source records share no family-name token across sources; read a sample; exclude them.
- **A person with two CIKs.** For the 308 shared names among filers, compare issuers, titles and addresses; read a sample; record which are one person, and how often, before treating every CIK as a distinct person.
- **Namesakes are different people.** Read a sample of namesake groups to confirm different issuers and places, not one person's two CIKs.

## 6. Evaluations

1. The Person profile as it is (no name order): the cost of misparsing, measured.
2. With `name_order` (§3).
3. The model on real people: gpt-6-luna deciding contested pairs, and as the review assistant, against the oracle, to revisit `model_role="suggest"` for people (design 06 §10.11).
4. At scale: 10,000 filers, for memory and review counts, as design 07 §13.4 did.

## 7. Data handling

- **The benchmark is built from a local edgar-persons-pool store** (`python -m thisness.benchmark edgar-stage --store PATH`), which projects what the benchmark needs into `~/.cache/thisness/edgar/edgar.duckdb`. Thisness does not fetch Section 16 filings itself: that is a multi-day, rate-limited crawl the other project already does.
- **What is kept:** names, former names, CIKs, issuer CIKs and names, titles, role flags, filing dates, city and state. **Not kept:** street addresses and postal codes (a business address identifies a person no better than their employer and city do, and a home address sometimes appears), and nothing outside Section 16.
- **The shipped fixture** is about 150 people from namesake groups and name changes, with the fields above, for tests. EDGAR filings are US government records with no copyright; the README credits the SEC as a courtesy, as the WFO fixture credits WFO.
- **Reproducibility for others** (decision 5): edgar-persons-pool is private, so nobody else can build the full benchmark. Options: publish edgar-persons-pool, publish a derived extract with the §7 fields, or keep the full benchmark local and the fixture public.

## 8. Order

1. Staging and the truth audit (§5, §7).
2. Name order (§3), and evaluation 1 then 2.
3. The benchmark generator (§4) and the fixture.
4. Models on real people (evaluation 3).
5. Scale (evaluation 4).

## 9. Decisions

| # | Question | Recommendation |
|---|---|---|
| 1 | Real personal data in a benchmark (reverses design 07 §8) | Yes, for Section 16 insiders only, with the fields of §7 and no street addresses |
| 2 | Records | Real people and namesakes; records generated as other systems write them, as in design 07 |
| 3 | Name order | A `name_order` record attribute set by the source, with an alternative split for compound family names |
| 4 | Staging | From a local edgar-persons-pool store; Thisness does not crawl EDGAR |
| 5 | Reproducibility for others | Fixture public, full benchmark local, for now; publishing edgar-persons-pool or a derived extract decided after the audit |
| 6 | Truth | The CIK, with CIKs holding two people excluded and people holding two CIKs merged where the audit confirms it |
| 7 | Default size | 1,000 sampled filers; 10,000 for scale |

## 10. Risks

- **Synthetic records on real people** test the generator's idea of how other systems write names, as design 07's supplier records did. The measured gains are on that; a real second source (a board list, a proxy statement) would be a stronger test, out of scope here.
- **Business addresses and employers are strong and shared**: two different directors of one issuer share both. Namesakes at one company (a father and son on one board) are the hardest real case, and the profile's generational-suffix constraint is what separates them.
- **Compound family names** may defeat the alternative split; the audit's sample and evaluation 2 show how often.

## 11. Implementation notes

### 11.1 Step 1: staging and the truth audit

`python -m thisness.benchmark edgar-stage --store PATH` attaches the edgar-persons-pool store read only and writes `~/.cache/thisness/edgar/edgar.duckdb` in under two seconds: 107,860 people (individual, confidence 0.85 or more, at least one Section 16 role), 326,531 names from three sources (section16, submissions, cik_lookup), 2,115 former names, 157,433 roles, and 154,839 city and state rows with filing counts. No street, no postal code; a `meta` table records the source store, the time and the classifier version (2). `edgar-truth` and `edgar-audit` report and sample; `benchmark.edgar.truth()` is the truth the generator will use.

**Spellings.** Counted on letters only (cik_lookup writes capitals, the filings mixed case), 106,077 CIKs (98.3%) have one spelling, 1,724 two, 59 three or more. The second spellings are what the benchmark wants: middle names added or dropped ("Hennemuth Robert" and "Hennemuth Robert George"), typos ("Woelkers Pau C." and "Woelkers Paul C."), suffixes moved ("DAY II ROWLAND W" and "DAY ROWLAND W II"). EDGAR's own namesake marker, a slash and a state ("MCDONALD JOHN D/MO"), appears on 41 CIKs and is stripped before comparing.

**Shared names.** 2,845 name keys (token sets, initials and suffixes ignored) are held by 6,564 CIKs, giving 5,106 pairs. The key is loose on purpose; the pairs were split by name and then by evidence, and samples of each read:

| Pairs | Count | What they are, read |
|---|---|---|
| Conflicting names (different initials, or a suffix on one) | 3,571 | different people: "Walsh Robert B" and "WALSH ROBERT T.", "PENSKE ROGER S" and "Penske Roger S Jr" |
| Compatible names, a shared issuer | 33 | one person with two CIKs, every pair read apart from two Hyatt trusts (left out as not people): "ROLLINS RANDALL R" and "ROLLINS R RANDALL" (Rollins Inc), "Carter Miller Jocelyn" and "Carter-Miller Jocelyn" (Interpublic, Backblaze), "Li Ka Shing" and "SHING LI KA" (ChromaDex) |
| Compatible names, a shared city only | 35 | either: one person whose issuer renamed itself ("Corbus Barclay", Bed Bath & Beyond and Beyond Inc), compound family names reordered ("Pineyro Sublett Carla" and "Sublett Carla Pineyro"), namesakes in a large city ("Zhang Wei", New York) |
| Compatible names, nothing shared | 1,467 | different people: none of 40 read was clearly one person, most with different careers ("Johnson Allison", PayPal marketing and a Pennsylvania bank CFO) |

**One CIK, unrelated names.** 21 CIKs carry two names that share no word. They are not mostly two people:
- 12 are one person renamed: marriage ("ISBELL JERI L" and "STROMQUIST JERI I"), typos corrected ("NEUKERNONS ARNAND"), nicknames and transliterations ("MEYER SOL" and "Mayer Solomon", "Markowiecki Bartek" and "Makowiecki Bartlomiej", "Bakalova Sofya" and "Garnette Sophia").
- 5 look like a CIK handed to another person, CIK 0001706713 among them; one ("West Shauna Faegre Drinker Biddle & Reath LLP" to "Varma Atul") was opened in a lawyer's name.
- 1 has an entity's former name.
- 3 are entities.

**Not people.** About 35 names the edgar-persons-pool classifier (v2) passes as individuals are entities:
- Danish ApS, Norwegian AS, French SAS and SCS;
- grantor trusts ("RF 2018 GRAT");
- SPAC sponsors;
- "GOULD INVESTORS L P".

A rule on those words and on digits finds them. The digit rule also catches 4 people whose EDGAR name carries a number ("Xu Lin 1962", "Isett Thomas Francis 3rd"). The classifier gap is edgar-persons-pool's to fix.

**The truth** (`edgar.truth`), from the above:
- **Left out:**
  - names that are not people (35);
  - CIKs with unrelated names (18 after the entities), since telling the renamed from the reassigned takes reading;
  - both CIKs of the 35 compatible pairs linked only by a city (70).
- **Merged:** the 33 compatible pairs at a shared issuer.
- **Result:** 107,737 CIKs kept, 107,705 people.
- **Different people:** everything else. That is wrong for an unknown few of the 1,467 compatible pairs with nothing shared; the sample read found none clearly.

The renamed people left out are the hardest real cases and are worth a reviewed list later. They are few (12), and the 2,034 people with former names (2,115 former names in all) remain for the `renamed` kind.

**Other counts.** 3,566 people carry a generational suffix; 107,770 have a city; 24,033 report at two or more issuers.

### 11.2 Step 2: name order, evaluations 1 and 2

**Name order.** `name_order` is a person attribute; the name normalizer (version 2) reads a name without a comma family first when it is `family_first`:
- the first word is the family name, with particles before it ("von Emster Kurt") or, as EDGAR sometimes writes them, after the name ("EMSTER KURT VON");
- the next word is the given name; the rest are middle names, kept as initials;
- suffixes may stand anywhere after the family name ("DAY II ROWLAND W").

A comma still means "Family, Given" in either order, and a value other than the two is ignored.

**The compound reading** (decision 3). A family-first name with a full third word gets a second reading: the third word as the given name and the second as the family name ("Pineyro Sublett Carla" read as Carla, Sublett). That is how a given-first record of the same person parses ("Carla Pineyro Sublett" has family name Sublett, middle initial P), and how EDGAR's own second CIK sometimes writes it ("LOSADA MORENO ANGEL" and "Moreno Angel Losada").
- The reading is a blocking key (`pname.reading`, a new `name_reading` generator).
- In the comparison (version 2) it is tried only when the main reading disagrees (`given_only`, `family_only`, `else`). When it agrees, the level is `fuzzy`, as for a given and family name swapped, never `exact`, so "Smith John Albert" read as Albert John is weak support at most.
- Joining the two words ("pineyrosublett") was left out: no given-first record parses that way.

**The benchmark used here** is the minimal one, with step 3's variants still to come. About n people are sampled as §4 says. Each has their EDGAR records (every spelling and former name, family first, the latest issuer as employer, a city) and one record from another system: the conformed name given first ("John A Smith JR"), an issuer 80% of the time, a city 60%. Former names that are trusts or companies ("MCCABE FAMILY TRUST ...") are not records. Default seed 20261016; 1,000 people give about 2,116 records.

**Results** (1,000 people, seeds 20261016 and 1; "oracle" is the simulated adjudicator deciding every contested pair):

| | B3 precision | B3 recall | Candidate pair recall | Reviews |
|---|---|---|---|---|
| Evaluation 1: no name order | .9986 / .9995 | .476 / .477 | .027 / .026 | 53 / 49 |
| Evaluation 1, oracle | .9986 / .9995 | .481 / .481 | | 7 / 3 |
| Evaluation 2: name order | .9975 / .9981 | .718 / .710 | .924 / .923 | 506 / 514 |
| Evaluation 2, oracle | .9975 / .9981 | .939 / .942 | | 58 / 53 |

- **Misparsing costs everything.** Without name order an EDGAR record and a given-first record of the same person almost never meet: "SMITH JOHN A" reads as given name Smith, family name A. Recall is the share of people with a single record.
- **With it, candidates are found** (.92). Without the oracle, most of what remains is boundary reviews: one EDGAR record against one other record, exact name and same issuer, comes to about 12 bits, the threshold.
  - The name weight is fitted on a sample that is half namesakes by design, so an exact name is worth about 10 bits.
  - Employer is capped at 2 bits (design 04 §6.4: colleagues share an employer).
  - Whether a model should decide these is evaluation 3.
- **With the oracle**, recall by kind is about .99 for plain, suffix and multi-issuer people, .96 for namesakes and .78 for renamed people. A married name with nothing else shared (no date of birth in EDGAR) is not found, and should not be on a name alone.
- **The compound reading** adds .7 points of candidate recall (.912 without it) and .2 of B3 recall, with no false merge.
- **The false merges read (seed 20261016) all have a suffix on one side only.**
  - Three are a father and son at one issuer, the father written without a suffix: "SHUFORD ROBERT F" and "SHUFORD ROBERT F JR" (Old Point Financial), "BERKLEY WILLIAM R" and "BERKLEY WILLIAM R JR" (W. R. Berkley), "SCHORSCH NICHOLAS S" and "Schorsch Nicholas S. Jr.".
  - The fourth, "R Scott Greer" and "GREER ROBERT S. JR.", may be one person with two CIKs.
  - The generational-suffix constraint needs a suffix on both sides.
  - The truth calls these different people (§11.1, conflicting suffixes).
  - This is design §10's hardest case, and it is real. A suffix on one side only, at the same issuer, could be its own name level or a review trigger; left for step 4, where the model's answer on these pairs is measured.
- **Given-first names are unchanged:** the synthetic person benchmark gives identical results before and after (seeds 11 and 12).
