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
